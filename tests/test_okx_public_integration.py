"""Opt-in live PUBLIC GET verification. Never uses auth/MCP/account tools."""

from datetime import UTC, datetime

import pytest

from tradingagents.dataflows.vendors import okx


@pytest.mark.integration
@pytest.mark.parametrize("interval", ["1h", "4h", "1d"])
def test_okx_public_closed_candles(interval):
    config = {"market_mode": okx.MODE, "okx_interval": interval, "okx_history_bars": 400,
              "okx_min_bars": 250, "okx_max_lag_bars": 0, "okx_bundle_ttl_seconds": 900}
    bundle = okx.build_bundle("BTC-USDT-SWAP", datetime.now(UTC).strftime("%Y-%m-%d"), config)
    assert bundle["status"] == "VALID", okx.describe(bundle)
    assert len(bundle["records"]) == 400
    assert bundle["instrument"]["settleCcy"] == "USDT"
    assert bundle["volume_units"]["Volume"] == "BTC"
    assert okx.check_bundle(bundle, config)["allow_new_risk"]
    print(okx.describe(bundle))


@pytest.mark.integration
def test_okx_public_historical_cutoff():
    config = {"market_mode": okx.MODE, "okx_interval": "1h", "okx_history_bars": 400,
              "okx_min_bars": 250, "okx_max_lag_bars": 0}
    bundle = okx.build_bundle("BTC-USDT-SWAP", "2026-10-01", config)
    assert bundle["status"] == "VALID", okx.describe(bundle)
    assert bundle["historical"]
    assert all(row["ts"] + 3600000 <= bundle["cutoff_ms"] for row in bundle["records"])
    assert bundle["latest_close_ms"] == bundle["cutoff_ms"]
    print(okx.describe(bundle))


@pytest.mark.integration
@pytest.mark.parametrize("interval", ["1h", "4h", "1d"])
def test_okx_public_derivative_evidence(interval):
    from tradingagents.dataflows.vendors.okx_derivatives import evidence_view

    config = {"market_mode": okx.MODE, "okx_interval": interval, "okx_derivatives_enabled": True,
              "okx_derivatives_history_points": 10, "okx_max_lag_bars": 0}
    b = okx.build_bundle("BTC-USDT-SWAP", datetime.now(UTC).strftime("%Y-%m-%d"), config)
    assert b["status"] == "VALID", okx.describe(b)
    e = evidence_view(b["derivatives"])
    for name in ("oi_current", "funding_current", "mark_current", "index_current", "orderbook",
                 "oi_history", "funding_history", "mark_history", "index_history"):
        part = e["components"][name]
        assert part["status"] == "VALID", (name, part)
        assert part["as_of_ms"] <= b["cutoff_ms"]
        if "history" in name:
            assert part["coverage"]["count"] == 10
    assert okx.check_bundle(b, config)["allow_new_risk"]
    assert not b["execution_enabled"]
    print(interval, e["status"], {k: p["as_of_utc"] for k, p in e["components"].items()})


@pytest.mark.integration
def test_okx_public_historical_derivatives_do_not_use_live():
    from tradingagents.dataflows.vendors.okx_derivatives import evidence_view

    config = {"market_mode": okx.MODE, "okx_interval": "4h", "okx_derivatives_enabled": True,
              "okx_derivatives_history_points": 10, "okx_max_lag_bars": 0}
    b = okx.build_bundle("BTC-USDT-SWAP", "2026-10-01", config)
    assert b["status"] == "VALID", okx.describe(b)
    parts = evidence_view(b["derivatives"])["components"]
    for name in ("oi_current", "funding_current", "mark_current", "index_current", "orderbook"):
        assert parts[name]["status"] == "DATA_UNAVAILABLE" and "data" not in parts[name]
    for name in ("oi_history", "funding_history", "mark_history", "index_history"):
        assert parts[name]["status"] == "VALID", (name, parts[name])
        assert parts[name]["as_of_ms"] <= b["cutoff_ms"]
