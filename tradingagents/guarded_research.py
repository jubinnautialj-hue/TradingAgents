"""D4-B explicit BTC-only launch; no timers, F2/F3, or strategy changes.

This process-local instrumentation is only installed by the new worker entry.
Legacy modules and their code/protocol hashes remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from tradingagents import auto_research as native
from tradingagents import research_budget as legacy
from tradingagents.dataflows.files import locked
from tradingagents.forward_validation import atomic, digest, read
from tradingagents.safety_budget import Ledger, Policy, SafetyStop, Usage, now_ms, optional_macro_source, MACRO_SOURCES

ROOT = Path(__file__).resolve().parents[1]
CALL = ContextVar("d4b_call", default=None)
WIRE = ContextVar("d4b_wire", default=None)
OBS = ContextVar("d4b_observations", default=None)
RENEWAL = ContextVar("d4b_native_renewal", default=False)


def normalize_usage(raw, source="RAW_RESPONSES"):
    """Only raw SDK usage is authoritative: SDK normalizers may invent zeros."""
    if not isinstance(raw, dict): return None
    keys = ("input_tokens", "output_tokens") if source == "RAW_RESPONSES" else ("prompt_tokens", "completion_tokens")
    details = raw.get("input_tokens_details" if source == "RAW_RESPONSES" else "prompt_tokens_details") or {}
    out = raw.get("output_tokens_details" if source == "RAW_RESPONSES" else "completion_tokens_details") or {}

    def count(v): return v if type(v) is int and v >= 0 else None

    return Usage(source=source, input_tokens=count(raw.get(keys[0])), output_tokens=count(raw.get(keys[1])),
                 total_tokens=count(raw.get("total_tokens")), cached_input_tokens=count(details.get("cached_tokens")),
                 cache_write_tokens=count(details.get("cache_write_tokens")),
                 reasoning_tokens=count(out.get("reasoning_tokens"))).model_dump()


def normalized_matches(result, raw):
    generations = getattr(result, "generations", [])
    if len(generations) != 1 or not raw: return False
    usage = getattr(generations[0].message, "usage_metadata", None)
    if not isinstance(usage, dict): return False
    if not all(type(usage.get(k)) is int and usage[k] == raw[k] for k in ("input_tokens", "output_tokens", "total_tokens")): return False
    for field, group, suffix in (("cached_input_tokens", "input_token_details", "cache_read"),
                                  ("cache_write_tokens", "input_token_details", "cache_creation"),
                                  ("reasoning_tokens", "output_token_details", "reasoning")):
        if raw.get(field) is None: continue
        details = usage.get(group) or {}
        values = [v for k, v in details.items() if k == suffix or k.endswith("_" + suffix)]
        if len(values) != 1 or values[0] != raw[field]: return False
    return True


def secure_url(url):
    """Inspect routing only, never headers, form data, tokens or bodies."""
    raw = str(url)
    try:
        parts = urlsplit(raw)
        valid = (parts.scheme == "https" and parts.hostname and parts.port in {None, 443}
                 and parts.username is None and parts.password is None
                 and not any(c in raw for c in "\\\r\n\t"))
    except ValueError:
        valid = False
    if not valid: raise SafetyStop("INSECURE_TRANSPORT_URL")
    return parts


def endpoint(method, url):
    from tradingagents.llm_clients import chatgpt_auth as auth

    parts = secure_url(url)
    host, path = parts.hostname, parts.path
    # The native client is the single source of endpoint paths. A source/path
    # update cannot silently expand the trusted HTTPS authentication authority.
    token, keys = secure_url(auth.TOKEN_URL), secure_url(auth.JWKS_URL)
    if any(p.hostname != "auth.openai.com" or p.query or p.fragment for p in (token, keys)):
        raise SafetyStop("UNTRUSTED_NATIVE_AUTH_ENDPOINT")
    method = method.upper()
    if host == token.hostname:
        if not parts.query and not parts.fragment:
            if method == "POST" and path == token.path: return "AUTH"
            if method == "GET" and path == keys.path: return "PUBLIC_GET"
        raise SafetyStop("NONPUBLIC_WRITE_DENIED")
    # Retain the two model authorities, but never accept arbitrary suffixes.
    if method == "POST" and not parts.query and not parts.fragment and (host, path) in {
        ("api.openai.com", "/v1/responses"), ("chatgpt.com", "/backend-api/codex/responses")
    }: return "MODEL"
    if method == "GET" and not parts.fragment: return "PUBLIC_GET"
    raise SafetyStop("NONPUBLIC_WRITE_DENIED")


def resource_check(policy):
    """Production is Linux-only until an equivalent Windows job object exists."""
    if sys.platform != "linux": raise SafetyStop("LINUX_CGROUP_REQUIRED")
    import pwd
    if os.geteuid() != pwd.getpwnam("ta-research").pw_uid: raise SafetyStop("DEDICATED_USER_REQUIRED")
    cg = next(s[3:] for s in Path("/proc/self/cgroup").read_text().splitlines() if s.startswith("0::"))
    base = Path("/sys/fs/cgroup") / cg.lstrip("/")
    mem = (base / "memory.max").read_text().strip()
    swap = (base / "memory.swap.max").read_text().strip()
    tasks = (base / "pids.max").read_text().strip()
    quota, period = (base / "cpu.max").read_text().split()
    if (mem == "max" or int(mem) > 1536 * 1024**2 or swap == "max" or int(swap) > 256 * 1024**2
            or tasks == "max" or int(tasks) > 64 or quota == "max" or int(quota) > int(period)
            or os.getpriority(os.PRIO_PROCESS, 0) < 10): raise SafetyStop("RESOURCE_LIMITS_REQUIRED")
    values = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines() if line.split()[1].isdigit()}
    import shutil
    if values["MemAvailable"] < policy.min_available_mib * 1024 or shutil.disk_usage("/").free < policy.min_free_gib * 1024**3:
        raise SafetyStop("RESOURCE_FLOOR")
    result = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized"], capture_output=True, text=True, timeout=5)
    if result.returncode or "NTPSynchronized=yes" not in result.stdout: raise SafetyStop("NTP_REQUIRED")


class Session:
    def __init__(self, ledger, run_id, *, health=None):
        self.ledger, self.run_id = ledger, run_id
        self.policy = Policy.model_validate(ledger.load()["policy"])
        self.health = health or (lambda: resource_check(self.policy))
        self.wall, self.mono = now_ms(), time.monotonic()
        self.meters = []
        self.model_lock = threading.RLock()

    def check(self):
        if abs((now_ms() - self.wall) - (time.monotonic() - self.mono) * 1000) > 1000:
            raise SafetyStop("CLOCK_DRIFT")
        self.health()
        with self.ledger.transaction() as (_, state, policy): self.ledger.check(state, policy, self.run_id)

    def meter_class(self):
        session = self

        class AuditedMeter(legacy.Meter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs); session.meters.append(self)

            def take(self, kind):
                session.check()
                if kind == "llm_calls" and CALL.get() is None: raise SafetyStop("UNSCOPED_LOGICAL_CALL")
                if kind == "http_dispatches" and WIRE.get() is None: raise SafetyStop("UNSCOPED_HTTP")
                super().take(kind)

        return AuditedMeter

    @contextmanager
    def guards(self, meter):
        """Outer logical scope + raw usage tap; legacy counters remain intact."""
        from langchain_core.language_models.chat_models import BaseChatModel
        import langchain_openai.chat_models.base as sdk
        import requests
        from tradingagents.llm_clients import chatgpt_auth as auth
        with legacy.guards(meter), ExitStack() as stack:
            original_refresh = auth._refresh

            def native_refresh(*args, **kwargs):
                # Opaque pass-through: do not inspect or retain credential data.
                marker = RENEWAL.set(True)
                try: return original_refresh(*args, **kwargs)
                except BaseException:
                    # The native client can fall back to a still-valid login.
                    # That fallback must not hide a failed renewal from safety.
                    with self.ledger.transaction() as (obj, _, __): self.ledger._pause(obj, "AUTH_RENEWAL_FAILED")
                    raise
                finally: RENEWAL.reset(marker)

            stack.enter_context(patch.object(auth, "_refresh", native_refresh))
            for name, source in (("_create_usage_metadata_responses", "RAW_RESPONSES"), ("_create_usage_metadata", "RAW_CHAT")):
                original = getattr(sdk, name)

                def usage_tap(raw, *args, _fn=original, _source=source, **kwargs):
                    obs = OBS.get()
                    if obs is not None:
                        value = normalize_usage(raw, _source)
                        if value not in obs["raw"]: obs["raw"].append(value)
                    return _fn(raw, *args, **kwargs)

                stack.enter_context(patch.object(sdk, name, usage_tap))
            generate = BaseChatModel._generate_with_cache

            def invoke_one(model, *args, **kwargs):
                if CALL.get() is not None: raise SafetyStop("NESTED_LOGICAL_CALL")
                self.check()
                if getattr(model, "model_name", None) != self.policy.model: raise SafetyStop("UNAPPROVED_MODEL")
                if self.ledger.load()["namespace"] == "PRODUCTION":
                    from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI
                    if type(model) is not ChatGPTPlanChatOpenAI: raise SafetyStop("AUTH_ADAPTER_CHANGED")
                cid = self.ledger.begin_call(self.run_id)
                ctx = CALL.set(cid); observations = {"raw": []}; token = OBS.set(observations)
                before = meter.value["reported_total_tokens"]
                try:
                    result = generate(model, *args, **kwargs)
                    raw = observations["raw"][0] if len(observations["raw"]) == 1 else None
                    response_id = None
                    generations = getattr(result, "generations", [])
                    if len(generations) == 1:
                        meta = getattr(generations[0].message, "response_metadata", {})
                        rid = meta.get("id")
                        if isinstance(rid, str) and rid.startswith("resp_"): response_id = hashlib.sha256(rid.encode()).hexdigest()
                    reconcile = (normalized_matches(result, raw) and raw.get("valid")
                                 and meter.value["reported_total_tokens"] - before == raw["total_tokens"])
                    self.ledger.finish_call(cid, raw, response_hash=response_id, reconciliation=reconcile,
                                            normalized_total=meter.value["reported_total_tokens"] - before)
                    if self.ledger.status()["paused"]: raise SafetyStop("USAGE_OR_BUDGET_STOP")
                    return result
                except BaseException as exc:
                    state = self.ledger.status()
                    if state["calls"][cid]["status"] == "PENDING":
                        raw = observations["raw"][0] if len(observations["raw"]) == 1 else None
                        self.ledger.finish_call(cid, raw, status="PARTIAL" if getattr(exc, "partial", False) else "FAILED", reconciliation=False)
                    raise
                finally:
                    OBS.reset(token); CALL.reset(ctx)

            def invoke(model, *args, **kwargs):
                # Native analysts may be scheduled concurrently. Serialize only
                # dispatch/accounting, not graph wiring, evidence or prompts.
                with self.model_lock:
                    return invoke_one(model, *args, **kwargs)

            stack.enter_context(patch.object(BaseChatModel, "_generate_with_cache", invoke))

            def no_direct_stream(*args, **kwargs):
                # Internal Responses SSE remains supported via invoke/_stream.
                raise SafetyStop("PUBLIC_STREAM_UNSUPPORTED")

            stack.enter_context(patch.object(BaseChatModel, "stream", no_direct_stream))

            async def no_async_stream(*args, **kwargs):
                raise SafetyStop("ASYNC_UNSUPPORTED")
                yield  # marks this as an async generator without dispatch

            stack.enter_context(patch.object(BaseChatModel, "astream", no_async_stream))

            def send(fn, client, request, *args, **kwargs):
                self.check()
                kind = endpoint(request.method, request.url)
                if kind == "AUTH" and not RENEWAL.get(): raise SafetyStop("AUTH_RENEWAL_CONTEXT_REQUIRED")
                legacy.public_exchange_only(request.method, request.url)
                protected = kind in {"AUTH", "MODEL"} or secure_url(request.url).hostname == "auth.openai.com"
                optional = (CALL.get() is None and kind == "PUBLIC_GET"
                            and optional_macro_source(request.method, request.url, self.policy.optional_source_policy) is not None)
                if (protected or optional) and isinstance(client, requests.Session):
                    # Requests follows redirects inside Session.send; disable
                    # that BEFORE dispatch, not after credentials were replayed.
                    kwargs["allow_redirects"] = False
                wid = self.ledger.begin_http(self.run_id, CALL.get(), kind,
                                            source_url=str(request.url) if optional else None)
                marker = WIRE.set(wid)
                try:
                    response = fn(client, request, *args, **kwargs)
                except BaseException:
                    self.ledger.finish_http(wid, failed=True)
                    raise
                else:
                    status = getattr(response, "status_code", None)
                    request_id = response.headers.get("x-request-id")
                    self.ledger.finish_http(wid, status=status,
                        request_hash=hashlib.sha256(request_id.encode()).hexdigest() if isinstance(request_id, str) else None)
                    reason = ("PROTECTED_REDIRECT_DENIED" if (protected or optional) and isinstance(status, int) and 300 <= status < 400
                              else "AUTH_RESPONSE_REJECTED" if kind == "AUTH" and status != 200 else None)
                    if reason:
                        with self.ledger.transaction() as (obj, _, __): self.ledger._pause(obj, reason)
                        try: response.close()
                        finally: raise SafetyStop(reason)
                    if optional and status == 200:
                        # A streamed body may fail AFTER send() returned 200. It is
                        # a transport failure, never an optional application 403.
                        errors = [requests.RequestException]
                        for name in ("httpx", "httpx2"):
                            try: errors.append(importlib.import_module(name).RequestError)
                            except ImportError: pass
                        attr = "iter_content" if isinstance(client, requests.Session) else "iter_raw"
                        original_iter = getattr(response, attr)
                        def checked_body(*a, **k):
                            try: yield from original_iter(*a, **k)
                            except tuple(errors):
                                with self.ledger.transaction() as (obj, _, __):
                                    self.ledger._pause(obj, "TRANSPORT_OR_SERVICE_FAILURE")
                                raise
                        setattr(response, attr, checked_body)
                    return response
                finally: WIRE.reset(marker)

            old_send = requests.Session.send
            stack.enter_context(patch.object(requests.Session, "send", lambda obj, req, *a, **k: send(old_send, obj, req, *a, **k)))
            for name in ("httpx", "httpx2"):
                try: module = importlib.import_module(name)
                except ImportError: continue
                fn = module.Client._send_single_request
                stack.enter_context(patch.object(module.Client, "_send_single_request",
                    lambda obj, req, *a, _fn=fn, **k: send(_fn, obj, req, *a, **k)))
            yield


def watchlist(policy):
    return native.Watchlist(instruments=[native.Instrument(instrument=policy.instrument)],
        forward_admission_policy=policy.forward_admission_policy, limits=native.Limits(
        min_interval_seconds=86400, max_symbols=1, task_seconds=policy.task_seconds, batch_seconds=policy.task_seconds,
        llm_per_task=policy.research_calls, llm_total=policy.research_calls,
        http_per_task=policy.research_http, http_total=policy.research_http))


def worker_diagnostic(ledger, run_id, *, registration=None, exception=None):
    """Bounded failure metadata only; no prompt/response, environment or raw exception text."""
    import traceback
    import re
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id): return False
    value = {"version": "d4b-worker-diagnostic-v1", "run_id": run_id, "at_ms": now_ms(),
             "execution_enabled": False, "stage": "WORKER_FAILURE"}
    if exception is not None:
        value["exception_type"] = type(exception).__name__[:80]
        reason = getattr(exception, "reason", None)
        value["error_code"] = reason if reason in {"PAUSED", "HTTP_BUDGET", "TOKEN_HEADROOM", "CALL_BUDGET",
            "RESOURCE_FLOOR", "CLOCK_DRIFT", "AUTH_RESPONSE_REJECTED", "PROTECTED_REDIRECT_DENIED"} else "DETAILS_WITHHELD"
        value["frames"] = [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name[:80]}
                           for f in traceback.extract_tb(exception.__traceback__)[-8:]]
    try:
        # Only the native receipt's report within this run's isolated batch tree is inspected.
        name = (registration or {}).get("report_json")
        if name:
            report = Path(name).resolve()
            report.relative_to((ledger.root / "batch").resolve())
            if report.name != "research_result.json": raise ValueError("Unexpected report")
            if report.stat().st_size <= 4 * 1024**2:
                sources = read(report).get("evidence", {}).get("events", {}).get("sources", {})
                failures = []
                for source_id, url in MACRO_SOURCES.items():
                    item = sources.get(source_id, {})
                    if item.get("http_status") == 403 and item.get("source") == url:
                        failures.append({"source_id": source_id, "source": url, "http_status": 403,
                            "stage": "PUBLIC_HTTP_RESPONSE", "category": "HTTP_APPLICATION_ERROR", "reason": "HTTP_403",
                            "observed_ms": item.get("failed_at_ms") if type(item.get("failed_at_ms")) is int else None})
                if failures:
                    value["source_failures"] = failures
                    cpi = next((f for f in failures if f["source_id"] == "cpi"), None)
                    if cpi: value["optional_source_failure"] = cpi  # legacy diagnostic key, not the global cause
            snapshot = report.with_name("multitimeframe_snapshot.json").resolve()
            snapshot.relative_to((ledger.root / "batch").resolve())
            if snapshot.is_file() and snapshot.stat().st_size <= 4 * 1024**2:
                raw = read(snapshot)
                value["market_status"] = raw.get("status") if raw.get("status") in {"VALID", "RESEARCH_INVALID", "DATA_UNAVAILABLE"} else "UNKNOWN"
                contexts = raw.get("error_context", [])
                value["market_errors"] = [{k: c[k] for k in ("stage", "exception_type", "error_code")
                                           if k in c and isinstance(c[k], str) and c[k].replace("_", "").isalnum() and len(c[k]) <= 80}
                                          for c in contexts[:4] if isinstance(c, dict)]
        journal = ledger.load()
        first = next((e for e in journal["events"] if e["kind"] == "PAUSED"), None)
        if first:
            value["first_global_pause"] = {"reason": first["payload"]["reason"], "at_ms": first["at_ms"], "seq": first["seq"]}
            cause = next((e for e in reversed(journal["events"][:first["seq"]]) if e["kind"] == "HTTP_SAFETY_FAILURE"), None)
            if cause: value["first_global_pause"]["http_failure"] = cause["payload"]
        folder = ledger.root / "diagnostics"
        folder.resolve().relative_to(ledger.root.resolve())
        folder.mkdir(exist_ok=True)
        path = folder / (run_id + ("-exception" if exception else "-result") + ".json")
        if not path.exists(): atomic(path, value)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Diagnostic failure does not suppress the original failure or alter budget state.
        return False
    return True


def worker(ledger, run_id, *, invoker=None):
    obj = ledger.load(); policy = Policy.model_validate(obj["policy"])
    if obj["namespace"] == "FIXTURE" and invoker is None: raise SafetyStop("FIXTURE_CANNOT_CALL_MODEL")
    if obj["namespace"] == "PRODUCTION" and invoker is not None: raise SafetyStop("FIXTURE_CANNOT_REGISTER_PRODUCTION")
    if obj["namespace"] == "PRODUCTION":
        if not (obj["approved"] and policy.enabled and policy.accepted_post_response_exposure): raise SafetyStop("USER_APPROVAL_REQUIRED")
        reviewed_implementation()
    session = Session(ledger, run_id, health=(lambda: None) if invoker else None)
    result = None
    try:
        session.check()
        with patch.object(native, "Meter", session.meter_class()), patch.object(native, "guards", session.guards):
            def inline(header, folder, seconds):
                value = native.worker(folder, invoker=invoker)
                return {"returncode": value.get("exit_code", 5), "timed_out": False}
            result = native.run_batch(watchlist(policy), ledger.root / "batch", namespace=obj["namespace"],
                                      runner=inline, validator=native.validate_pool)
        # Exactly one original task with a verified F1 registration is required.
        state = ledger.status()
        calls = [c for c in state["calls"].values() if c["run_id"] == run_id]
        aggregate = sum(m.value["reported_total_tokens"] for m in session.meters)
        ok = bool(calls) and all(c["status"] == "COMPLETE" for c in calls)
        # Native receipt is re-verified using the unchanged F1 recovery path.
        store = native.Store(ledger.root / "batch", obj["namespace"])
        tasks = store.tasks()
        registered = [native.recover(store, h) for h in tasks if h["instrument"] == policy.instrument
                      and h["schedule_slot_ms"] == result.get("schedule_slot_ms")]
        ok = ok and any(v and v.get("registration") and v["registration"].get("workflow_status") == "COMPLETED"
                        and v["registration"].get("llm_status") == "AVAILABLE"
                        and v["registration"].get("allow_new_risk") is False
                        and v["registration"].get("execution_enabled") is False for v in registered)
        reg = next((v["registration"] for v in registered if v and v.get("registration")), None)
        link = {k: reg.get(k) for k in ("registration_id", "action", "plan_valid", "workflow_status")} if reg else None
        ledger.finish_research(run_id, aggregate, success=ok, registration=link)
        if not ok: worker_diagnostic(ledger, run_id, registration=reg)
        return result
    except BaseException as exc:
        worker_diagnostic(ledger, run_id, exception=exc)
        current = ledger.status()
        if current["runs"][run_id]["status"] == "RUNNING": ledger.finish_research(run_id, None, success=False)
        raise


def launch(ledger):
    obj = ledger.load(); policy = Policy.model_validate(obj["policy"])
    if not (obj["namespace"] == "PRODUCTION" and obj["approved"] and policy.enabled and policy.accepted_post_response_exposure):
        raise SafetyStop("USER_APPROVAL_REQUIRED")
    reviewed_implementation()
    try: resource_check(policy)
    except Exception:
        ledger.resource_pause()
        raise
    with locked(ledger.root / "controller", wait=False) as held:
        if not held: raise SafetyStop("CONTROLLER_BUSY")
        rid = ledger.start_research()
        try:
            process = subprocess.Popen([sys.executable, "-m", "tradingagents.guarded_research", "_worker",
                                        "--ledger", str(ledger.root), "--run-id", rid], cwd=ROOT,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except BaseException:
            ledger.finish_research(rid, None, success=False)
            raise
        state = ledger.status()
        remaining = min(policy.task_seconds,
                        (state["runs"][rid]["started_ms"] + policy.task_seconds * 1000 - ledger.clock()) / 1000,
                        (state["expires_ms"] - ledger.clock()) / 1000)
        try: code = process.wait(timeout=max(.001, remaining))
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
            finally:
                state = ledger.status()
                if state["runs"][rid]["status"] == "RUNNING": ledger.finish_research(rid, None, success=False)
            raise
        state = ledger.status()
        if state["runs"][rid]["status"] == "RUNNING": ledger.finish_research(rid, None, success=False)
        return {"returncode": code, "status": ledger.status()}


def reviewed_implementation():
    """A later reviewed commit is required before real launches; no self-commit."""
    for name in ("safety_budget.py", "guarded_research.py", "auto_research.py", "research_budget.py"):
        file = "tradingagents/" + name
        result = subprocess.run(["git", "show", "HEAD:" + file], cwd=ROOT, capture_output=True, timeout=5)
        if result.returncode or result.stdout.replace(b"\r\n", b"\n") != (ROOT / file).read_bytes().replace(b"\r\n", b"\n"):
            raise SafetyStop("REVIEWED_COMMIT_REQUIRED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "status", "pause", "resume", "recover-interrupted", "run", "_worker"])
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args(); ledger = Ledger(args.ledger)
    try:
        if args.command == "init":
            if not args.policy: raise ValueError("Policy required")
            reviewed_implementation()
            ledger.initialize(Policy.model_validate(read(args.policy)), approved=args.approve, namespace="PRODUCTION")
            value = {"status": "INITIALIZED_NOT_STARTED"}
        elif args.command == "run":
            value = launch(ledger)
            if value["returncode"] or value["status"]["paused"]: raise SafetyStop("RESEARCH_FAILED_OR_PAUSED")
        elif args.command == "_worker":
            if not args.run_id: raise ValueError("Run id required")
            value = worker(ledger, args.run_id)
        elif args.command == "pause": ledger.pause(); value = ledger.status()
        elif args.command == "resume":
            if ledger.load()["namespace"] == "PRODUCTION": resource_check(Policy.model_validate(ledger.load()["policy"]))
            ledger.resume(resource_rechecked=True); value = ledger.status()
        elif args.command == "recover-interrupted":
            with locked(ledger.root / "controller", wait=False) as held:
                if not held: raise SafetyStop("CONTROLLER_BUSY")
                ledger.recover_interrupted(); value = ledger.status()
        else: value = ledger.status()
        print(json.dumps(value, ensure_ascii=True, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"status": "SAFELY_STOPPED", "error_type": type(exc).__name__,
                          "reason": exc.reason if isinstance(exc, SafetyStop) else "SANITIZED_ERROR",
                          "execution_enabled": False, "simulation_admitted": False, "allow_new_risk": False}))
        raise SystemExit(2)


if __name__ == "__main__": main()
