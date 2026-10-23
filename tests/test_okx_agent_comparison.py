import copy
import json
import pytest
import httpx
from langchain_core.messages import AIMessage
from tradingagents import okx_agent_comparison as c
from tradingagents.agents.candidate_filter import CandidateFilterChoice, validate_choice
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.conditional_logic import ConditionalLogic


def payload():
    return {"candidate_id": "fixed", "cutoff_ms": 1704067200000,
            "cutoff_utc": "2024-01-01T00:00:00+00:00", "candidate": {"direction": "LONG", "entry": 100},
            "execution_enabled": False}


class Model:
    def __init__(self, decision="ACCEPT", fail=None, extra=None):
        self.decision, self.fail, self.extra = decision, fail, extra
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        return AIMessage(content="Evidence-grounded historical assessment; no future facts.",
                         usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    def with_structured_output(self, schema, include_raw=False):
        model = self
        class Bound:
            def invoke(self, prompt):
                raw = model.invoke(prompt)
                value = {"candidate_id": "fixed", "decision": model.decision,
                         "evidence_ids": ["candidate", "1h"], "rationale": "The supplied one-hour structure supports observation of the original trigger."}
                if model.extra: value.update(model.extra)
                parsed = schema.model_validate(value)
                return {"raw": raw, "parsed": parsed, "parsing_error": None} if include_raw else parsed
        return Bound()


@pytest.mark.parametrize("decision", ["ACCEPT", "REJECT", "ABSTAIN"])
def test_native_nine_roles_no_tools_memory_or_candidate_changes(decision, monkeypatch):
    def forbidden(*a, **k): raise AssertionError("Network/Memory forbidden")
    monkeypatch.setattr("tradingagents.dataflows.vendors.okx.OKXPublicClient.get", forbidden)
    from tradingagents.memory import TradingMemoryLog
    monkeypatch.setattr(TradingMemoryLog, "get_past_context", forbidden)
    p = payload(); before = copy.deepcopy(p); m = Model(decision)
    result = c.run_case(p, m, m, {})
    assert result["status"] == "COMPLETED", result
    assert result["decision"] == decision and len(result["calls"]) == 9
    assert set(result["nodes"]) == c.REQUIRED_NODES
    assert result["execution_enabled"] is False and not result["allow_new_risk"]
    assert before == p
    assert all("Use ONLY" in prompt and "No external knowledge" in prompt for prompt in m.prompts)


@pytest.mark.parametrize("fail", [httpx.ReadTimeout("secret must not be logged"), httpx.RemoteProtocolError("partial secret")])
def test_failure_not_abstention_or_rejection_and_no_retry(fail):
    m = Model(fail=fail)
    result = c.run_case(payload(), m, m, {})
    assert result["status"] == "MODEL_FAILED" and result["decision"] is None
    assert len(m.prompts) == 1 and len(result["calls"]) == 1
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("extra", [{"entry_price": 50}, {"direction": "SHORT"}, {"contracts": 9}])
def test_model_cannot_change_levels_direction_quantity(extra):
    m = Model(extra=extra)
    result = c.run_case(payload(), m, m, {})
    assert result["status"] == "FORMAT_OR_WORKFLOW_ERROR" and result["decision"] is None


@pytest.mark.parametrize("value", [
    {"candidate_id": "wrong", "decision": "ACCEPT", "evidence_ids": ["candidate", "1h"], "rationale": "sufficiently long evidence"},
    {"candidate_id": "fixed", "decision": "BUY", "evidence_ids": ["candidate"], "rationale": "sufficiently long evidence"},
    {"candidate_id": "fixed", "decision": "ACCEPT", "evidence_ids": ["future_pnl"], "rationale": "sufficiently long evidence"},
    {"candidate_id": "fixed", "decision": "ACCEPT", "evidence_ids": ["candidate"], "rationale": "sufficiently long evidence"},
])
def test_schema_and_evidence_fail_closed(value):
    with pytest.raises(ValueError): validate_choice(value, "fixed")


def test_sample_all_candidates_independent_of_outcomes():
    ds = [{"cutoff_ms": i, "candidate": {"direction": "LONG"}, "outcome": i*10} for i in range(9)]
    assert [d["cutoff_ms"] for d in c.sample_candidates(ds)] == [0, 4, 8]
    ds.reverse()
    for d in ds: d["outcome"] = -999
    assert [d["cutoff_ms"] for d in c.sample_candidates(ds)] == [0, 4, 8]


def test_no_tools_binding_or_live_memory_mode():
    wrapped = c.AuditedModel(Model(), [], {"calls": 0, "deadline": 999999999}, "quick")
    with pytest.raises(ValueError): wrapped.bind_tools([])
    with pytest.raises(ValueError):
        GraphSetup(Model(), Model(), ConditionalLogic(), 1, okx_mode=True).setup_graph(["market"], historical_filter=True)
    with pytest.raises(ValueError):
        GraphSetup(Model(), Model(), ConditionalLogic(), 1).setup_graph(["market"], memory_node=lambda s: {}, historical_filter=True)


def test_budget_blocks_tenth_request():
    wrapped = c.AuditedModel(Model(), [], {"calls": 9, "deadline": 999999999}, "quick")
    with pytest.raises(RuntimeError, match="BUDGET"): wrapped.invoke("test")


def test_historical_input_ignores_funding_outcome_and_current_snapshots(monkeypatch):
    decision = {"cutoff_ms": 4*c.bt.HOUR, "snapshot_id": "fixed", "candidate": {"direction": "LONG"},
                "diagnostic": {"evidence": {}, "outcome": "FORBIDDEN_FUTURE_PNL"}}
    regenerated = {**decision, "diagnostic": {"evidence": {}}}
    seen = []
    def regenerate(data, *a, **k):
        seen.append(data)
        assert set(data) == {"symbol", "server_ms", "instrument", "candles"}
        assert all(int(r[0])+c.bt.okx.BARS[p][1] <= decision["cutoff_ms"] for p, rows in data["candles"].items() for r in rows)
        return [regenerated]
    monkeypatch.setattr(c.bt, "regenerate_decisions", regenerate)
    monkeypatch.setattr(c.bt, "historical_bundle", lambda *a: {"timeframe_analysis": {p: {} for p in ("1h", "4h", "1d")},
        "timeframes": {p: {"latest_close_ms": 0, "status": "VALID"} for p in ("1h", "4h", "1d")}})
    data = {"symbol": c.bt.SYMBOL, "instrument": {}, "funding": "FORBIDDEN_FUTURE_RATE",
            "candles": {p: [[0, 1], [10*c.bt.HOUR, 999]] for p in ("1h", "4h", "1d")},
            "derivatives": "FORBIDDEN_CURRENT_BOOK", "memory": "FORBIDDEN_MEMORY"}
    result = c.case_input(data, decision, c.continuous.fixed_config())
    assert "FORBIDDEN" not in json.dumps(result)
    assert "funding" not in seen[0]
    result_again = c.case_input(data, decision, c.continuous.fixed_config())
    assert result_again == result


def test_retrieval_clock_snapshot_ids_cannot_change_historical_input(monkeypatch):
    decision = {"cutoff_ms": 4*c.bt.HOUR, "snapshot_id": "stable", "candidate": {"direction": "LONG"}, "diagnostic": {}}
    monkeypatch.setattr(c.bt, "regenerate_decisions", lambda *a, **k: [decision])
    counter = [0]
    def bundle(*args):
        counter[0] += 1
        return {"timeframe_analysis": {p: {"snapshot_id": str(counter[0])} for p in ("1h", "4h", "1d")},
                "timeframes": {p: {"latest_close_ms": 0, "status": "VALID"} for p in ("1h", "4h", "1d")}}
    monkeypatch.setattr(c.bt, "historical_bundle", bundle)
    data = {"symbol": c.bt.SYMBOL, "instrument": {}, "candles": {p: [] for p in ("1h", "4h", "1d")}}
    first = c.case_input(data, decision, c.continuous.fixed_config())
    second = c.case_input(data, decision, c.continuous.fixed_config())
    assert first == second and counter[0] == 2


def test_future_holdout_rejected_before_reconstruction():
    with pytest.raises(ValueError, match="holdout"):
        c.case_input({}, {"cutoff_ms": c.continuous.DEVELOPMENT_END}, c.continuous.fixed_config())


def ledger_fixture():
    from tests.test_okx_backtest import START, CONFIG, INFO, bars, funding, decision
    rows, fs, ds = bars(24), funding(), [decision()]
    end = START+24*c.bt.HOUR
    original = c.bt.simulate(rows, fs, ds, INFO, CONFIG, START, end)
    original["decisions"] = ds
    data = {"candles": {"1h": [[r["ts"], r["open"], r["high"], r["low"], r["close"], 0, 0, 0, "1"] for r in rows]},
            "funding": fs, "instrument": INFO, "funding_evidence": None,
            "mark_records": [{"ts": r["ts"], "open": r["open"]} for r in rows]}
    return {"start": START, "end": end, "data": data, "config": CONFIG.model_dump()}, original, ds[0]["snapshot_id"]


@pytest.mark.parametrize("decision,status", [("ACCEPT", "COMPLETED"), ("REJECT", "COMPLETED"),
                                           ("ABSTAIN", "COMPLETED"), (None, "MODEL_FAILED")])
def test_paired_accounting_distinguishes_abstention_failure_and_rejection(decision, status):
    inputs, original, sid = ledger_fixture()
    saved = copy.deepcopy(original)
    result = c.comparison(inputs, original, [{"candidate_id": sid, "cutoff_utc": "test", "status": status,
                                             "decision": decision, "calls": [1]}])
    assert result["B_all"]["metrics"] is None
    assert result["groups"]["A_studied"]["trades"] == original["trades"]
    assert original == saved
    if decision == "ACCEPT":
        assert result["groups"]["B_accepted_only"]["trades"] == original["trades"]
    else:
        assert not result["groups"]["B_accepted_only"]["trades"]
    if decision in {None, "ABSTAIN"}:
        assert result["cases"][0]["interpretation"] == "NOT_A_SUCCESSFUL_REJECTION"
        assert not result["groups"]["A_decisive_only"]["trades"]
    if decision == "REJECT":
        assert result["groups"]["A_decisive_only"]["trades"] == original["trades"]


def test_all_opportunities_accepted_reproduce_equity_and_costs():
    inputs, original, sid = ledger_fixture()
    result = c.paired_ledger(inputs, original, {sid})
    assert result["metrics"] == original["metrics"]
    assert result["equity_curve"] == original["equity_curve"]
    assert c.paired_ledger(inputs, original, set())["metrics"]["trades"] == 0


def test_unused_candidate_not_promoted_to_replacement_trade():
    inputs, original, _ = ledger_fixture()
    result = c.paired_ledger(inputs, original, {"originally_skipped_or_expired"})
    assert result["trades"] == []


def test_offline_resume_never_creates_model_or_reads_model_preferences(tmp_path, monkeypatch, capsys):
    import sys
    source, experiment = tmp_path/"source", tmp_path/"experiment"
    source.mkdir(); experiment.mkdir()
    inputs, original = {"dataset": "historical"}, {"baseline": "unchanged"}
    c.write_new(source/"inputs.json", inputs); c.write_new(source/"result.json", original)
    p = payload()
    c.write_new(experiment/"fixed.input.json", p)
    c.write_new(experiment/"protocol.json", {"source_digest": c.bt.digest(inputs), "baseline_result_digest": c.bt.digest(original),
        "candidate_ids": ["fixed"], "payload_hashes": [c.bt.digest(p)], "version": "original"})
    c.write_new(experiment/"comparison.json", {"execution_enabled": False, "decision_counts": {"ACCEPT": 1}})
    def forbidden(*a, **k): raise AssertionError("Offline resume must not authenticate or call models")
    monkeypatch.setattr(c.costs, "audit", lambda *a: {})
    monkeypatch.setattr(c, "create_tier_client", forbidden)
    monkeypatch.setattr(c, "model_config", forbidden)
    monkeypatch.setattr(sys, "argv", ["comparison", "--run", str(source), "--resume-experiment", str(experiment)])
    c.main()
    assert json.loads(capsys.readouterr().out)["new_model_calls"] == 0
