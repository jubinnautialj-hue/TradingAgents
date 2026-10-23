"""OKX public research contracts, with no live/account/write requests."""

import copy

import pandas as pd
import pytest
from typer.testing import CliRunner

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.okx_research import final_gate, preflight
from tradingagents.dataflows.vendors import okx

NOW = int(pd.Timestamp("2026-10-22T12:05:00Z").timestamp() * 1000)
DAY = "2026-10-22"
SYMBOL = "BTC-USDT-SWAP"
CONFIG = {"market_mode": okx.MODE, "okx_interval": "1h", "okx_history_bars": 400,
          "okx_min_bars": 250, "okx_max_lag_bars": 0, "okx_bundle_ttl_seconds": 900}
INFO = {"instId": SYMBOL, "instType": "SWAP", "instFamily": "BTC-USDT", "quoteCcy": "",
        "settleCcy": "USDT", "ctType": "linear", "state": "live", "ctValCcy": "BTC",
        "ctVal": "0.01", "ctMult": "1", "minSz": "0.01", "lotSz": "0.01", "tickSz": "0.1"}


def candles(interval="1h", count=400, cutoff=NOW):
    step = okx.BARS[interval][1]
    end = cutoff // step * step - step
    return [[str(end - i * step), str(100 + (i % 7)), "110", "90", str(101 + i % 5),
             "100", "1", "105", "1"] for i in range(count)]


class FakeClient:
    def get(self, path, params=None):
        assert path == "/api/v5/public/time"
        return [{"ts": str(NOW)}]

    def instrument(self, symbol):
        okx.validate_symbol(symbol)
        return copy.deepcopy(INFO)

    def candles(self, symbol, interval, cutoff, count):
        return candles(interval, count, cutoff)


