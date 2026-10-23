import copy
import json
from pathlib import Path
import pytest
from tradingagents import okx_traded_comparison as c
from tests.test_okx_agent_comparison import ledger_fixture, payload, Model


def fixture():
    inputs, original, sid = ledger_fixture()
    # Original fixture uses TRADE open as funding proxy; supplied marks match it.
    return inputs, original, sid


def frozen(tmp_path, monkeypatch):
    root = tmp_path/"experiment"; root.mkdir()
    cfg = c.research.model_config()
    p = payload()
    protocol = {"version": c.VERSION, "code": c.source_fingerprints(), "execution_enabled": False,
                "max_cases": 1, "candidate_ids": [p["candidate_id"]], "payload_hashes": [c.bt.digest(p)],
                "model_config": c.safe_config(cfg)}
    c.immutable(root/"protocol.json", protocol); c.immutable(root/"fixed.input.json", p)
    monkeypatch.setattr(c.research, "model_config", lambda: copy.deepcopy(cfg))
    return root, protocol


def test_unique_original_candidate_lifecycle_mapping():
    i, r, _ = fixture()
    ds, lineage = c.locate_candidates(i, r, expected=1)
    assert len(ds) == len(lineage) == 1
    assert lineage[0]["retest_confirmed_ms"] == r["trades"][0]["entry_ms"]
    assert "net_pnl" not in json.dumps(lineage)


@pytest.mark.parametrize("bad", ["ownership", "direction", "stop", "duplicate", "missing_event", "entry_price", "confirmation"])
def test_unreliable_linkage_blocks(bad):
    i, r, _ = fixture()
    t = r["trades"][0]
    if bad == "ownership": t["snapshot_id"] = "wrong"
    if bad == "direction": t["direction"] = "SHORT"
    if bad == "stop": t["stop"] += 1
    if bad == "duplicate": r["decisions"].append(copy.deepcopy(r["decisions"][0]))
    if bad == "missing_event": r["events"] = [e for e in r["events"] if e["type"] != "PENDING_RETEST"]
    if bad == "entry_price": next(e for e in r["events"] if e["type"] == "ENTRY")["price"] += 1
    if bad == "confirmation": t["entry_ms"] -= c.bt.HOUR
    with pytest.raises(ValueError): c.locate_candidates(i, r, expected=1)


