"""Offline joint research: independent periods, confirmed structure and hard gates."""

import copy
import json

import pytest
from typer.testing import CliRunner

from tests.test_graph_end_to_end import ARGS, ScriptedModel, _Client
from tests.test_okx_derivatives import DerivativeClient
from tests.test_okx_research import CONFIG, DAY, INFO, NOW, SYMBOL
from tradingagents.agents.context import get_instrument_context_from_state
from tradingagents.dataflows.okx_research import final_gate
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv, okx_multitimeframe as mtf
from tradingagents.default_config import DEFAULT_CONFIG

CFG = {
    **CONFIG,
    "okx_multi_timeframe": True,
    "okx_interval": "4h",
    "okx_derivatives_history_points": 10,
}


class MultiClient(DerivativeClient):
    def __init__(self, directions=None, broken=None, pending=True, bad=None):
        super().__init__(bad)
        self.directions = directions or dict.fromkeys(mtf.PERIODS, 1)
        self.broken = broken
        self.pending = pending
        self.candle_calls = []
        self.instruments = 0

    def instrument(self, symbol):
        self.instruments += 1
        assert symbol == SYMBOL
        return copy.deepcopy(INFO)

    def candles(self, symbol, interval, cutoff, count):
        self.candle_calls.append((symbol, interval, cutoff))
        step = okx.BARS[interval][1]
        end = cutoff // step * step - step
        base = {"1h": 1000, "4h": 2000, "1d": 3000}[interval]
        rows = []
        for i in range(count):
            c = base - i * self.directions[interval]
            rows.append(
                [
                    str(end - i * step),
                    str(c - 0.2),
                    str(c + 2),
                    str(c - 2),
                    str(c),
                    "100",
                    "1",
                    str(c),
                    "1",
                ]
            )
        if interval == self.broken:
            rows.pop(10)
        if self.pending:
            rows.insert(
                0,
                [
                    str(end + step),
                    str(base),
                    str(base + 2),
                    str(base - 2),
                    str(base),
                    "100",
                    "1",
                    str(base),
                    "0",
                ],
            )
        return rows