@pytest.fixture
def bundle(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    result = okx.build_bundle(SYMBOL, DAY, CONFIG, FakeClient())
    assert result["status"] == "VALID", result
    return result


@pytest.mark.parametrize("interval", ["1h", "4h", "1d"])
def test_intervals_utc_units(monkeypatch, interval):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    bundle = okx.build_bundle(SYMBOL, DAY, {**CONFIG, "okx_interval": interval}, FakeClient())
    assert bundle["status"] == "VALID", bundle
    assert bundle["symbol"] == SYMBOL
    assert bundle["volume_units"] == {"Volume": "BTC", "VolumeContracts": "contracts", "VolumeQuote": "USDT"}
    assert bundle["records"][-1]["Volume"] == 1
    assert all(r["ts"] % okx.BARS[interval][1] == 0 for r in bundle["records"])
    assert bundle["records"][-1]["Date"].endswith("+00:00")
    if interval == "1d":
        assert okx.BARS[interval][0] == "1Dutc"


def test_symbol_and_no_yahoo_fallback():
    from cli.prompts import detect_asset_type, parse_ticker
    from tradingagents.dataflows.router import route_to_vendor
    assert parse_ticker(SYMBOL) == SYMBOL
    assert detect_asset_type(SYMBOL).value == "crypto"
    with pytest.raises(ValueError, match="fallback is blocked"):
        route_to_vendor("get_stock_data", SYMBOL, DAY, DAY)
    for bad in ("BTC-USDT", "BTC-USD", "BTC-USD-SWAP", "../BTC-USDT-SWAP"):
        with pytest.raises(ValueError):
            okx.validate_symbol(bad)


@pytest.mark.parametrize("field,value", [("settleCcy", "BTC"), ("state", "suspend"),
                                         ("ctType", "inverse"), ("minSz", "0"),
                                         ("tickSz", "nan"), ("ctVal", "")])
def test_instrument_validation(field, value):
    with pytest.raises(okx.DataQualityError):
        okx.validate_instrument({**INFO, field: value}, SYMBOL)


def test_unconfirmed_and_future_excluded():
    rows = candles()
    step = okx.BARS["1h"][1]
    pending = [str(NOW // step * step), "100", "110", "90", "101", "100", "1", "105", "0"]
    df, notes = okx.validate_candles([pending, *rows], "1h", NOW, 250, 400, 0)
    assert len(df) == 400 and "Unconfirmed candle excluded" in notes
    pending[-1] = "1"
    df, notes = okx.validate_candles([pending, *rows], "1h", NOW, 250, 400, 0)
    assert len(df) == 400 and any("cutoff" in n for n in notes)


@pytest.mark.parametrize("kind", ["duplicate", "gap", "nan", "inf", "negative", "ohlc", "timestamp", "short", "confirm"])
def test_bad_data(kind):
    rows = candles()
    if kind == "duplicate":
        rows[10] = rows[11]
    elif kind == "gap":
        rows.pop(10)
    elif kind in ("nan", "inf", "negative", "ohlc"):
        rows[0][1] = {"nan": "NaN", "inf": "Inf", "negative": "-1", "ohlc": "999"}[kind]
    elif kind == "timestamp":
        rows[0][0] = "not a timestamp"
    elif kind == "short":
        rows[0].pop()
    else:
        rows[0][-1] = "2"
    with pytest.raises(okx.DataQualityError):
        okx.validate_candles(rows, "1h", NOW, 250, 400, 0)


def test_sort_warmup_stale():
    rows = candles()
    rows[2], rows[8] = rows[8], rows[2]
    df, notes = okx.validate_candles(rows, "1h", NOW, 250, 400, 0)
    assert df.ts.is_monotonic_increasing and any("Out-of-order" in n for n in notes)
    with pytest.raises(okx.DataQualityError, match="Warmup"):
        okx.validate_candles(candles(count=200), "1h", NOW, 250, 400)
    with pytest.raises(okx.DataQualityError, match="freshness"):
        okx.validate_candles(candles(cutoff=NOW - 7200000), "1h", NOW, 250, 400, 0)


def test_history_date(bundle):
    cutoff = okx.research_cutoff("2026-10-20", NOW)
    assert cutoff == int(pd.Timestamp("2026-10-21T00:00Z").timestamp() * 1000)
    df, _ = okx.validate_candles(candles(cutoff=cutoff), "1h", cutoff, 250, 400, 0)
    assert (df.ts + 3600000 <= cutoff).all()


def test_pagination(monkeypatch):
    client = okx.OKXPublicClient()
    all_rows = candles(count=500)
    calls = []
    def get(path, params):
        calls.append((path, params))
        return [r for r in all_rows if int(r[0]) < int(params["after"])][:100]
    monkeypatch.setattr(client, "get", get)
    rows = client.candles(SYMBOL, "1d", NOW, 400)
    # Bar parameter is part of every request (the fixture below uses 1h).
    assert all(c[1]["bar"] == "1Dutc" for c in calls)
    calls.clear()
    rows = client.candles(SYMBOL, "1h", NOW, 400)
    assert len(rows) == 400 and len(calls) == 4
    assert len({r[0] for r in rows}) == 400
    assert int(calls[1][1]["after"]) == int(all_rows[99][0])


def test_shared_snapshot_tools_no_requests(bundle, monkeypatch):
    from tradingagents.agents.tools import (
        get_indicators,
        get_stock_data,
        get_verified_market_snapshot,
    )
    monkeypatch.setattr(okx.OKXPublicClient, "get", lambda *a, **k: pytest.fail("Repeated request"))
    with run_config(CONFIG):
        outputs = [get_stock_data.func(SYMBOL, "2026-10-20", DAY, DAY, bundle),
                   get_indicators.func(SYMBOL, "rsi,atr", DAY, 30, DAY, bundle),
                   get_verified_market_snapshot.func(SYMBOL, DAY, 30, DAY, bundle)]
    assert all(bundle["snapshot_id"] in output for output in outputs)
    assert all(SYMBOL in output for output in outputs)
    with run_config(CONFIG):
        past = get_indicators.func(SYMBOL, "rsi", "2026-10-20", 30, DAY, bundle)
    assert "RESEARCH_INVALID" in past and "historical" in past


@pytest.mark.parametrize("mutation", ["stale", "interval", "identity", "records", "date", "missing"])
def test_hard_gate_blocks_buy(bundle, monkeypatch, mutation):
    state = {"company_of_interest": SYMBOL, "trade_date": DAY, "market_snapshot": copy.deepcopy(bundle),
             "final_rating": "Buy", "final_trade_decision": "**Rating**: Buy", "allow_new_risk": True}
    if mutation == "stale":
        monkeypatch.setattr(okx, "utc_ms", lambda: NOW + 901000)
    elif mutation == "interval":
        state["market_snapshot"]["interval"] = "4h"
    elif mutation == "identity":
        state["company_of_interest"] = "ETH-USDT-SWAP"
    elif mutation == "records":
        state["market_snapshot"]["records"][-1]["Close"] = 999999
    elif mutation == "date":
        state["trade_date"] = "2026-10-20"
    else:
        state["market_snapshot"] = {}
    result = final_gate(state, CONFIG)
    assert not result["allow_new_risk"] and not result["execution_enabled"]
    assert result["final_rating"] == "REVIEW"
    assert "RESEARCH_INVALID" in result["final_trade_decision"]


def test_http_allowlist_retry_and_no_auth(monkeypatch):
    calls, sleeps = [], []
    responses = [429, 503, 200]
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs, self.auth))
            code = responses.pop(0)
            return type("Response", (), {"status_code": code, "headers": {"Retry-After": "1"},
                                         "json": lambda self: {"code": "0", "data": [{"ts": str(NOW)}]}})()
    monkeypatch.setattr(okx.requests, "Session", Session)
    monkeypatch.setattr(okx.time, "sleep", sleeps.append)
    assert okx.OKXPublicClient().get("/api/v5/public/time")
    assert len(calls) == 3 and any(s >= 1 for s in sleeps)
    assert all(url.startswith(okx.BASE_URL) and kwargs["allow_redirects"] is False
               and "headers" not in kwargs and callable(auth) for url, kwargs, auth in calls)
    for path in ("/api/v5/trade/order", "/api/v5/account/balance", "/api/v5/asset/transfer"):
        with pytest.raises(ValueError, match="blocked"):
            okx.OKXPublicClient().get(path)
    assert len(calls) == 3


def test_http_timeout_bounded(monkeypatch):
    count = []
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def get(self, *a, **k):
            count.append(1)
            raise okx.requests.Timeout("sensitive URL must not propagate")
    monkeypatch.setattr(okx.requests, "Session", Session)
    monkeypatch.setattr(okx.time, "sleep", lambda _: None)
    with pytest.raises(okx.VendorUnavailableError, match="3 attempts: Timeout"):
        okx.OKXPublicClient().get("/api/v5/public/time")
    assert len(count) == 3


def test_reports_recheck_invalid_buy(bundle, monkeypatch, tmp_path):
    from tradingagents.reporting import write_report_tree
    state = {"company_of_interest": SYMBOL, "trade_date": DAY, "market_snapshot": bundle,
             "final_rating": "Buy", "final_trade_decision": "**Rating**: Buy"}
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW + 901000)
    write_report_tree(state, SYMBOL, tmp_path, settings=CONFIG)
    for filename in ("complete_report.md", "complete_report.html"):
        text = (tmp_path / filename).read_text(encoding="utf-8")
        assert "RESEARCH_INVALID" in text and "STALE_DATA" in text and "REVIEW" in text


