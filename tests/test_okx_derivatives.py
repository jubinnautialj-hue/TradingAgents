"""No network: public-only derivative units, chronology, isolation and research wiring."""

import copy
import json

import pytest
from typer.testing import CliRunner

from tests.test_okx_research import CONFIG, DAY, INFO, NOW, SYMBOL, FakeClient
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.okx_research import final_gate, preflight
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv


class DerivativeClient(FakeClient):
    def __init__(self, bad=None):
        self.calls = []
        self.bad = bad

    def get(self, path, params=None):
        self.calls.append((path, params))
        assert path in okx.PUBLIC_PATHS
        if path == self.bad:
            raise TimeoutError("not forwarded to model")
        if path == "/api/v5/public/time":
            return [{"ts": str(NOW)}]
        row = {"instId": SYMBOL, "instType": "SWAP", "ts": str(NOW - 1000)}
        if path == deriv.OI:
            return [{**row, "oi": "100", "oiCcy": "1", "oiUsd": "100"}]
        if path == deriv.FUNDING:
            return [
                {
                    **row,
                    "fundingRate": "0.0001",
                    "fundingTime": str(NOW + 3600000),
                    "nextFundingTime": str(NOW + 10800000),
                    "settState": "settled",
                }
            ]
        if path == deriv.MARK:
            return [{**row, "markPx": "100"}]
        if path == deriv.INDEX:
            return [{**row, "instId": "BTC-USDT", "idxPx": "99"}]
        if path == deriv.TICKER:
            return [{**row, "last": "101"}]
        if path == deriv.BOOK:
            return [
                {
                    "ts": row["ts"],
                    "asks": [["101", "100", "0", "1"], ["102", "100", "0", "2"]],
                    "bids": [["99", "100", "0", "1"], ["98", "100", "0", "2"]],
                }
            ]
        interval = next(
            k for k, v in okx.BARS.items() if v[0] == params.get("period", params.get("bar", "1H"))
        )
        step = okx.BARS[interval][1]
        cursor = int(params.get("end", params.get("after")))
        end = (cursor - 1) // step * step
        stamps = [end - i * step for i in range(int(params["limit"]))]
        if path == deriv.OI_HISTORY:
            return [[str(t), "100", "1", "100"] for t in stamps]
        if path == deriv.FUNDING_HISTORY:
            return [
                {**row, "fundingTime": str(t), "fundingRate": "0.0002", "realizedRate": "0.0001"}
                for t in stamps
            ]
        if path in (deriv.MARK_HISTORY, deriv.INDEX_HISTORY):
            return [[str(t), "100", "110", "90", "101", "1"] for t in stamps]
        raise AssertionError(path)


