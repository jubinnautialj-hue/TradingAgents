"""Public flow has no account capability; fixed trades verify bounded evidence."""
import copy
import json

import pytest

from tradingagents.dataflows.vendors import okx_orderflow as f
from tests.test_okx_research import INFO, NOW, CONFIG, DAY, SYMBOL

START = NOW-60000
SPOT = {"instId": "BTC-USDT", "instType": "SPOT", "state": "live", "baseCcy": "BTC", "quoteCcy": "USDT"}


class Client:
    def __init__(self): self.calls = []
    def get(self, path, params):
        self.calls.append((path, params))
        if path == "/api/v5/public/instruments": return [SPOT]
        spot = params["instId"] == "BTC-USDT"
        if path == f.TRADES:
            return [{"instId": params["instId"], "tradeId": str(i), "ts": str(ts), "px": px,
                     "sz": ("0.02" if i == 3 else "0.01") if spot else ("2" if i == 3 else "1"),
                     "side": side, "source": "0"}
                    for i, ts, px, side in [(3, NOW-10000, "100", "buy"), (2, START+10000, "90", "sell"), (1, START-1, "90", "sell")]]
        assert path == "/api/v5/market/history-candles"
        return [[str(START), "90", "100", "90", "100", "0.03" if spot else "3", "2.9" if spot else "0.03", "2.9", "1"]]


def bundle():
    return {"status": "VALID", "snapshot_id": "core", "symbol": SYMBOL, "instrument": INFO,
            "cutoff_ms": NOW, "historical": False}


@pytest.fixture(autouse=True)
def clock(monkeypatch): monkeypatch.setattr(f.okx, "utc_ms", lambda: NOW)