@pytest.fixture
def joint(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(deriv, "utc_ms", lambda: NOW)
    client = MultiClient()
    b = okx.build_bundle(SYMBOL, DAY, CFG, client)
    assert b["status"] == "VALID", b
    return b, client


def test_cutoff_independent_indicators_and_units(joint):
    b, client = joint
    assert len(client.candle_calls) == 3 and client.instruments == 1
    assert {call[2] for call in client.candle_calls} == {b["cutoff_ms"]}
    assert set(b["timeframes"]) == set(mtf.PERIODS)
    assert len({a["snapshot_id"] for a in b["timeframe_analysis"].values()}) == 3
    for period, child in b["timeframes"].items():
        step = okx.BARS[period][1]
        assert child["interval"] == period and child["cutoff_ms"] == b["cutoff_ms"]
        assert child["latest_close_ms"] == b["cutoff_ms"] // step * step
        assert len(child["records"]) == 400
        assert all(r["ts"] + step <= b["cutoff_ms"] for r in child["records"])
        assert b["timeframe_analysis"][period]["indicators"]["close_50_sma"] == pytest.approx(
            sum(r["Close"] for r in child["records"][-50:]) / 50
        )
        assert "Unconfirmed candle excluded" in child["issues"]
        assert child["volume_units"]["Volume"] == "BTC"
    assert okx.BARS["1d"][0] == "1Dutc"
    assert b["timeframes"]["1d"]["latest_close_ms"] % 86400000 == 0
    assert b["confluence"]["state"] == "TREND_RESONANCE"
    assert b["confluence"]["primary_regime"] == "UP"
    before = len(client.candle_calls)
    for render in [
        okx.render_snapshot(b),
        okx.render_prices(b, DAY, DAY),
        okx.render_indicator(b, "rsi"),
    ]:
        assert all(period in render for period in mtf.PERIODS)
    assert len(client.candle_calls) == before


@pytest.mark.parametrize(
    "directions,state",
    [
        ({"1h": -1, "4h": -1, "1d": -1}, "TREND_RESONANCE"),
        ({"1h": 1, "4h": -1, "1d": 1}, "TREND_CONFLICT"),
        ({"1h": 0, "4h": 0, "1d": 0}, "INSUFFICIENT_EVIDENCE"),
        ({"1h": 1, "4h": 1, "1d": -1}, "TREND_CONFLICT"),
    ],
)
def test_resonance_conflict_and_no_majority_vote(joint, directions, state):
    b = okx.build_bundle(SYMBOL, DAY, CFG, MultiClient(directions))
    assert b["status"] == "VALID"
    assert b["confluence"]["state"] == state
    assert b["confluence"]["priority"] == ["1d", "4h", "1h"]


@pytest.mark.parametrize("period", mtf.PERIODS)
def test_missing_required_period_stops_research(joint, period):
    b = okx.build_bundle(SYMBOL, DAY, CFG, MultiClient(broken=period))
    assert b["status"] == "RESEARCH_INVALID" and not b["allow_new_risk"]
    assert b["timeframes"][period]["status"] == "PARTIAL"
    assert "RESEARCH_INVALID" in okx.render_snapshot(b)


@pytest.mark.parametrize(
    "kind", ["missing", "cutoff", "identity", "price", "analysis", "rules", "interval"]
)
def test_integrity_failures(joint, kind):
    original, _ = joint
    b = copy.deepcopy(original)
    if kind == "missing":
        del b["timeframes"]["1h"]
    elif kind == "cutoff":
        b["timeframes"]["1h"]["cutoff_ms"] -= 1000
    elif kind == "identity":
        b["timeframes"]["1h"]["symbol"] = "ETH-USDT-SWAP"
    elif kind == "price":
        b["timeframes"]["1h"]["records"][-1]["Close"] = 9999
    elif kind == "analysis":
        b["timeframe_analysis"]["1h"]["trend"] = "DOWN"
    elif kind == "rules":
        b["structure_rules"]["pivot_window"] = 10
    else:
        b["timeframes"]["1h"]["interval"] = "4h"
    checked = okx.check_bundle(b, CFG, SYMBOL, DAY)
    assert not checked["allow_new_risk"] and checked["status"] == "RESEARCH_INVALID"


def test_stale_children_and_output_gate(joint, monkeypatch, tmp_path):
    b, _ = joint
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW + 901000)
    state = {
        "market_snapshot": b,
        "company_of_interest": SYMBOL,
        "trade_date": DAY,
        "final_rating": "Buy",
        "final_trade_decision": "BUY",
        "llm_status": "AVAILABLE",
    }
    state.update(final_gate(state, CFG))
    assert state["final_rating"] == "REVIEW" and not state["allow_new_risk"]
    assert all(c["status"] == "STALE_DATA" for c in state["market_snapshot"]["timeframes"].values())
    from tradingagents.reporting import write_report_tree

    report = write_report_tree(state, SYMBOL, tmp_path, settings=CFG)
    assert "RESEARCH_INVALID" in report.read_text(encoding="utf-8")
    assert not json.loads((tmp_path / "multitimeframe_snapshot.json").read_text(encoding="utf-8"))[
        "allow_new_risk"
    ]


def test_historical_derivatives_shared_once_and_no_live(joint):
    client = MultiClient()
    b = okx.build_bundle(SYMBOL, "2026-10-21", {**CFG, "okx_derivatives_enabled": True}, client)
    assert b["status"] == "VALID", b
    assert b["historical"] and len({c[2] for c in client.candle_calls}) == 1
    assert not {deriv.BOOK, deriv.OI, deriv.FUNDING, deriv.MARK, deriv.TICKER, deriv.INDEX} & {
        p for p, _ in client.calls
    }
    assert b["derivatives"]["components"]["orderbook"]["status"] == "DATA_UNAVAILABLE"
    assert all("derivatives" not in child for child in b["timeframes"].values())


