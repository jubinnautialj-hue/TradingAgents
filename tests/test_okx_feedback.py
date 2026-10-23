"""Fixed simulation feedback, native memory isolation and no future lessons."""
import copy
import json

import pytest
from langchain_core.messages import AIMessage

from tests.test_okx_agent_comparison import Model, payload
from tests.test_okx_traded_comparison import fixture
from tradingagents import okx_agent_comparison as research, okx_feedback as f
from tradingagents.memory import TradingMemoryLog
from tradingagents.memory.reflection import Reflector
from tradingagents.memory.okx_simulation import (OKXSimulationMemory, SimulationFeedback,
    SimulationReflection, as_ms, settle_simulation_feedback)
from tradingagents.llm_clients.recovery import digest
from tradingagents.llm_clients.reliability import LLMUnavailable


def record(*, decision="ACCEPT", failed=False, unfilled=False):
    inputs, ledger, sid = fixture()
    candidate = next(d for d in ledger["decisions"] if d["snapshot_id"] == sid)
    p = payload()
    p.update(cutoff_ms=candidate["cutoff_ms"], cutoff_utc=f.bt.iso(candidate["cutoff_ms"]))
    case = research.run_case(p, Model(decision), Model(decision), {})
    for call in case["calls"]:
        call.update(started_at_utc=p["cutoff_utc"], elapsed_seconds=0)
    trade = ledger["trades"][0]
    if failed:
        case.update(status="MODEL_FAILED", decision=None, failure={"kind": "MODEL_SERVICE_ERROR"})
        case.pop("choice", None)
    events = [e for e in ledger["events"] if e.get("snapshot_id") == sid]
    if unfilled:
        trade = None
        events = [{"snapshot_id": sid, "ts": inputs["end"], "type": "EXPIRED"}]
    now = inputs["end"]+1000
    return f.build_feedback(case, candidate, trade, events, source_hashes={"fixture": "source-v1"},
        strategy_version=ledger["strategy_version"], strategy_fingerprint="frozen-strategy",
        model_version="fixed-test-model", source_available_at_ms=now, recorded_at_ms=now,
        limitations=["Test-only synthetic prices and costs"], window_end_ms=inputs["end"])


class LessonModel:
    def __init__(self, fail=None, raw=None): self.calls, self.fail, self.raw, self.messages = 0, fail, raw, []
    def invoke(self, messages):
        self.calls += 1
        self.messages.append(messages)
        if self.fail: raise self.fail
        return self.raw or AIMessage(content="This simulated outcome cannot establish an analysis error. Preserve frozen rules and audit the evidence assumptions.",
                                     usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30})


def settle(memory, backend, now, mode="MOCK"):
    return settle_simulation_feedback(memory, Reflector(backend), as_of=now,
        model_version="fixed-test-model", generation_mode=mode, clock=lambda: now)


@pytest.mark.parametrize("decision", ["ACCEPT", "REJECT", "ABSTAIN"])
def test_decision_and_baseline_ownership_saved(decision):
    r = record(decision=decision)
    assert r.decision == decision and r.baseline_trade["snapshot_id"] == r.candidate_id
    assert r.strategy_validated is False and r.execution_enabled is False
    if decision == "REJECT": assert "COUNTERFACTUAL" in r.error_category
    if decision == "ABSTAIN": assert "NOT_SUCCESSFUL" in r.error_category


def test_model_failure_is_not_profitable_filter_and_not_reflected(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record(failed=True))
    assert r.outcome_status == "MODEL_FAILURE" and r.decision is None
    assert memory.load_feedback(as_of=r.available_at_ms) == []
    assert len(memory.load_feedback(as_of=r.available_at_ms, include_diagnostics=True)) == 1
    model = LessonModel()
    assert settle(memory, model, r.available_at_ms).settled == []
    assert model.calls == 0


def test_unfilled_is_not_zero_pnl():
    r = record(unfilled=True)
    assert r.outcome_status == "UNFILLED" and r.baseline_trade is None
    with pytest.raises(ValueError): SimulationFeedback.model_validate({**r.model_dump(), "outcome_status": "FLAT"})


def test_wait_and_data_error_distinct(tmp_path):
    r = record(unfilled=True).model_dump()
    wait = SimulationFeedback.model_validate({**r, "decision": "WAIT", "outcome_status": "WAIT"})
    assert wait.baseline_trade is None
    error = SimulationFeedback.model_validate({**r, "research_status": "DATA_ERROR", "decision": None,
                                             "outcome_status": "DATA_ERROR"})
    assert error.outcome_status != wait.outcome_status
    with pytest.raises(ValueError): SimulationFeedback.model_validate({**error.model_dump(), "decision": "REJECT"})


