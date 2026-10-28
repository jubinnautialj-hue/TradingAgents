"""Opt-in D4-B operational accounting. No prompts, credentials or response text."""
from __future__ import annotations

import copy
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents.dataflows.files import locked
from tradingagents.forward_validation import atomic, digest, read
from tradingagents.llm_clients.reliability import LLMUnavailable

VERSION = "d4b-safety-budget-v1"
DAY = 86_400_000


def implementation_hash():
    import hashlib
    root = Path(__file__).resolve().parents[1]
    names = ("safety_budget.py", "guarded_research.py", "research_budget.py", "auto_research.py")
    return digest({name: hashlib.sha256((root / "tradingagents" / name).read_bytes()).hexdigest() for name in names})


class SafetyStop(LLMUnavailable):
    def __init__(self, reason):
        super().__init__("D4B_" + reason)
        self.reason = reason


class Policy(ResearchModel):
    version: Literal["d4b-safety-budget-v1"] = VERSION
    enabled: bool = False
    accepted_post_response_exposure: bool = False
    optional_source_policy: Literal["strict-v1", "bls-cpi-403-v1", "official-macro-403-v1"] = "strict-v1"
    instrument: Literal["BTC-USDT-SWAP"] = "BTC-USDT-SWAP"
    model: Literal["gpt-6.1-sol"] = "gpt-6.1-sol"
    hours: Literal[72] = 72
    interval_seconds: Literal[86400] = 86400
    max_researches: Literal[3] = 3
    task_seconds: int = Field(default=900, ge=10, le=900)
    research_tokens: int = Field(default=600000, ge=1, le=600000)
    daily_tokens: int = Field(default=600000, ge=1, le=600000)
    campaign_tokens: int = Field(default=1800000, ge=1, le=1800000)
    headroom_tokens: int = Field(default=100000, ge=1, le=600000)
    warning_fraction: float = Field(default=.7, gt=0, lt=1)
    research_calls: int = Field(default=24, ge=1, le=24)
    daily_calls: int = Field(default=24, ge=1, le=24)
    campaign_calls: int = Field(default=72, ge=1, le=72)
    research_http: int = Field(default=240, ge=5, le=240)
    daily_http: int = Field(default=240, ge=1, le=240)
    campaign_http: int = Field(default=720, ge=1, le=720)
    min_available_mib: int = Field(default=1536, ge=1024, le=2048)
    min_free_gib: int = Field(default=10, ge=10)
    execution_enabled: Literal[False] = False
    simulation_admitted: Literal[False] = False
    allow_new_risk: Literal[False] = False

    @model_validator(mode="after")
    def headroom_fits(self):
        if self.headroom_tokens > min(self.research_tokens, self.daily_tokens, self.campaign_tokens):
            raise ValueError("Operational headroom exceeds token thresholds")
        return self


def now_ms():
    return time.time_ns() // 1_000_000


# Fixed reviewed routing only; native FEEDS must agree before a source is admitted.
MACRO_SOURCES = {
    "fed_policy": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "fed_speeches": "https://www.federalreserve.gov/feeds/speeches.xml",
    "cpi": "https://www.bls.gov/feed/cpi.rss",
    "ppi": "https://www.bls.gov/feed/ppi.rss",
    "employment": "https://www.bls.gov/feed/empsit.rss",
}


def optional_macro_source(method, url, policy="strict-v1"):
    """Return a reviewed ID, never trust caller-supplied optional labels."""
    from tradingagents.dataflows.vendors.okx_events import FEEDS
    names = ("cpi",) if policy == "bls-cpi-403-v1" else tuple(MACRO_SOURCES) if policy == "official-macro-403-v1" else ()
    for name in names:
        if FEEDS.get(name) != (MACRO_SOURCES[name], "MACRO", None):
            raise SafetyStop("OPTIONAL_SOURCE_DEFINITION_CHANGED")
    raw = str(url)
    try:
        p = urlsplit(raw)
        if (method != "GET" or p.scheme != "https" or p.port not in {None, 443}
                or p.query or p.fragment or p.username is not None or p.password is not None
                or any(c in raw for c in "\\\r\n\t")):
            return None
        return next((name for name in names if
            (p.hostname, p.path) == (urlsplit(MACRO_SOURCES[name]).hostname, urlsplit(MACRO_SOURCES[name]).path)), None)
    except ValueError:
        return None