def test_actual_side_units_cumulative_and_aligned_linkage():
    raw = f.collect_orderflow(bundle(), {}, Client())
    assert raw["status"] == "VALID"
    for market in raw["markets"].values():
        s = market["statistics"]
        assert s["buy_base"] == .02 and s["sell_base"] == .01
        assert s["delta_base"] == pytest.approx(.01)
        assert market["cvd"]["buckets_10s"][-1]["cvd_base"] == pytest.approx(.01)
        assert market["start_ms"] == START and market["end_ms"] == NOW
        assert market["coverage"]["volume_ratio"] == pytest.approx(1)
    assert raw["linkage"]["window_vwap_premium_bps"] == pytest.approx(0)
    assert raw["linkage"]["causal_leadership"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("field,value", [("side", "unknown"), ("sz", "-1"), ("px", "NaN"),
    ("ts", str(NOW+1)), ("instId", "ETH-USDT-SWAP"), ("tradeId", "bad")])
def test_bad_or_future_trade_blocked(field, value):
    row = Client().get(f.TRADES, {"instId": SYMBOL})[0]
    row[field] = value
    with pytest.raises(ValueError): f.parse_trade(row, INFO, NOW)


@pytest.mark.parametrize("kind", ["missing", "no_progress", "volume", "duplicate", "unclosed", "gap", "reversed"])
def test_incomplete_or_corrupt_data_never_has_complete_cvd(kind):
    class Bad(Client):
        def get(self, path, params):
            page = super().get(path, params)
            if path == f.TRADES:
                if kind in {"missing", "no_progress"}: page = page[:2]
                if kind == "volume": page[0]["sz"] = "1"
                if kind == "reversed": page = list(reversed(page))
            if path.endswith("history-candles"):
                if kind == "duplicate": page += copy.deepcopy(page)
                if kind == "unclosed": page[0][8] = "0"
                if kind == "gap": page = []
            return page
    raw = f.collect_orderflow(bundle(), {"okx_orderflow_max_pages": 1 if kind == "missing" else 2}, Bad())
    assert raw["status"] != "VALID" and raw["linkage"] is None
    assert all(m.get("cvd") is None for m in raw["markets"].values())


def test_historical_does_not_request_current_spot_or_trades():
    client = Client(); raw = f.collect_orderflow({**bundle(), "historical": True}, {}, client)
    assert raw["status"] == "DATA_UNAVAILABLE" and not client.calls


def test_stale_and_tampered_evidence_cannot_be_used():
    raw = f.collect_orderflow(bundle(), {}, Client()); before = copy.deepcopy(raw)
    stale = f.evidence_view(raw, NOW+301000)
    assert stale["status"] == "STALE_DATA" and stale["linkage"] is None
    assert all(m["statistics"] is None and m["cvd"] is None for m in stale["markets"].values())
    assert raw == before
    raw["linkage"]["window_vwap_premium_bps"] = 1
    assert f.evidence_view(raw)["status"] == "RESEARCH_INVALID"


def test_no_spot_no_invented_linkage():
    class NoSpot(Client):
        def get(self, path, params):
            return [] if path.endswith("instruments") else super().get(path, params)
    raw = f.collect_orderflow(bundle(), {}, NoSpot())
    assert raw["markets"]["swap"]["status"] == "VALID"
    assert raw["status"] == "PARTIAL" and raw["linkage"] is None


@pytest.mark.parametrize("conflict", [False, True])
def test_id_pagination_overlap_deduplicates_or_rejects_conflict(conflict):
    class Paged(Client):
        def get(self, path, params):
            rows = super().get(path, params)
            if path == f.TRADES:
                if params["type"] == "2": return rows[:2]
                assert params["after"] == "2"
                rows = rows[1:]
                if conflict: rows[0]["px"] = "91"
            return rows
    raw = f.collect_orderflow(bundle(), {"okx_orderflow_max_pages": 2}, Paged())
    assert (raw["status"] == "VALID") is (not conflict)
    if not conflict:
        assert raw["markets"]["swap"]["pages"] == 2
        assert raw["markets"]["swap"]["trade_count"] == 2


def test_conversion_overflow_rejected():
    row = Client().get(f.TRADES, {"instId": SYMBOL})[0]
    row.update(px="1e308", sz="1e308")
    with pytest.raises(ValueError): f.parse_trade(row, INFO, NOW)


@pytest.mark.parametrize("config", [{"okx_orderflow_minutes": 0}, {"okx_orderflow_minutes": 61},
                                    {"okx_orderflow_max_pages": 101}, {"okx_orderflow_ttl_seconds": 301}])
def test_request_and_freshness_budgets_are_bounded(config):
    with pytest.raises(ValueError): f.collect_orderflow(bundle(), config, Client())


def test_multi_period_sums_and_price_delta_disagreement():
    raw = f.collect_orderflow(bundle(), {}, Client())
    m = raw["markets"]["swap"]
    assert sum(x["delta_base"] for x in m["cvd"]["buckets_30s"]) == pytest.approx(m["statistics"]["delta_base"])
    assert len(m["cvd"]["buckets_10s"]) == 6
    rows = [{"ts": START+1, "side": "buy", "base": 1, "quote": 100},
            {"ts": START+2, "side": "sell", "base": 2, "quote": 202}]
    assert f.aggregate(rows, START, NOW, 60000)[0]["cvd_base"] == -1
    rows[0].update(price=100, ts=START+1000)
    rows[1].update(price=101, ts=NOW-1000)
    assert f.divergence_evidence(rows, START, NOW)["higher_price_high_lower_cvd"]


def test_every_existing_role_context_can_see_bounded_evidence():
    from tradingagents.agents.context import get_instrument_context_from_state
    raw = f.collect_orderflow(bundle(), {}, Client())
    text = get_instrument_context_from_state({"company_of_interest": SYMBOL, "instrument_context": "OKX swap", "orderflow_evidence": raw})
    assert "Optional taker flow" in text and "causality" in text
    assert "raw_trades" not in text and "trade_id" not in text


def test_preflight_opt_in_and_default_no_new_requests(monkeypatch):
    from tradingagents.dataflows.config import run_config
    from tradingagents.dataflows.okx_research import preflight
    monkeypatch.setattr("tradingagents.dataflows.okx_research.build_bundle", lambda *a: bundle())
    monkeypatch.setattr("tradingagents.dataflows.okx_research.check_bundle", lambda b,*a: {**b,"allow_new_risk": True})
    calls = []
    monkeypatch.setattr(f, "collect_orderflow", lambda *a: calls.append(1) or {"status": "PARTIAL"})
    with run_config({**CONFIG, "okx_orderflow_enabled": False}): preflight({"company_of_interest": SYMBOL,"trade_date": DAY})
    assert not calls
    with run_config({**CONFIG, "okx_orderflow_enabled": True}):
        s = preflight({"company_of_interest": SYMBOL,"trade_date": DAY})
    assert len(calls) == 1 and s["data_quality"] == "VALID" and not s["execution_enabled"]


def test_report_contains_original_and_effective_flow_json(tmp_path):
    from tradingagents.reporting import write_report_tree
    raw = f.collect_orderflow(bundle(), {}, Client())
    report = write_report_tree({"company_of_interest": SYMBOL, "trade_date": DAY, "orderflow_evidence": raw,
        "final_trade_decision": "WAIT - mock evidence only", "final_rating": "REVIEW", "execution_enabled": False}, SYMBOL, tmp_path)
    saved = json.loads((tmp_path/"orderflow_snapshot.json").read_text())
    assert saved["original"] == raw and not saved["execution_enabled"]
    assert "taker flow" in report.read_text() and (tmp_path/"complete_report.html").exists()


def test_native_mock_graph_preserves_flow_and_original_gate(monkeypatch, tmp_path):
    from tests.test_okx_graph import graph_setup
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    model, _, config = graph_setup.__wrapped__(monkeypatch, tmp_path)
    raw = f.collect_orderflow(bundle(), {}, Client())
    monkeypatch.setattr(f, "collect_orderflow", lambda *a: raw)
    config["okx_orderflow_enabled"] = True
    graph = TradingAgentsGraph(["market"], config=config)
    state, _ = graph.propagate(SYMBOL, DAY, "crypto")
    assert state["orderflow_evidence"] == raw and len(model.calls) >= 9
    assert state["data_quality"] == "VALID" and not state["execution_enabled"]
    assert graph.run_settings()["okx_orderflow_enabled"]


@pytest.mark.integration
def test_public_btc_flow_either_reconciles_or_explicitly_degrades(monkeypatch):
    # Undo this module's test clock only; actual requests use the shared GET client.
    from datetime import UTC, datetime
    monkeypatch.setattr(f.okx, "utc_ms", lambda: int(datetime.now(UTC).timestamp()*1000))
    client = f.okx.OKXPublicClient()
    cutoff = int(client.get("/api/v5/public/time")[0]["ts"])
    core = {**bundle(), "cutoff_ms": cutoff, "instrument": client.instrument(SYMBOL)}
    raw = f.collect_orderflow(core, {}, client)
    assert not raw["execution_enabled"] and raw["end_ms"] <= cutoff
    assert raw["snapshot_id"]
    for market in raw["markets"].values():
        if market["status"] == "VALID":
            assert all(c["matched"] for c in market["volume_reconciliation"])
            assert all(t["ts"] < raw["end_ms"] for t in market["raw_trades"])
        else:
            assert market["statistics"] is None and market["cvd"] is None
