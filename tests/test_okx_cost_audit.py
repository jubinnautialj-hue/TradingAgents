import copy
import math
import json
import pytest
from tradingagents import okx_cost_audit as audit, okx_backtest as bt


def trade(direction="LONG"):
    return {"direction": direction, "base_quantity": .01, "entry_reference": 10000.,
            "exit_reference": 11000. if direction == "LONG" else 9000.,
            "stop": 9500. if direction == "LONG" else 10500.,
            "target": 12000. if direction == "LONG" else 8000.,
            "funding_cashflow": -.02, "entry_ms": 0, "exit_ms": 8*bt.HOUR, "exit_reason": "MAX_HOLDING"}


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_cost_ordering_and_fixed_inputs(direction):
    t = trade(direction); saved = copy.deepcopy(t)
    values = [audit.reprice(t, ".1", *audit.SCENARIOS[n]) for n in ("lower", "base", "higher")]
    assert values[0]["net_pnl"] > values[1]["net_pnl"] > values[2]["net_pnl"]
    assert all(v["entry_geometry_preserved"] for v in values)
    assert all(v["funding_cashflow"] == -.02 for v in values)
    assert t == saved


def test_slippage_embedded_once_and_fee_both_legs():
    r = audit.reprice(trade(), ".1", 5, 2)
    assert r["entry_price"] == 10002
    assert r["exit_price"] == 10997.8
    assert r["fees_usdt"] == pytest.approx(.01*(10002+10997.8)*.0005)
    assert r["net_pnl"] == pytest.approx(10-r["slippage_usdt"]-r["fees_usdt"]-.02)


@pytest.mark.parametrize("fee,slip", [(math.nan, 2), (5, math.inf), (-1, 2), (5, -2), (101, 1)])
def test_bad_costs_blocked(fee, slip):
    with pytest.raises(ValueError): audit.reprice(trade(), ".1", fee, slip)


def test_geometry_not_silently_reselected():
    t = trade(); t["target"] = 10001
    assert audit.reprice(t, ".1", 5, 2)["entry_geometry_preserved"] is False


def mark(ts):
    return {"ts": ts, "open": 10000., "high": 10100., "low": 9900., "close": 10001.}


def test_actual_event_times_sign_and_hour_proxy_bounds():
    fs = [{"ts": ts, "realized_rate": .001} for ts in (0, 4*bt.HOUR, 8*bt.HOUR)]
    marks = {r["ts"]: mark(r["ts"]) for r in fs}
    result = audit.funding_details(trade(), fs, marks)
    assert [r["model_charged"] for r in result] == [False, True, True]
    assert [r["settlement_boundary_ambiguous"] for r in result] == [True, False, True]
    assert sum(r["cashflow"] for r in result) == pytest.approx(-.2)
    assert result[1]["if_held_hour_range_min"] == pytest.approx(-.101)
    assert result[1]["if_held_hour_range_max"] == pytest.approx(-.099)
    short = audit.funding_details(trade("SHORT"), fs, marks)
    assert sum(r["cashflow"] for r in short) == pytest.approx(.2)


def test_intrabar_exit_excludes_next_boundary():
    t = trade(); t["exit_reason"] = "STOP"
    ts = t["exit_ms"]
    r = audit.funding_details(t, [{"ts": ts, "realized_rate": .001}], {ts: mark(ts)})[0]
    assert not r["model_charged"]
    assert r["settlement_boundary_ambiguous"]


@pytest.mark.parametrize("invalid", ["missing", "nan", "bad_range"])
def test_mark_missing_or_invalid_blocks(invalid):
    ts = 4*bt.HOUR; marks = {ts: mark(ts)}
    if invalid == "missing": marks = {}
    if invalid == "nan": marks[ts]["open"] = math.nan
    if invalid == "bad_range": marks[ts]["low"] = 11000
    with pytest.raises(ValueError):
        audit.funding_details(trade(), [{"ts": ts, "realized_rate": .001}], marks)


def test_frozen_config_and_holdout_guards(monkeypatch):
    monkeypatch.setattr(audit.continuous, "strategy_fingerprint", lambda: "frozen")
    inputs = {"config": audit.continuous.fixed_config().model_dump(), "strategy_fingerprint": "frozen",
              "start": 0, "end": audit.continuous.DEVELOPMENT_END+bt.HOUR}
    original = {"strategy_fingerprint": "frozen"}
    with pytest.raises(ValueError, match="Protected holdout"): audit.audit(inputs, original)
    inputs["config"]["slippage_bps"] = 3
    with pytest.raises(ValueError, match="Frozen"): audit.audit(inputs, original)


def test_fee_assumptions_explicit_and_frozen_baseline():
    assert audit.SCENARIOS == {"lower": (3, 1), "base": (5, 2), "higher": (8, 5)}
    assert audit.continuous.fixed_config().execution_enabled is False


def test_cli_exports_immutable_report_and_repeated_run(tmp_path, monkeypatch):
    import sys
    source = tmp_path/"source"; source.mkdir()
    for name in ("inputs.json", "result.json"):
        (source/name).write_text("{}", encoding="utf-8")
    result = {"status": "TEST", "trades": [], "scenarios": {}, "limitations": [],
              "official_evidence_review": {"sources": []}, "funding_uncertainty": {}}
    monkeypatch.setattr(audit, "audit", lambda *_: result)
    monkeypatch.setattr(sys, "argv", ["audit", "--run", str(source), "--output", str(tmp_path/"out")])
    audit.main()
    root = tmp_path/"out"/bt.digest(result)
    before = (root/"report.md").read_bytes()
    audit.main()
    assert (root/"report.md").read_bytes() == before
    assert json.loads((root/"audit.json").read_text())["status"] == "TEST"


@pytest.mark.parametrize("bad", ["decision_future", "timeframe_future", "quality", "mark_gap", "mark_duplicate"])
def test_pit_quality_and_mark_coverage_guards(monkeypatch, bad):
    monkeypatch.setattr(audit.continuous, "strategy_fingerprint", lambda: "frozen")
    d = {"cutoff_ms": 0, "evidence_latest_ms": 0,
         "diagnostic": {"data_quality": True, "evidence": {"1h": {"latest_close_ms": 0}}}}
    data = {"symbol": bt.SYMBOL, "candles": {}, "mark_records": [mark(0), mark(bt.HOUR)]}
    if bad == "decision_future": d["evidence_latest_ms"] = 1
    if bad == "timeframe_future": d["diagnostic"]["evidence"]["1h"]["latest_close_ms"] = 1
    if bad == "quality": d["diagnostic"]["data_quality"] = False
    if bad == "mark_gap": data["mark_records"] = [mark(0)]
    if bad == "mark_duplicate": data["mark_records"].append(mark(0))
    inputs = {"config": audit.continuous.fixed_config().model_dump(), "strategy_fingerprint": "frozen",
              "start": 0, "end": bt.HOUR, "data": data}
    original = {"strategy_fingerprint": "frozen", "execution_enabled": False, "decisions": [d]}
    with pytest.raises(ValueError): audit.audit(inputs, original)