def test_decisions_sealed_before_outcomes_and_native_workflow_cached(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    model = Model()
    manifest = c.execute(root, backend_factory=lambda cfg: (model, model))
    assert len(model.prompts) == 9
    assert manifest["scope"].startswith("Independent Agent")
    assert not (root/"analysis.json").exists()
    case = c.verified_cases(root, protocol)[0]
    assert case["decision"] == "ACCEPT" and set(case["nodes"]) == c.research.REQUIRED_NODES
    def forbidden(*a): raise AssertionError("Cached experiment must not create a model")
    assert c.execute(root, backend_factory=forbidden) == manifest
    assert len(model.prompts) == 9


def test_no_outcome_open_without_seal(tmp_path, monkeypatch):
    root, _ = frozen(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError, match="sealed_decisions"):
        c.analyze(root, tmp_path/"outcomes_must_not_be_opened")


def test_modified_sealed_decision_blocked(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    c.execute(root, backend_factory=lambda cfg: (Model(), Model()))
    path = root/"fixed.result.json"; case = c.read(path); case["elapsed_seconds"] += 1
    path.write_text(json.dumps(case), encoding="utf-8")
    with pytest.raises(ValueError, match="modified"): c.verified_cases(root, protocol)


def test_interrupted_case_not_retried_or_counted_as_reject(tmp_path, monkeypatch):
    root, _ = frozen(tmp_path, monkeypatch)
    c.immutable(root/"fixed.started.json", {"interrupted": True})
    def forbidden(*a): raise AssertionError("Ambiguous interrupted call must not replay")
    manifest = c.execute(root, backend_factory=forbidden)
    assert manifest["records"][0]["status"] == "INTERRUPTED_NOT_RETRIED"
    assert manifest["records"][0]["decision"] is None


@pytest.mark.parametrize("bad", ["code", "input", "models"])
def test_freeze_change_prevents_model_calls(tmp_path, monkeypatch, bad):
    root, protocol = frozen(tmp_path, monkeypatch)
    if bad == "code": protocol["code"] = {}; (root/"protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    if bad == "input": (root/"fixed.input.json").write_text("{}", encoding="utf-8")
    if bad == "models": monkeypatch.setattr(c.research, "model_config", lambda: {"quick_think_llm": "different"})
    def forbidden(*a): raise AssertionError("No LLM on changed protocol")
    with pytest.raises(ValueError): c.execute(root, backend_factory=forbidden)


@pytest.mark.parametrize("action,status,suppressed", [("ACCEPT", "COMPLETED", False), ("REJECT", "COMPLETED", True),
    ("ABSTAIN", "COMPLETED", False), (None, "MODEL_FAILED", False)])
def test_unknowns_passthrough_not_successful_avoidance(action, status, suppressed):
    i, r, sid = fixture(); saved = copy.deepcopy(r["decisions"])
    result = c.filtered_decisions(r["decisions"], [{"candidate_id": sid, "status": status, "decision": action}])
    assert (result[0]["candidate"] is None) == suppressed
    assert r["decisions"] == saved


def test_continuous_replay_preserves_A_and_reenables_later_candidates():
    from tests.test_okx_backtest import START, CONFIG, INFO, bars, funding, decision
    rows, fs = bars(24), funding()
    first = decision(); second = copy.deepcopy(first)
    second.update(cutoff_ms=START+4*c.bt.HOUR, evidence_latest_ms=START+4*c.bt.HOUR, snapshot_id="later_unstudied")
    ds = [first, second]
    original = c.bt.simulate(rows, fs, ds, INFO, CONFIG, START, START+24*c.bt.HOUR)
    original["decisions"] = ds
    data = {"candles": {"1h": [[r["ts"], r["open"], r["high"], r["low"], r["close"], 0, 0, 0, "1"] for r in rows]},
        "funding": fs, "instrument": INFO, "mark_records": [{"ts": r["ts"], "open": r["open"]} for r in rows]}
    inputs = {"data": data, "config": CONFIG.model_dump(), "start": START, "end": START+24*c.bt.HOUR}
    A = c.continuous_replay(inputs, ds)
    assert A["metrics"] == original["metrics"] and A["equity_curve"] == original["equity_curve"]
    assert any(e["type"] == "DECISION_SKIPPED" and e["snapshot_id"] == "later_unstudied" for e in A["events"])
    B = c.continuous_replay(inputs, c.filtered_decisions(ds, [{"candidate_id": "fixed", "status": "COMPLETED", "decision": "REJECT"}]))
    assert len(B["trades"]) == 1 and B["trades"][0]["snapshot_id"] == "later_unstudied"
    assert B["metrics"]["fees_usdt"] > 0 and B["arithmetic_audit"]["status"] == "PASSED"
    assert B["metrics"]["final_equity"] == pytest.approx(CONFIG.initial_equity+B["trades"][0]["net_pnl"])


def test_immutable_atomic_output(tmp_path):
    path = tmp_path/"result.json"
    c.immutable(path, {"same": True}); c.immutable(path, {"same": True})
    with pytest.raises(ValueError): c.immutable(path, {"same": False})


def test_membership_and_order_do_not_depend_on_pnl():
    inputs, original, _ = fixture()
    a, links = c.locate_candidates(inputs, original, expected=1)
    original["trades"][0]["net_pnl"] *= -1000000
    b, links_again = c.locate_candidates(inputs, original, expected=1)
    assert [d["snapshot_id"] for d in a] == [d["snapshot_id"] for d in b]
    assert links == links_again


def fourteen(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    ps = [{**payload(), "candidate_id": f"case-{n:02}"} for n in range(14)]
    protocol.update(max_cases=14, candidate_ids=[p["candidate_id"] for p in ps], payload_hashes=[c.bt.digest(p) for p in ps])
    (root/"protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    for p in ps: c.immutable(root/(p["candidate_id"]+".input.json"), p)
    return root, protocol


def test_parallel_fourteen_native_cases_isolated_bounded_and_cached(tmp_path, monkeypatch):
    import re
    from langchain_core.messages import AIMessage
    root, protocol = fourteen(tmp_path, monkeypatch)
    original_read = c.read
    def guarded(path):
        assert Path(path).name not in {"inputs.json", "result.json", "lineage.json", "candidate_catalog.json", "analysis.json"}
        return original_read(path)
    monkeypatch.setattr(c, "read", guarded)
    class DynamicModel:
        def invoke(self, prompt):
            sid = re.search(r'"candidate_id":\s*"([^"]+)"', prompt).group(1)
            return AIMessage(content="Historical evidence only for "+sid)
        def with_structured_output(self, schema, include_raw=False):
            owner = self
            class Bound:
                def invoke(self, prompt):
                    sid = re.search(r'"candidate_id":\s*"([^"]+)"', prompt).group(1)
                    raw = owner.invoke(prompt)
                    value = schema.model_validate({"candidate_id": sid, "decision": "ACCEPT",
                        "evidence_ids": ["candidate", "1h"], "rationale": "Only the provided historical boundary supports conditional observation."})
                    return {"raw": raw, "parsed": value, "parsing_error": None} if include_raw else value
            return Bound()
    model = DynamicModel()
    sealed = c.execute(root, backend_factory=lambda cfg: (model, model))
    cases = c.verified_cases(root, protocol)
    assert len(cases) == 14 and sum(len(case["calls"]) for case in cases) == 126
    assert all(case["status"] == "COMPLETED" for case in cases)
    for case in cases:
        for call in case["calls"]:
            assert all(sid not in call["input"] for sid in protocol["candidate_ids"] if sid != case["candidate_id"])
    def forbidden(*a): raise AssertionError("Cannot replay cached native calls")
    assert c.execute(root, backend_factory=forbidden) == sealed


def test_rate_limit_stops_new_case_requests_not_counted_as_rejection(tmp_path, monkeypatch):
    from tradingagents.llm_clients.reliability import LLMUnavailable
    root, protocol = fourteen(tmp_path, monkeypatch)
    def limited(*a): raise LLMUnavailable("RATE_LIMIT")
    c.execute(root, backend_factory=limited)
    cases = c.verified_cases(root, protocol)
    assert all(case["decision"] is None for case in cases)
    assert sum(case["status"] == "MODEL_INITIALIZATION_OR_WORKFLOW_FAILED" for case in cases) <= 2
    assert sum(case["status"] == "NOT_STARTED_SERVICE_LIMIT" for case in cases) >= 12


def test_sealed_analysis_full_path_and_continuous_outputs(tmp_path, monkeypatch):
    root, protocol = frozen(tmp_path, monkeypatch)
    inputs, original, _ = fixture()
    # D.3 uses the MARK-open funding source, unlike the legacy fixture's
    # default TRADE-open label. Preserve the actual comparison's event source.
    original = c.continuous_replay(inputs, original["decisions"])
    lineage = [{"candidate_id": "fixed", "checked": True}]
    protocol.update(source_digest=c.bt.digest(inputs), baseline_result_digest=c.bt.digest(original),
                    lineage_hash=c.bt.digest(lineage), scope="TEST_RETROSPECTIVE", historical_limitations=[], LLM_hindsight="TEST")
    (root/"protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    baseline = tmp_path/"baseline"; baseline.mkdir()
    c.immutable(baseline/"inputs.json", inputs); c.immutable(baseline/"result.json", original)
    c.execute(root, backend_factory=lambda cfg: (Model(), Model()))
    monkeypatch.setattr(c.costs, "audit", lambda *a: {})
    monkeypatch.setattr(c, "locate_candidates", lambda *a: (original["decisions"], lineage))
    result = c.analyze(root, baseline)
    assert result["A_metrics"] == result["B_hybrid_continuous_metrics"] == original["metrics"]
    assert result["decision_counts"]["ACCEPT"] == 1
    assert (root/"A_continuous.json").exists() and (root/"B_equity.csv").exists()
    assert c.analyze(root, baseline) == result


@pytest.mark.parametrize("numerical_change", [False, True])
def test_analysis_import_repair_only_protected_AST_unchanged(tmp_path, numerical_change):
    import hashlib
    source = Path(c.__file__).read_text(encoding="utf-8")
    old = source.replace("import ast\n", "").replace("from collections import Counter\n", "")
    if numerical_change:
        old = old.replace("max_window_days=731", "max_window_days=730")
    name = Path(c.__file__).name
    protocol = {"code": c.source_fingerprints()}
    protocol["code"][name] = hashlib.sha256(old.encode()).hexdigest()
    c.immutable(tmp_path/"source_snapshot.json", {name: old})
    c.immutable(tmp_path/"sealed_decisions.json", {"unchanged_decisions": True})
    if numerical_change:
        with pytest.raises(ValueError, match="protected algorithm"): c._analysis_source_provenance(tmp_path, protocol)
    else:
        c._analysis_source_provenance(tmp_path, protocol)
        assert c.read(tmp_path/"analysis_source_amendment.json")["research_and_numerical_AST"] == "UNCHANGED"
