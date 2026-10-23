"""OKX simulated feedback adapter over the native Markdown memory log.

No market fetches, execution, database, retrieval model or strategy mutations.
Outcome time, artifact availability and lesson creation are separate clocks.
"""
from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents.dataflows.files import locked
from tradingagents.llm_clients.recovery import digest
from tradingagents.llm_clients.reliability import classify_failure
from tradingagents.memory.log import TradingMemoryLog

VERSION = "okx-simulation-feedback-v1"
PROMPT_VERSION = "okx-simulation-reflection-v1"


def utc_ms():
    return time.time_ns()//1_000_000


def as_ms(value):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if not isinstance(value, str):
        raise ValueError("Explicit UTC-aware cutoff required")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Timezone required; date-only memory queries forbidden")
    result = int(moment.timestamp()*1000)
    if result <= 0:
        raise ValueError("Invalid cutoff")
    return result


def iso(ms):
    return datetime.fromtimestamp(ms/1000, UTC).isoformat(timespec="milliseconds")


class SimulationFeedback(ResearchModel):
    kind: Literal["OKX_SIMULATION_FEEDBACK"] = "OKX_SIMULATION_FEEDBACK"
    version: Literal[VERSION] = VERSION
    feedback_id: str
    candidate_id: str
    research_id: str
    snapshot_id: str
    exchange: Literal["OKX"] = "OKX"
    instrument: Literal["BTC-USDT-SWAP"] = "BTC-USDT-SWAP"
    mode: Literal["OFFLINE_SIMULATION"] = "OFFLINE_SIMULATION"
    research_status: Literal["COMPLETED", "MODEL_FAILED", "DATA_ERROR", "UNSTUDIED", "INCOMPLETE_RESEARCH"]
    decision: Literal["ACCEPT", "REJECT", "ABSTAIN", "WAIT"] | None
    judgment: str
    outcome_status: Literal["PROFIT", "LOSS", "FLAT", "UNFILLED", "WAIT", "MODEL_FAILURE", "DATA_ERROR", "UNSTUDIED"]
    baseline_trade: dict[str, Any] | None = None
    state_machine: list[dict[str, Any]]
    error_category: str
    model_version: str
    strategy_version: str
    strategy_fingerprint: str
    source_hashes: dict[str, str]
    evidence_limitations: list[str]
    research_cutoff_ms: int = Field(gt=0)
    research_recorded_at_ms: int = Field(gt=0)
    outcome_known_at_ms: int = Field(gt=0)
    source_available_at_ms: int = Field(gt=0)
    recorded_at_ms: int = Field(gt=0)
    available_at_ms: int = Field(gt=0)
    simulated: Literal[True] = True
    execution_enabled: Literal[False] = False
    allow_new_risk: Literal[False] = False
    strategy_validated: Literal[False] = False

    @model_validator(mode="after")
    def validate_link_and_time(self):
        if self.snapshot_id != self.candidate_id:
            raise ValueError("Feedback candidate/snapshot ownership mismatch")
        if self.research_status == "COMPLETED" and self.decision is None:
            raise ValueError("Completed research requires its actual decision")
        if self.research_status != "COMPLETED" and self.decision is not None:
            raise ValueError("Failure cannot become a decision or successful rejection")
        if self.research_status in {"MODEL_FAILED", "INCOMPLETE_RESEARCH"} and self.outcome_status != "MODEL_FAILURE":
            raise ValueError("Model failure is not successful risk avoidance")
        if self.research_status == "DATA_ERROR" and self.outcome_status != "DATA_ERROR":
            raise ValueError("Data failure is not a trading result")
        if self.research_recorded_at_ms < self.research_cutoff_ms or self.outcome_known_at_ms < self.research_cutoff_ms:
            raise ValueError("Result predates decision")
        if self.available_at_ms != max(self.research_recorded_at_ms, self.outcome_known_at_ms,
                                       self.source_available_at_ms, self.recorded_at_ms):
            raise ValueError("Feedback availability clock mismatch")
        if not self.source_hashes or not self.strategy_fingerprint:
            raise ValueError("Missing provenance")
        for event in self.state_machine:
            if event.get("snapshot_id") != self.candidate_id or not self.research_cutoff_ms <= event["ts"] <= self.outcome_known_at_ms:
                raise ValueError("Foreign/future lifecycle event")
        if self.baseline_trade:
            trade = self.baseline_trade
            for key in ("net_pnl", "fees_usdt", "slippage_usdt", "funding_cashflow", "contracts",
                        "base_quantity", "entry_price", "exit_price"):
                value = trade.get(key)
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                    raise ValueError("Invalid numeric simulated outcome")
            if any(trade[k] <= 0 for k in ("contracts", "base_quantity", "entry_price", "exit_price")) or any(
                trade[k] < 0 for k in ("fees_usdt", "slippage_usdt")):
                raise ValueError("Invalid simulated quantity/cost")
            if trade.get("snapshot_id") != self.candidate_id or trade.get("simulated") is not True:
                raise ValueError("Not an owned simulated trade")
            if trade["research_cutoff_ms"] != self.research_cutoff_ms or not self.research_cutoff_ms < trade["entry_ms"] <= trade["exit_ms"] <= trade["resolved_at_ms"] <= self.outcome_known_at_ms:
                raise ValueError("Trade timeline mismatch")
            if self.research_status == "COMPLETED":
                expected = "PROFIT" if trade["net_pnl"] > 0 else "LOSS" if trade["net_pnl"] < 0 else "FLAT"
                if self.outcome_status != expected:
                    raise ValueError("Outcome disagrees with baseline ledger")
        elif self.outcome_status in {"PROFIT", "LOSS", "FLAT"}:
            raise ValueError("No trade is not zero profit")
        return self