def test_feedback_exact_intraday_cutoff_and_actual_creation_gated(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    assert memory.load_feedback(as_of=r.research_cutoff_ms) == []
    assert memory.load_feedback(as_of=r.outcome_known_at_ms) == []
    assert memory.load_feedback(as_of=r.available_at_ms-1) == []
    assert memory.load_feedback(as_of=r.available_at_ms) == [r]
    assert memory.get_past_context(r.instrument, as_of=r.research_cutoff_ms) == ""


@pytest.mark.parametrize("cutoff", [None, "2026-01-01", "2026-01-01T12:00:00", 0, True])
def test_ambiguous_cutoff_forbidden(cutoff):
    with pytest.raises(ValueError): as_ms(cutoff)


def test_timezones_normalized_to_same_utc_instant():
    assert as_ms("2026-01-01T16:00:00+08:00") == as_ms("2026-01-01T08:00:00Z")


def test_reflection_not_visible_before_creation_and_mock_not_injected(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    generated = r.available_at_ms+1000
    model = LessonModel()
    assert settle(memory, model, generated).settled
    assert memory.load_reflections(as_of=generated-1) == []
    assert len(memory.load_reflections(as_of=generated)) == 1
    assert memory.get_past_context(r.instrument, as_of=generated) == ""  # Mock is demonstration only.
    assert len(model.messages) == 1
    assert json.loads(model.messages[0][-1][1])["baseline_trade"]["net_pnl"] == r.baseline_trade["net_pnl"]


def test_complete_feedback_and_real_labelled_lesson_context(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    now = r.available_at_ms+2000
    assert settle(memory, LessonModel(), now, mode="REAL_LLM").settled
    context = memory.get_past_context(r.instrument, as_of=now)
    assert "SIMULATION ONLY" in context and "unvalidated" in context
    assert memory.get_past_context(r.instrument, as_of=now-1) == ""


def test_feedback_and_reflection_idempotent_no_repeat_model(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    later = {**r.model_dump(), "recorded_at_ms": r.available_at_ms+5000,
             "source_available_at_ms": r.available_at_ms+5000, "available_at_ms": r.available_at_ms+5000}
    assert memory.store_feedback(later) == r
    model = LessonModel()
    settle(memory, model, r.available_at_ms+1000)
    before = memory._log_path.read_bytes()
    assert not settle(memory, model, r.available_at_ms+2000).settled
    assert model.calls == 1 and memory._log_path.read_bytes() == before


def test_parent_or_candidate_mismatch_and_backdating_blocked(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    bad = r.model_dump()
    bad["baseline_trade"]["snapshot_id"] = "foreign"
    with pytest.raises(ValueError): SimulationFeedback.model_validate(bad)
    lesson = dict(feedback_id=r.feedback_id, feedback_hash=digest(r.model_dump(mode="json")),
                  text="unvalidated fixed lesson", model_version="mock", generation_mode="MOCK",
                  generated_at_ms=r.available_at_ms-1, available_at_ms=r.available_at_ms)
    with pytest.raises(ValueError): memory.store_reflection(lesson)
    lesson.update(generated_at_ms=r.available_at_ms, feedback_hash="wrong")
    with pytest.raises(ValueError): memory.store_reflection(lesson)


def test_source_mutation_is_not_duplicate_success(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    changed = {**r.model_dump(), "judgment": "Changed judgment"}
    with pytest.raises(ValueError, match="changed"): memory.store_feedback(changed)


def test_stocks_remain_in_separate_log_and_no_yahoo_settlement(tmp_path, monkeypatch):
    stock = TradingMemoryLog({"memory_log_path": str(tmp_path/"stock.md")})
    stock.store_decision("AAPL", "2026-01-01", "Rating: Buy\nStock thesis")
    before = (tmp_path/"stock.md").read_bytes()
    memory = OKXSimulationMemory(tmp_path)
    memory.store_feedback(record())
    assert memory.get_pending_entries() == []
    def forbidden(*a, **k): raise AssertionError("Yahoo must not be accessed")
    monkeypatch.setattr("tradingagents.memory.settlement.fetch_returns", forbidden)
    from tradingagents.memory.settlement import settle_all_pending
    assert settle_all_pending(memory, Reflector(LessonModel()), {}).settled == []
    assert (tmp_path/"stock.md").read_bytes() == before
    assert stock.get_pending_entries()[0]["ticker"] == "AAPL"
    with pytest.raises(ValueError): memory.load_entries()
    with pytest.raises(ValueError): memory.store_decision("BTC-USDT-SWAP", "2026-01-01", "BUY")


@pytest.mark.parametrize("field", ["execution_enabled", "allow_new_risk", "strategy_validated"])
def test_model_cannot_promote_permissions_or_validate_strategy(field):
    with pytest.raises(ValueError): SimulationFeedback.model_validate({**record().model_dump(), field: True})


def test_reflection_failure_persisted_not_retried_or_used(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    model = LessonModel(fail=LLMUnavailable("STREAM_INTERRUPTED", partial=True))
    first = settle(memory, model, r.available_at_ms+1000)
    assert first.failed and not first.settled
    assert memory.load_reflections(as_of=r.available_at_ms+1000) == []
    assert settle(memory, model, r.available_at_ms+2000).failed[0][2] == "PREVIOUS_ATTEMPT_NOT_REPLAYED"
    assert model.calls == 1


@pytest.mark.parametrize("raw", [AIMessage(content="partial", response_metadata={"status": "incomplete"}),
                                AIMessage(content="partial", response_metadata={"finish_reason": "length"}),
                                AIMessage(content="")])
def test_incomplete_reflection_never_becomes_complete(tmp_path, raw):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    assert settle(memory, LessonModel(raw=raw), r.available_at_ms+1000).failed
    assert memory.load_reflections(as_of=r.available_at_ms+1000) == []


def test_future_settlement_and_unavailable_input_blocked(tmp_path):
    memory = OKXSimulationMemory(tmp_path)
    r = memory.store_feedback(record())
    with pytest.raises(ValueError):
        settle_simulation_feedback(memory, Reflector(LessonModel()), as_of=r.available_at_ms+1,
                                   model_version="mock", generation_mode="MOCK", clock=lambda: r.available_at_ms)
    model = LessonModel()
    with pytest.raises(ValueError):
        Reflector(model).reflect_on_simulation_feedback(r.model_dump(), as_of_ms=r.available_at_ms-1)
    assert model.calls == 0


@pytest.mark.parametrize("field,value", [("net_pnl", float("nan")), ("net_pnl", float("inf")),
                                        ("fees_usdt", -1), ("contracts", 0)])
def test_invalid_outcome_numbers_rejected(field, value):
    data = record().model_dump()
    data["baseline_trade"][field] = value
    with pytest.raises(ValueError): SimulationFeedback.model_validate(data)


def test_missing_terminal_event_cannot_be_called_unfilled():
    inputs, ledger, sid = fixture()
    p = payload()
    p["cutoff_utc"] = f.bt.iso(inputs["start"])
    case = research.run_case(p, Model(), Model(), {})
    with pytest.raises(ValueError, match="terminal"):
        f.build_feedback(case, ledger["decisions"][0], None, [], source_hashes={"fixture": "fixed"},
            strategy_version="fixed", strategy_fingerprint="fixed", model_version="mock",
            source_available_at_ms=inputs["end"], recorded_at_ms=inputs["end"])


def test_wrong_ledger_blocked_before_cost_or_network(tmp_path, monkeypatch):
    from tests.test_okx_traded_comparison import frozen
    source, protocol = frozen(tmp_path, monkeypatch)
    from tradingagents import okx_traded_comparison as experiment
    experiment.execute(source, backend_factory=lambda _: (Model(), Model()))
    protocol.update(source_digest="wrong", baseline_result_digest="wrong")
    (source/"protocol.json").write_text(json.dumps(protocol))
    seal = experiment.read(source/"sealed_decisions.json")
    seal["protocol_hash"] = f.bt.digest(protocol)
    (source/"sealed_decisions.json").write_text(json.dumps(seal))
    run = tmp_path/"run"; run.mkdir()
    (run/"inputs.json").write_text("{}")
    (run/"result.json").write_text("{}")
    with pytest.raises(ValueError, match="Wrong baseline"):
        f.import_experiment(source, run, OKXSimulationMemory(tmp_path/"feedback"))


def test_outcome_cannot_be_known_before_exit():
    data = record().model_dump()
    data["baseline_trade"]["exit_ms"] = data["outcome_known_at_ms"]+1
    with pytest.raises(ValueError, match="timeline"):
        SimulationFeedback.model_validate(data)


def test_reflector_direct_future_cutoff_is_blocked():
    from tradingagents.memory.okx_simulation import utc_ms
    model = LessonModel()
    with pytest.raises(ValueError):
        Reflector(model).reflect_on_simulation_feedback(record().model_dump(), as_of_ms=utc_ms()+3600000)
    assert model.calls == 0