def test_cli_data_only_same_gate(bundle, monkeypatch, tmp_path):
    import cli.main as main
    from cli.main import app
    monkeypatch.setattr(okx, "build_bundle", lambda *a, **k: bundle)
    monkeypatch.setattr("tradingagents.dataflows.okx_research.build_bundle", lambda *a, **k: bundle)
    monkeypatch.setitem(main.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    result = CliRunner().invoke(app, ["okx-research", "--instrument", SYMBOL, "--date", DAY, "--data-only"])
    assert result.exit_code == 0, result.output
    assert "VALID" in result.output and "execution_enabled=False" in result.output
    invalid = {**bundle, "status": "PARTIAL", "allow_new_risk": False, "issues": ["insufficient bars"]}
    monkeypatch.setattr("tradingagents.dataflows.okx_research.build_bundle", lambda *a, **k: invalid)
    result = CliRunner().invoke(app, ["okx-research", "--instrument", SYMBOL, "--date", DAY, "--data-only"])
    assert result.exit_code == 2 and "allow_new_risk=False" in result.output


def test_preflight_failure_no_direction(monkeypatch):
    monkeypatch.setattr(okx.OKXPublicClient, "get", lambda *a, **k: (_ for _ in ()).throw(okx.requests.Timeout()))
    with run_config(CONFIG):
        result = preflight({"company_of_interest": SYMBOL, "trade_date": DAY})
    assert result["data_quality"] == "DATA_UNAVAILABLE"
    assert result["final_rating"] == "REVIEW" and not result["allow_new_risk"]