class SimulationReflection(ResearchModel):
    kind: Literal["OKX_SIMULATION_REFLECTION"] = "OKX_SIMULATION_REFLECTION"
    feedback_id: str
    feedback_hash: str
    text: str = Field(min_length=10, max_length=8000)
    prompt_version: Literal[PROMPT_VERSION] = PROMPT_VERSION
    model_version: str
    generated_at_ms: int = Field(gt=0)
    available_at_ms: int = Field(gt=0)
    usage: dict[str, Any] | None = None
    generation_mode: Literal["MOCK", "REAL_LLM"]
    status: Literal["UNVALIDATED_RESEARCH_NOTE"] = "UNVALIDATED_RESEARCH_NOTE"
    execution_enabled: Literal[False] = False
    allow_new_risk: Literal[False] = False
    strategy_validated: Literal[False] = False


class OKXSimulationMemory(TradingMemoryLog):
    """Native storage/locking/parser, isolated namespace and mandatory PIT reads."""
    def __init__(self, root):
        super().__init__({"memory_log_path": str(Path(root)/"okx_simulation"/"memory.md")})

    def _records(self):
        records = []
        for entry in super().load_entries():
            data = json.loads(entry["decision"])
            schema = SimulationFeedback if data.get("kind") == "OKX_SIMULATION_FEEDBACK" else SimulationReflection
            records.append(schema.model_validate(data))
        return records

    def _append(self, record):
        # Caller owns the native log lock. No fake stock returns/alpha fields.
        tag = f"[{iso(record.available_at_ms)} | OKX:BTC-USDT-SWAP | SIMULATION | resolved]"
        body = json.dumps(record.model_dump(mode="json"), ensure_ascii=True, allow_nan=False)
        old = self._log_path.read_text(encoding="utf-8") if self._log_path.exists() else ""
        temporary = self._log_path.with_suffix(".tmp")
        temporary.write_text(old+tag+"\n\nDECISION:\n"+body+self._SEPARATOR, encoding="utf-8")
        temporary.replace(self._log_path)  # Native outcome-update atomic pattern.

    def load_entries(self):
        raise ValueError("OKX simulation reads require load_feedback/load_reflections with as_of")

    def store_feedback(self, value):
        record = SimulationFeedback.model_validate(value)
        with locked(self._log_path):
            for old in self._records():
                if isinstance(old, SimulationFeedback) and old.feedback_id == record.feedback_id:
                    exclude = {"recorded_at_ms", "source_available_at_ms", "available_at_ms"}
                    if old.model_dump(exclude=exclude) != record.model_dump(exclude=exclude):
                        raise ValueError("Existing feedback provenance/content changed")
                    return old
            self._append(record)
        return record

    def load_feedback(self, *, as_of, include_diagnostics=False):
        cutoff = as_ms(as_of)
        with locked(self._log_path):
            return [r for r in self._records() if isinstance(r, SimulationFeedback)
                    and r.available_at_ms <= cutoff
                    and (include_diagnostics or r.research_status == "COMPLETED")]

    def load_reflections(self, *, as_of):
        cutoff = as_ms(as_of)
        with locked(self._log_path):
            records = self._records()
        feedback = {r.feedback_id: r for r in records if isinstance(r, SimulationFeedback)}
        result = []
        for r in records:
            if not isinstance(r, SimulationReflection):
                continue
            parent = feedback.get(r.feedback_id)
            if parent is None or digest(parent.model_dump(mode="json")) != r.feedback_hash:
                raise ValueError("Reflection parent provenance changed")
            if r.generated_at_ms < parent.available_at_ms or r.available_at_ms != max(r.generated_at_ms, parent.available_at_ms):
                raise ValueError("Backdated reflection")
            if r.available_at_ms <= cutoff and parent.research_status == "COMPLETED":
                result.append(r)
        return result

    def store_reflection(self, value):
        record = SimulationReflection.model_validate(value)
        with locked(self._log_path):
            records = self._records()
            parent = next((r for r in records if isinstance(r, SimulationFeedback) and r.feedback_id == record.feedback_id), None)
            if parent is None or parent.research_status != "COMPLETED" or digest(parent.model_dump(mode="json")) != record.feedback_hash:
                raise ValueError("Missing/invalid completed feedback")
            if record.generated_at_ms < parent.available_at_ms or record.available_at_ms != max(record.generated_at_ms, parent.available_at_ms):
                raise ValueError("Backdated reflection")
            for old in records:
                if isinstance(old, SimulationReflection) and old.feedback_id == record.feedback_id:
                    if old != record:
                        raise ValueError("Reflection cannot be resampled/overwritten")
                    return old
            self._append(record)
        return record

    def get_pending_entries(self):
        return []  # This adapter must never enter Yahoo/stock settlement.

    def store_decision(self, *args, **kwargs):
        raise ValueError("Use typed simulation feedback, not stock decisions")

    def get_past_context(self, ticker, n_same=5, n_cross=0, as_of=None):
        cutoff = as_ms(as_of)  # None is intentionally forbidden for OKX.
        if ticker != "BTC-USDT-SWAP" or n_cross != 0 or not 0 <= n_same <= 100:
            raise ValueError("Only isolated BTC-USDT-SWAP simulation context supported")
        reflections = self.load_reflections(as_of=cutoff)
        # A mocked lesson is demonstration evidence, never research context.
        reflections = [r for r in reflections if r.generation_mode == "REAL_LLM"]
        if not n_same or not reflections:
            return ""
        return "SIMULATION ONLY; unvalidated lessons, no strategy changes or execution authority.\n"+"\n".join(
            f"{r.feedback_id}: {r.text}" for r in sorted(reflections, key=lambda r: (r.available_at_ms, r.feedback_id))[-n_same:])