def optional_cpi_request(method, url):
    """Compatibility API: remains CPI-only."""
    return optional_macro_source(method, url, "bls-cpi-403-v1") == "cpi"


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()


class Usage(ResearchModel):
    source: Literal["RAW_RESPONSES", "RAW_CHAT"]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    valid: bool = False
    unsupported_fields: Literal["UNKNOWN"] = "UNKNOWN"

    @model_validator(mode="after")
    def arithmetic(self):
        self.valid = (None not in (self.input_tokens, self.output_tokens, self.total_tokens)
                      and self.input_tokens + self.output_tokens == self.total_tokens
                      and (self.cached_input_tokens is None or self.cached_input_tokens <= self.input_tokens)
                      and (self.reasoning_tokens is None or self.reasoning_tokens <= self.output_tokens))
        return self


class RegistrationLink(ResearchModel):
    registration_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["OPEN_LONG_CANDIDATE", "OPEN_SHORT_CANDIDATE", "WAIT", "REDUCE_LONG", "REDUCE_SHORT", "EXIT_CANDIDATE"] | None
    plan_valid: bool
    workflow_status: Literal["COMPLETED", "FAILED", "BLOCKED_DATA"]


def _durable(path, value):
    atomic(path, value)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Ledger:
    """One explicitly approved global root; all launches must use this entry.

    Existing legacy CLIs are deliberately unchanged and are NOT intercepted by
    this opt-in ledger. Keep their research scheduling disabled in deployment.
    """

    def __init__(self, root, *, clock=now_ms):
        self.root = Path(root).resolve()
        self.path = self.root / "journal.json"
        self.clock = clock

    def initialize(self, policy, *, approved=False, namespace="FIXTURE"):
        if namespace not in {"FIXTURE", "PRODUCTION"}:
            raise ValueError("Invalid namespace")
        if namespace == "PRODUCTION" and not (approved and policy.enabled and policy.accepted_post_response_exposure):
            raise SafetyStop("USER_APPROVAL_REQUIRED")
        self.root.mkdir(parents=True, exist_ok=True)
        with locked(self.root / "transaction"):
            if self.path.exists():
                raise SafetyStop("EXISTING_BUDGET_NEVER_RESET")
            value = {"version": VERSION, "root": str(self.root), "namespace": namespace,
                     "implementation_hash": implementation_hash(),
                     "policy": policy.model_dump(), "approved": bool(approved), "events": []}
            _durable(self.path, {**value, "sha256": digest(value)})
            self._append(value, "INITIALIZED", {"expires_ms": self.clock() + 72 * 3600_000})

    def load(self):
        """Strictly read-only; never creates a root or a lock."""
        obj = read(self.path)
        value = {k: v for k, v in obj.items() if k != "sha256"}
        if obj.get("sha256") != digest(value) or value.get("root") != str(self.root) or value.get("version") != VERSION:
            raise SafetyStop("LEDGER_FINGERPRINT")
        if value.get("implementation_hash") != implementation_hash(): raise SafetyStop("IMPLEMENTATION_CHANGED")
        Policy.model_validate(value["policy"])
        previous = None
        for i, event in enumerate(value["events"]):
            if (event["seq"] != i or event["previous"] != previous
                    or event["hash"] != digest({k: v for k, v in event.items() if k != "hash"})):
                raise SafetyStop("EVENT_CHAIN")
            previous = event["hash"]
        if not value["events"] or value["events"][0]["kind"] != "INITIALIZED":
            raise SafetyStop("INITIALIZATION_INCOMPLETE")
        return value

    def _append(self, obj, kind, payload):
        if len(obj["events"]) >= 10000:
            raise SafetyStop("JOURNAL_CAPACITY")
        t = self.clock()
        if obj["events"] and t < obj["events"][-1]["at_ms"]:
            raise SafetyStop("CLOCK_BACKWARDS")
        event = {"seq": len(obj["events"]), "at_ms": t, "kind": kind, "payload": payload,
                 "previous": obj["events"][-1]["hash"] if obj["events"] else None}
        event["hash"] = digest(event)
        obj["events"].append(event)
        if len(str(obj)) > 16 * 1024 * 1024:
            raise SafetyStop("JOURNAL_CAPACITY")
        _durable(self.path, {**obj, "sha256": digest(obj)})

    @staticmethod
    def view(obj):
        state = {"runs": {}, "calls": {}, "wires": {}, "days": {}, "tokens": 0,
                 "llm_calls": 0, "http": 0, "paused": [], "expires_ms": None}

        def daily(day):
            return state["days"].setdefault(day, {"tokens": 0, "llm_calls": 0, "http": 0})

        for e in obj["events"]:
            p, kind, day = e["payload"], e["kind"], day_of(e["at_ms"])
            if kind == "INITIALIZED": state["expires_ms"] = p["expires_ms"]
            elif kind == "PAUSED": state["paused"].append(p["reason"])
            elif kind == "RESUMED": state["paused"] = []
            elif kind == "RESEARCH_STARTED":
                state["runs"][p["run_id"]] = {**p, "started_ms": e["at_ms"], "tokens": 0,
                                                "llm_calls": 0, "http": 0, "status": "RUNNING"}
            elif kind == "RESEARCH_FINISHED":
                state["runs"][p["run_id"]].update(status=p["status"], registration=p.get("registration"), outcome=p.get("outcome"))
            elif kind == "CALL_STARTED":
                state["calls"][p["call_id"]] = {**p, "started_ms": e["at_ms"], "status": "PENDING", "usage": None}
                state["llm_calls"] += 1
                state["runs"][p["run_id"]]["llm_calls"] += 1
                daily(day)["llm_calls"] += 1
            elif kind == "HTTP_STARTED":
                state["wires"][p["wire_id"]] = {**p, "started_ms": e["at_ms"], "status": "PENDING"}
                state["http"] += 1
                state["runs"][p["run_id"]]["http"] += 1
                daily(day)["http"] += 1
            elif kind == "HTTP_FINISHED": state["wires"][p["wire_id"]].update(p)
            elif kind == "CALL_FINISHED":
                call = state["calls"][p["call_id"]]
                call.update(p)
                n = (p["usage"] or {}).get("total_tokens")
                if isinstance(n, int):
                    state["tokens"] += n
                    state["runs"][call["run_id"]]["tokens"] += n
                    # Cross-midnight exposure counts conservatively in BOTH UTC
                    # days, while campaign/research count a response only once.
                    for charged_day in {day_of(call["started_ms"]), day}:
                        daily(charged_day)["tokens"] += n
        return state

    @contextmanager
    def transaction(self):
        with locked(self.root / "transaction"):
            obj = self.load()
            yield obj, self.view(obj), Policy.model_validate(obj["policy"])

    def _pause(self, obj, reason):
        if reason not in self.view(obj)["paused"]:
            self._append(obj, "PAUSED", {"reason": reason})

    def pause(self, reason="MANUAL"):
        if reason != "MANUAL": raise ValueError("CLI only accepts MANUAL pause")
        with self.transaction() as (obj, _, __): self._pause(obj, reason)

    def resource_pause(self):
        with self.transaction() as (obj, _, __): self._pause(obj, "RESOURCE_ADMISSION_FAILURE")

    def resume(self, *, resource_rechecked=False):
        with self.transaction() as (obj, state, _):
            allowed = {"MANUAL", "RESOURCE_ADMISSION_FAILURE"} if resource_rechecked else {"MANUAL"}
            if (any(r not in allowed for r in state["paused"])
                    or any(c["status"] != "COMPLETE" for c in state["calls"].values())
                    or any(w["status"] == "PENDING" for w in state["wires"].values())
                    or any(r["status"] == "RUNNING" for r in state["runs"].values())
                    or self.clock() >= state["expires_ms"]):
                raise SafetyStop("UNSAFE_RESUME")
            self._append(obj, "RESUMED", {})

    def check(self, state, policy, run_id=None):
        if state["paused"]: raise SafetyStop("PAUSED")
        if self.clock() >= state["expires_ms"]: raise SafetyStop("CAMPAIGN_EXPIRED")
        if run_id and state["runs"][run_id]["status"] != "RUNNING": raise SafetyStop("RESEARCH_NOT_ACTIVE")
        if run_id and (self.clock() - state["runs"][run_id]["started_ms"] >= policy.task_seconds * 1000):
            raise SafetyStop("TASK_TIMEOUT")

    def start_research(self):
        with self.transaction() as (obj, state, policy):
            self.check(state, policy)
            if self.clock() + policy.task_seconds * 1000 > state["expires_ms"]:
                raise SafetyStop("INSUFFICIENT_CAMPAIGN_WINDOW")
            runs = list(state["runs"].values())
            if any(r["status"] == "RUNNING" for r in runs): raise SafetyStop("INFLIGHT_RESEARCH_UNKNOWN")
            if len(runs) >= policy.max_researches: raise SafetyStop("RESEARCH_COUNT")
            if state["llm_calls"] >= policy.campaign_calls or state["http"] >= policy.campaign_http:
                raise SafetyStop("CAMPAIGN_REQUEST_BUDGET")
            if runs and self.clock() - max(r["started_ms"] for r in runs) < DAY:
                raise SafetyStop("NOT_DUE_24H")
            if state["tokens"] + policy.headroom_tokens > policy.campaign_tokens:
                raise SafetyStop("TOKEN_HEADROOM")
            today = state["days"].get(day_of(self.clock()), {"tokens": 0, "llm_calls": 0, "http": 0})
            if (today["tokens"] + policy.headroom_tokens > policy.daily_tokens
                    or today["llm_calls"] >= policy.daily_calls or today["http"] >= policy.daily_http):
                raise SafetyStop("DAILY_BUDGET")
            rid = uuid4().hex
            self._append(obj, "RESEARCH_STARTED", {"run_id": rid, "instrument": policy.instrument})
            return rid

    def begin_call(self, run_id):
        with self.transaction() as (obj, s, p):
            self.check(s, p, run_id)
            if any(c["status"] == "PENDING" for c in s["calls"].values()): raise SafetyStop("INFLIGHT_CALL_UNKNOWN")
            day = s["days"].get(day_of(self.clock()), {"tokens": 0, "llm_calls": 0, "http": 0})
            run = s["runs"][run_id]
            for counters, token_cap, call_cap in ((s, p.campaign_tokens, p.campaign_calls),
                                                  (day, p.daily_tokens, p.daily_calls),
                                                  (run, p.research_tokens, p.research_calls)):
                if counters["tokens"] + p.headroom_tokens > token_cap: raise SafetyStop("TOKEN_HEADROOM")
                if counters["llm_calls"] >= call_cap: raise SafetyStop("CALL_BUDGET")
            cid = uuid4().hex
            self._append(obj, "CALL_STARTED", {"run_id": run_id, "call_id": cid})
            return cid

    def begin_http(self, run_id, call_id, endpoint_kind, *, source_url=None):
        if endpoint_kind not in {"MODEL", "AUTH", "PUBLIC_GET"}: raise SafetyStop("TRANSPORT_DENIED")
        with self.transaction() as (obj, s, p):
            self.check(s, p, run_id)
            source_id = optional_macro_source("GET", source_url, p.optional_source_policy) if source_url is not None else None
            if source_url is not None and not (source_id and endpoint_kind == "PUBLIC_GET" and call_id is None):
                raise SafetyStop("OPTIONAL_SOURCE_SCOPE_DENIED")
            if endpoint_kind == "MODEL" and (call_id not in s["calls"] or s["calls"][call_id]["status"] != "PENDING"):
                raise SafetyStop("UNSCOPED_MODEL_HTTP")
            if endpoint_kind == "MODEL" and any(w["call_id"] == call_id and w["endpoint_kind"] == "MODEL" for w in s["wires"].values()):
                self._pause(obj, "RETRY_EXPOSURE_UNVERIFIED")
                raise SafetyStop("RETRY_EXPOSURE_UNVERIFIED")
            day = s["days"].get(day_of(self.clock()), {"http": 0})
            if s["http"] >= p.campaign_http or day["http"] >= p.daily_http or s["runs"][run_id]["http"] >= p.research_http:
                raise SafetyStop("HTTP_BUDGET")
            wid = uuid4().hex
            payload = {"wire_id": wid, "run_id": run_id, "call_id": call_id, "endpoint_kind": endpoint_kind}
            if source_url is not None:
                payload["optional_source"] = p.optional_source_policy
                payload["source_id"] = source_id
            self._append(obj, "HTTP_STARTED", payload)
            return wid

    def finish_http(self, wire_id, *, status=None, failed=False, request_hash=None):
        with self.transaction() as (obj, s, policy):
            if s["wires"][wire_id]["status"] != "PENDING": raise SafetyStop("DUPLICATE_HTTP_RESULT")
            self._append(obj, "HTTP_FINISHED", {"wire_id": wire_id, "status": "FAILED" if failed else "RECEIVED",
                                                "http_status": status, "request_id_sha256": request_hash})
            wire = s["wires"][wire_id]
            unavailable = (not failed and status == 403 and policy.optional_source_policy in {"bls-cpi-403-v1", "official-macro-403-v1"}
                           and wire.get("optional_source") == policy.optional_source_policy
                           and wire.get("source_id") in MACRO_SOURCES
                           and wire["endpoint_kind"] == "PUBLIC_GET" and wire["call_id"] is None)
            if unavailable:
                self._append(obj, "OPTIONAL_SOURCE_UNAVAILABLE", {"wire_id": wire_id,
                    "source": MACRO_SOURCES[wire.get("source_id")], "source_id": wire.get("source_id"),
                    "stage": "PUBLIC_HTTP_RESPONSE", "source_policy": policy.optional_source_policy,
                    "http_status": 403, "category": "HTTP_APPLICATION_ERROR", "reason": "HTTP_403",
                    "available": False})
            elif failed or status in {401, 403, 408, 409, 429, 500, 502, 503, 504}:
                self._append(obj, "HTTP_SAFETY_FAILURE", {"wire_id": wire_id, "stage": "HTTP_TRANSPORT" if failed else "HTTP_RESPONSE",
                    "http_status": status, "endpoint_kind": wire["endpoint_kind"],
                    "reason": "TRANSPORT_OR_SERVICE_FAILURE"})
                self._pause(obj, "TRANSPORT_OR_SERVICE_FAILURE")

    def finish_call(self, call_id, usage, *, status="COMPLETE", response_hash=None, reconciliation=True,
                    normalized_total=None):
        if status not in {"COMPLETE", "FAILED", "PARTIAL", "UNKNOWN"}: raise ValueError("Invalid status")
        usage = Usage.model_validate(usage).model_dump() if usage is not None else None
        with self.transaction() as (obj, s, policy):
            call = s["calls"][call_id]
            if call["status"] != "PENDING": raise SafetyStop("DUPLICATE_CALL_RESULT")
            duplicate = response_hash is not None and any(c.get("response_id_sha256") == response_hash for c in s["calls"].values())
            model_wires = [w for w in s["wires"].values() if w["call_id"] == call_id and w["endpoint_kind"] == "MODEL"]
            valid = bool(usage and usage.get("valid") and reconciliation and not duplicate
                         and len(model_wires) == 1 and model_wires[0]["status"] == "RECEIVED"
                         and model_wires[0].get("http_status") is not None
                         and 200 <= model_wires[0]["http_status"] < 300)
            # A duplicate response must NOT increase token counters again.
            persisted_usage = None if duplicate else usage
            self._append(obj, "CALL_FINISHED", {"call_id": call_id, "status": status if valid else "UNKNOWN",
                         "completion_status": status, "usage_state": "VALID" if valid else "UNKNOWN",
                         "usage": persisted_usage, "response_id_sha256": response_hash,
                         "normalized_total_tokens": normalized_total,
                         "raw_vs_normalized_delta": normalized_total - usage["total_tokens"]
                         if type(normalized_total) is int and usage and type(usage.get("total_tokens")) is int else None,
                         "reconciled": bool(reconciliation), "error_type": None if valid else "USAGE_UNCERTAIN"})
            if not valid or status != "COMPLETE": self._pause(obj, "UNKNOWN_OR_FAILED_USAGE")
            state = self.view(obj)
            day = state["days"].get(day_of(self.clock()), {"tokens": 0})
            if (state["tokens"] >= policy.campaign_tokens or day["tokens"] >= policy.daily_tokens
                    or state["runs"][call["run_id"]]["tokens"] >= policy.research_tokens):
                self._pause(obj, "TOKEN_THRESHOLD_REACHED")

    def finish_research(self, run_id, aggregate=None, *, success=False, registration=None):
        registration = RegistrationLink.model_validate(registration).model_dump() if registration is not None else None
        with self.transaction() as (obj, s, _):
            run = s["runs"][run_id]
            if run["status"] != "RUNNING": raise SafetyStop("RESEARCH_ALREADY_FINISHED")
            calls = [c for c in s["calls"].values() if c["run_id"] == run_id]
            if (not success or aggregate is None or aggregate != run["tokens"]
                    or not calls or any(c["status"] != "COMPLETE" for c in calls)
                    or any(w["status"] == "PENDING" for w in s["wires"].values() if w["run_id"] == run_id)):
                self._pause(obj, "RESEARCH_FAILURE_OR_RECONCILIATION")
            outcome = ("RESEARCH_FAILED" if not success else "WAIT" if registration and registration["action"] == "WAIT"
                       else "CANDIDATE" if registration and registration["plan_valid"] else "REVIEW_ONLY")
            self._append(obj, "RESEARCH_FINISHED", {"run_id": run_id, "registration": registration, "outcome": outcome,
                        "status": "COMPLETED" if success and not self.view(obj)["paused"] else "FAILED",
                        "aggregate_tokens": aggregate, "per_call_tokens": run["tokens"]})

    def recover_interrupted(self):
        """Account an orphan as failed/unknown, never replay it or clear quota."""
        active = [rid for rid, run in self.status()["runs"].items() if run["status"] == "RUNNING"]
        for rid in active: self.finish_research(rid, None, success=False)

    def status(self):
        obj = self.load()
        state = self.view(obj)
        state["token_total_scope"] = "REPORTED_ONLY_NOT_BILLABLE_OR_PRE_REQUEST_HARD_CAP"
        state["cost"] = "UNKNOWN"
        state["unknown_usage"] = any(c["status"] != "COMPLETE" for c in state["calls"].values())
        state["total_usage"] = "UNKNOWN" if state["unknown_usage"] else state["tokens"]
        policy = Policy.model_validate(obj["policy"])
        state["warnings"] = []
        for scope, counter, cap in [("CAMPAIGN", state, policy.campaign_tokens),
                                    *[("DAY:" + d, v, policy.daily_tokens) for d, v in state["days"].items()],
                                    *[("RESEARCH:" + r, v, policy.research_tokens) for r, v in state["runs"].items()]]:
            if counter["tokens"] >= cap * policy.warning_fraction:
                state["warnings"].append({"scope": scope, "reported_tokens": counter["tokens"], "soft_threshold": cap})
        state.update(execution_enabled=False, allow_new_risk=False, simulation_admitted=False)
        return copy.deepcopy(state)