def test_live_derivatives_shared_once_optional_fault(joint):
    client = MultiClient(bad=deriv.OI)
    b = okx.build_bundle(SYMBOL, DAY, {**CFG, "okx_derivatives_enabled": True}, client)
    assert b["status"] == "VALID"
    assert len([p for p, _ in client.calls if p == deriv.BOOK]) == 1
    assert b["derivatives"]["components"]["oi_current"]["status"] == "DATA_UNAVAILABLE"
    assert b["derivatives"]["research_cutoff_ms"] == b["cutoff_ms"]


def patterned_child(prices, rules):
    """Reuse a validated shape, retain enough warmup for all indicators."""
    rows = MultiClient().candles(SYMBOL, "1h", NOW, 400)[1:]
    rows = list(reversed(rows))
    for i, price in enumerate(prices):
        j = len(rows) - len(prices) + i
        rows[j][1:5] = [str(price), str(price + 1), str(price - 1), str(price)]
    df, _ = okx.validate_candles(rows, "1h", NOW, 250, 400, 0)
    child = {
        "records": df.assign(Date=df.Date.map(lambda x: x.isoformat())).to_dict("records"),
        "interval": "1h",
        "source": "OKX mock",
        "cutoff_ms": NOW,
        "latest_close_ms": int(df.ts.iloc[-1]) + 3600000,
        "snapshot_id": "mock",
        "status": "VALID",
        "volume_units": {"Volume": "BTC"},
    }
    return mtf.analyze_timeframe(child, rules)


@pytest.mark.parametrize("direction,pattern", [(1, "HH_HL"), (-1, "LH_LL")])
def test_confirmed_pivots_no_future_use(direction, pattern):
    rules = mtf.structure_rules({"okx_pivot_window": 1})
    prices = [1000 + direction * (i * 2 + (10 if i % 2 else 0)) for i in range(40)]
    a = patterned_child(prices, rules)
    assert a["pivot_structure"] == pattern
    assert all(
        p["confirmed_at_ms"] <= a["latest_close_ms"]
        for group in a["confirmed_pivots"].values()
        for p in group
    )
    assert all(
        p["bar_open_ms"] < a["latest_close_ms"] - 3600000
        for group in a["confirmed_pivots"].values()
        for p in group
    )


def test_breakout_rejected_break_and_retest_rules():
    rules = mtf.structure_rules({"okx_pivot_window": 1, "okx_retest_atr_fraction": 1})
    assert patterned_child([1000] * 30 + [1010], rules)["events"]["close_breakout_up"]
    assert patterned_child([1000] * 30 + [990], rules)["events"]["close_breakout_down"]
    # Last high 1001.5 > prior high 1001; close 1000.5 stays inside range.
    a = patterned_child([1000] * 30 + [1000.5], rules)
    assert a["events"]["intrabar_up_break_rejected"]
    assert not a["events"]["close_breakout_up"]
    assert patterned_child([1000] * 30 + [1010, 1002], rules)["events"]["up_breakout_retest_held"]
    assert patterned_child([1000] * 30 + [990, 998], rules)["events"][
        "down_breakout_retest_rejected"
    ]


def test_all_agents_receive_period_context(joint):
    b, _ = joint
    text = get_instrument_context_from_state(
        {"company_of_interest": SYMBOL, "instrument_context": "OKX SWAP", "market_snapshot": b}
    )
    assert "1d regime" in text and "1h confirmation" in text
    assert (
        b["snapshot_id"] not in text
    )  # Child IDs and common cutoff, not an ambiguous parent interval.
    assert all(c["snapshot_id"] in text for c in b["timeframes"].values())
    assert "| Issues |\n|---" in text


def graph_config(tmp_path):
    return {
        **copy.deepcopy(DEFAULT_CONFIG),
        **CFG,
        "results_dir": str(tmp_path),
        "memory_log_path": str(tmp_path / "memory.md"),
    }