def settle_simulation_feedback(memory, reflector, *, as_of, model_version, generation_mode,
                               max_entries=1, clock=utc_ms):
    """Adapt native settlement locking and result reporting; use the audited ledger.

    Each reflection has a durable attempt marker. Ambiguous interrupted/failed
    calls are not replayed automatically. No price fetch or fabricated alpha.
    """
    from tradingagents.memory.settlement import Settlement
    from tradingagents.okx_traded_comparison import immutable
    cutoff = as_ms(as_of)
    if not 0 <= max_entries <= 20 or generation_mode not in {"MOCK", "REAL_LLM"}:
        raise ValueError("Invalid bounded reflection policy")
    if cutoff > clock():
        raise ValueError("Cannot generate feedback from a future as-of")
    result = Settlement()
    with memory.settling() as held:
        if not held:
            return result
        completed = {r.feedback_id for r in memory.load_reflections(as_of=clock())}
        pending = [r for r in memory.load_feedback(as_of=cutoff) if r.feedback_id not in completed][:max_entries]
        for record in pending:
            attempt_path = memory._log_path.parent/(record.feedback_id+".reflection_attempt.json")
            if attempt_path.exists():
                result.failed.append((record.instrument, record.feedback_id, "PREVIOUS_ATTEMPT_NOT_REPLAYED"))
                continue
            immutable(attempt_path, {"status": "STARTED", "feedback_hash": digest(record.model_dump(mode="json")),
                      "started_at_ms": clock(), "generation_mode": generation_mode, "execution_enabled": False})
            try:
                reply = reflector.reflect_on_simulation_feedback(record.model_dump(mode="json"), as_of_ms=cutoff)
                generated = clock()
                lesson = SimulationReflection(feedback_id=record.feedback_id,
                    feedback_hash=digest(record.model_dump(mode="json")), text=reply["text"],
                    model_version=model_version, generated_at_ms=generated,
                    available_at_ms=max(generated, record.available_at_ms), usage=reply.get("usage"),
                    generation_mode=generation_mode)
                memory.store_reflection(lesson)
                immutable(attempt_path.with_suffix(".result.json"), {"status": "COMPLETED", "usage": lesson.usage,
                          "execution_enabled": False})
                result.settled.append((record.instrument, record.feedback_id))
            except Exception as exc:
                failure = classify_failure(exc)
                kind = failure.kind if failure else "REFLECTION_FORMAT_OR_PROGRAM_ERROR"
                immutable(attempt_path.with_suffix(".result.json"), {"status": "FAILED",
                          "failure": failure.summary() if failure else {"kind": kind}, "usage": None,
                          "execution_enabled": False})
                result.failed.append((record.instrument, record.feedback_id, kind))
                # Do not pay for further calls after any model/format failure.
                break
    return result