@pytest.fixture
def enhanced(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(deriv, "utc_ms", lambda: NOW)
    client = DerivativeClient()
    cfg = {**CONFIG, "okx_derivatives_enabled": True, "okx_derivatives_history_points": 10}
    bundle = okx.build_bundle(SYMBOL, DAY, cfg, client)
    assert bundle["status"] == "VALID", bundle
    return cfg, bundle, client


def test_pinned_optional_data_and_integrity(enhanced):
    cfg, b, client = enhanced
    assert b["derivatives"]["research_cutoff_ms"] == b["cutoff_ms"]
    assert okx.check_bundle(b, cfg)["allow_new_risk"]
    assert b["execution_enabled"] is False
    calls = len(client.calls)
    okx.render_prices(b, DAY, DAY)
    okx.render_indicator(b, "rsi")
    rendered = okx.render_snapshot(b)
    assert "forecast_rate" in rendered and "not taker flow" in rendered
    assert len(client.calls) == calls
    damaged = copy.deepcopy(b)
    damaged["derivatives"]["components"]["oi_current"]["data"]["contracts"] = 200
    assert not okx.check_bundle(damaged, cfg)["allow_new_risk"]


@pytest.mark.parametrize("interval", ["1h", "4h", "1d"])
def test_history_units_utc_and_alignment(monkeypatch, interval):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(deriv, "utc_ms", lambda: NOW)
    b = okx.build_bundle(
        SYMBOL,
        DAY,
        {
            **CONFIG,
            "okx_interval": interval,
            "okx_derivatives_enabled": True,
            "okx_derivatives_history_points": 10,
        },
        DerivativeClient(),
    )
    assert b["status"] == "VALID"
    parts = b["derivatives"]["components"]
    assert parts["oi_current"]["data"]["base_quantity"] == 1
    assert parts["oi_current"]["units"]["notional_usd"] == "USD"
    assert parts["funding_current"]["data"]["observed_interval_hours"] == 2
    assert all(r["close_ms"] <= b["cutoff_ms"] for r in parts["mark_history"]["data"]["records"])
    assert b["derivatives"]["price_oi_evidence"]


def test_historical_never_fetches_live(monkeypatch):
    monkeypatch.setattr(deriv, "utc_ms", lambda: NOW)
    client = DerivativeClient()
    cutoff = NOW - 86400000
    e = deriv.collect_derivatives(
        client, INFO, cutoff, "1h", True, {"okx_derivatives_history_points": 10}
    )
    assert not {deriv.OI, deriv.FUNDING, deriv.MARK, deriv.INDEX, deriv.TICKER, deriv.BOOK} & {
        p for p, _ in client.calls
    }
    assert e["components"]["oi_current"]["status"] == "DATA_UNAVAILABLE"
    assert all(r["ts"] <= cutoff for r in e["components"]["funding_history"]["data"]["records"])


def test_optional_failure_does_not_bypass_or_block_core(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(deriv, "utc_ms", lambda: NOW)
    cfg = {**CONFIG, "okx_derivatives_enabled": True, "okx_derivatives_history_points": 10}
    b = okx.build_bundle(SYMBOL, DAY, cfg, DerivativeClient(deriv.OI))
    assert b["status"] == "VALID" and b["allow_new_risk"]
    assert b["derivatives"]["components"]["oi_current"]["status"] == "DATA_UNAVAILABLE"
    assert "not forwarded" not in json.dumps(b)
    b["records"][0]["Close"] = -1
    assert not okx.check_bundle(b, cfg)["allow_new_risk"]


def test_expiry_no_mutation_or_future_use(enhanced):
    _, b, _ = enhanced
    e = b["derivatives"]
    original = copy.deepcopy(e)
    view = deriv.evidence_view(e, NOW + 301000)
    assert view["components"]["orderbook"]["status"] == "STALE_DATA"
    assert "data" not in view["components"]["orderbook"]
    assert e == original
    saved_view = deriv.evidence_view(e, NOW)
    assert "price_comparison" in saved_view
    assert "price_comparison" not in deriv.evidence_view(saved_view, NOW + 301000)
    e["components"]["oi_current"]["as_of_ms"] = NOW + 1
    assert deriv.evidence_view(e, NOW)["components"]["oi_current"]["status"] == "RESEARCH_INVALID"


@pytest.mark.parametrize("rate", [0.001, -0.001])
def test_realized_funding_direction_and_no_forecast_substitution(rate):
    assert deriv.funding_cashflow(2, 100, rate, "LONG") == -200 * rate
    assert deriv.funding_cashflow(2, 100, rate, "SHORT") == 200 * rate
    row = {
        "instId": SYMBOL,
        "instType": "SWAP",
        "fundingTime": str(NOW),
        "fundingRate": "0.9",
        "realizedRate": str(rate),
    }
    result = deriv.parse_funding_history([row], SYMBOL, NOW)
    assert result["records"][0]["long_cashflow_per_1000_usdt_notional"] == -1000 * rate
    row["realizedRate"] = ""
    with pytest.raises(ValueError):
        deriv.parse_funding_history([row], SYMBOL, NOW)


@pytest.mark.parametrize("bad", ["nan", "inf", "negative", "unit", "future"])
def test_invalid_oi(bad):
    row = {"ts": NOW, "oi": "100", "oiCcy": "1", "oiUsd": "100"}
    if bad in {"nan", "inf", "negative"}:
        row["oiUsd"] = {"nan": "nan", "inf": "inf", "negative": "-1"}[bad]
    elif bad == "unit":
        row["oiCcy"] = "100"
    else:
        row["ts"] = NOW + 1
    with pytest.raises(ValueError):
        deriv.parse_oi(row, INFO, NOW)


def test_oi_gap_zero_and_rate():
    step = 3600000
    start = NOW // step * step - 4 * step
    rows = [
        [start, "0", "0", "0"],
        [start + step, "100", "1", "100"],
        [start + 2 * step, "200", "2", "200"],
        [start + 4 * step, "300", "3", "300"],
    ]
    d = deriv.parse_oi_history(rows, INFO, NOW, "1h")
    assert d["has_gaps"] and len(d["changes"]) == 1
    assert d["changes"][0]["change_fraction"] == 1
    with pytest.raises(ValueError):
        deriv.parse_oi_history(rows + [rows[0]], INFO, NOW, "1h")


def test_book_units_fills_and_insufficient_depth():
    row = DerivativeClient().get(deriv.BOOK)[0]
    book = deriv.parse_book(row, INFO, NOW, 150)
    assert book["asks"][0]["base_quantity"] == 1
    assert book["buy_estimate"]["vwap"] == pytest.approx((101 + 102 * 0.5) / 1.5)
    assert book["sell_estimate"]["vwap"] == pytest.approx((99 + 98 * 0.5) / 1.5)
    assert book["imbalance"] == 0 and book["abnormal_spread"]
    thin = deriv.parse_book(row, INFO, NOW, 100000)
    assert thin["buy_estimate"]["status"] == "INSUFFICIENT_DEPTH"
    assert thin["buy_estimate"]["slippage_bps_vs_best"] is None


@pytest.mark.parametrize(
    "bad", ["cross", "sort", "empty", "duplicate", "negative", "tick", "future"]
)
def test_abnormal_books(bad):
    row = DerivativeClient().get(deriv.BOOK)[0]
    if bad == "cross":
        row["bids"][0][0] = "102"
    elif bad == "sort":
        row["asks"].reverse()
    elif bad == "empty":
        row["asks"] = []
    elif bad == "duplicate":
        row["asks"].append(row["asks"][0])
    elif bad == "negative":
        row["asks"][0][1] = "-1"
    elif bad == "tick":
        row["asks"][0][0] = "101.01"
    else:
        row["ts"] = NOW + 1
    with pytest.raises(ValueError):
        deriv.parse_book(row, INFO, NOW, 100)


def test_price_confirm_and_cutoff():
    end = NOW // 3600000 * 3600000
    rows = [[end, "100", "110", "90", "101", "0"], [end - 3600000, "100", "110", "90", "101", "1"]]
    assert len(deriv.parse_price_history(rows, "1h", NOW)["records"]) == 1
    rows[0][-1] = "1"
    assert len(deriv.parse_price_history(rows, "1h", NOW)["records"]) == 1


def test_pagination_and_overlap(monkeypatch):
    client = DerivativeClient()
    rows = deriv.history_pages(
        client,
        deriv.OI_HISTORY,
        {"instId": SYMBOL, "period": "1H"},
        NOW,
        cursor_field="end",
        time_key=lambda r: r[0],
        count=250,
    )
    assert len(rows) == 250 and len(client.calls) == 3
    assert len({r[0] for r in rows}) == 250
    monkeypatch.setattr(client, "get", lambda *a, **k: rows[:100])
    with pytest.raises(ValueError, match="progress|overlapped"):
        deriv.history_pages(
            client,
            deriv.OI_HISTORY,
            {},
            NOW,
            cursor_field="end",
            time_key=lambda r: r[0],
            count=250,
        )


def test_cli_and_api_same_gate_and_archive(enhanced, monkeypatch, tmp_path):
    cfg, _, client = enhanced
    from cli import main

    monkeypatch.setattr(okx, "OKXPublicClient", lambda: client)
    cfg = {**cfg, "results_dir": str(tmp_path), "data_vendors": {}}
    monkeypatch.setattr(main, "DEFAULT_CONFIG", cfg)
    with run_config(cfg):
        state = {"company_of_interest": SYMBOL, "trade_date": DAY}
        state.update(preflight(state))
        state.update(final_gate(state))
    assert state["allow_new_risk"] and not state["execution_enabled"]
    result = CliRunner().invoke(
        main.app, ["okx-research", "--date", DAY, "--data-only", "--derivatives"]
    )
    assert result.exit_code == 0, result.output
    archive = json.loads(
        next(tmp_path.rglob("derivatives_snapshot.json")).read_text(encoding="utf-8")
    )
    assert archive["execution_enabled"] is False
    assert archive["snapshot_id"] == okx.fingerprint(state["market_snapshot"])


def test_public_path_guard_still_blocks_private():
    for path in ["/api/v5/trade/order", "/api/v5/account/balance", "/api/v5/asset/transfer"]:
        with pytest.raises(ValueError):
            okx.OKXPublicClient().get(path)


def test_enriched_full_agent_workflow(enhanced, monkeypatch, tmp_path):
    from tests.test_graph_end_to_end import ARGS, ScriptedModel, _Client
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph

    cfg, _, client = enhanced
    client.calls.clear()
    cfg = {
        **copy.deepcopy(DEFAULT_CONFIG),
        **cfg,
        "results_dir": str(tmp_path),
        "memory_log_path": str(tmp_path / "memory.md"),
    }
    monkeypatch.setitem(ARGS, "curr_date", DAY)
    monkeypatch.setitem(ARGS, "end_date", DAY)
    monkeypatch.setitem(ARGS, "start_date", DAY)
    model = ScriptedModel(structured=True)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda *a, **k: _Client(model))
    monkeypatch.setattr(okx, "OKXPublicClient", lambda: client)
    state, signal = trading_graph.TradingAgentsGraph(["market"], config=cfg).propagate(
        SYMBOL, DAY, "crypto"
    )
    assert signal == "Overweight" and state["workflow_status"] == "COMPLETED"
    assert state["market_snapshot"]["derivatives"]["components"]["orderbook"]["status"] == "VALID"
    assert not state["execution_enabled"]
    assert len([p for p, _ in client.calls if p == deriv.BOOK]) == 1


def test_optional_collection_crossing_close_boundary_fails_closed(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(deriv, "utc_ms", lambda: NOW)

    class BoundaryClient(DerivativeClient):
        times = 0

        def get(self, path, params=None):
            if path == "/api/v5/public/time":
                self.times += 1
                if self.times == 2:
                    monkeypatch.setattr(okx, "utc_ms", lambda: NOW + 3600000)
                    return [{"ts": str(NOW + 3600000)}]
            return super().get(path, params)

    cfg = {**CONFIG, "okx_derivatives_enabled": True, "okx_derivatives_history_points": 10}
    b = okx.build_bundle(SYMBOL, DAY, cfg, BoundaryClient())
    assert b["status"] == "STALE_DATA" and not b["allow_new_risk"]