def test_mock_llm_full_workflow_and_cli(joint, monkeypatch, tmp_path):
    from cli import main
    from tradingagents.graph import trading_graph

    _, client = joint
    model = ScriptedModel(structured=True)
    monkeypatch.setitem(ARGS, "curr_date", DAY)
    monkeypatch.setitem(ARGS, "end_date", DAY)
    monkeypatch.setitem(ARGS, "start_date", DAY)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda *a, **k: _Client(model))
    monkeypatch.setattr(okx, "OKXPublicClient", lambda: client)
    cfg = graph_config(tmp_path)
    graph = trading_graph.TradingAgentsGraph(["market"], config=cfg)
    state, signal = graph.propagate(SYMBOL, DAY, "crypto")
    assert state["workflow_status"] == "COMPLETED" and signal == "Overweight"
    assert state["market_snapshot"]["multi_timeframe"] and not state["execution_enabled"]
    report = graph.save_reports(state, SYMBOL, tmp_path / "report")
    assert "Multi-timeframe quality" in report.read_text(encoding="utf-8")
    assert (
        len(
            json.loads(
                (report.parent / "multitimeframe_snapshot.json").read_text(encoding="utf-8")
            )["timeframes"]
        )
        == 3
    )
    monkeypatch.setattr(main, "DEFAULT_CONFIG", cfg)
    result = CliRunner().invoke(
        main.app,
        ["okx-research", "--interval", "4h", "--multi-timeframe", "--data-only", "--date", DAY],
    )
    assert result.exit_code == 0, result.output


def test_failed_required_period_prevents_any_model_call(joint, monkeypatch, tmp_path):
    from tradingagents.graph import trading_graph

    model = ScriptedModel(structured=True)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda *a, **k: _Client(model))
    monkeypatch.setattr(okx, "OKXPublicClient", lambda: MultiClient(broken="1d"))
    state, signal = trading_graph.TradingAgentsGraph(
        ["market"], config=graph_config(tmp_path)
    ).propagate(SYMBOL, DAY, "crypto")
    assert signal == "REVIEW" and not model.calls and not state["allow_new_risk"]


def test_exact_utc_midnight_boundary(joint, monkeypatch):
    midnight = NOW // 86400000 * 86400000
    monkeypatch.setattr(okx, "utc_ms", lambda: midnight)
    client = MultiClient()
    monkeypatch.setattr(client, "get", lambda path, params=None: [{"ts": str(midnight)}])
    b = okx.build_bundle(SYMBOL, DAY, CFG, client)
    assert b["status"] == "VALID"
    assert all(c["latest_close_ms"] == midnight for c in b["timeframes"].values())
    assert b["timeframes"]["1d"]["records"][-1]["ts"] == midnight - 86400000


def test_future_confirmed_candles_excluded(joint):
    class FutureClient(MultiClient):
        def candles(self, *args):
            rows = super().candles(*args)
            rows[0][-1] = "1"
            return rows

    b = okx.build_bundle(SYMBOL, DAY, CFG, FutureClient())
    assert b["status"] == "VALID"
    assert all(
        "Candle beyond decision cutoff excluded" in c["issues"] for c in b["timeframes"].values()
    )


def test_model_buy_cannot_override_final_required_period_gate(joint, monkeypatch, tmp_path):
    from tradingagents.graph import trading_graph

    _, client = joint
    monkeypatch.setattr(
        trading_graph, "create_tier_client", lambda *a, **k: _Client(ScriptedModel(structured=True))
    )
    monkeypatch.setattr(okx, "OKXPublicClient", lambda: client)

    def bad_portfolio(_llm):
        def node(state):
            b = copy.deepcopy(state["market_snapshot"])
            del b["timeframes"]["1d"]
            return {
                "market_snapshot": b,
                "final_rating": "Buy",
                "final_trade_decision": "BUY",
                "allow_new_risk": True,
            }

        return node

    monkeypatch.setattr("tradingagents.graph.setup.create_portfolio_manager", bad_portfolio)
    state, signal = trading_graph.TradingAgentsGraph(
        ["market"], config=graph_config(tmp_path)
    ).propagate(SYMBOL, DAY, "crypto")
    assert signal == "REVIEW" and not state["allow_new_risk"] and not state["execution_enabled"]


def test_single_mode_cannot_accept_joint_checkpoint(joint):
    b, _ = joint
    assert not okx.check_bundle(b, {**CFG, "okx_multi_timeframe": False})["allow_new_risk"]
