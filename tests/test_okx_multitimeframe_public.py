"""Explicit opt-in, public GET only; independent periods under one cutoff."""

from datetime import UTC, datetime

import pytest

from tradingagents.dataflows.vendors import okx
from tradingagents.dataflows.vendors.okx_derivatives import evidence_view


@pytest.mark.integration
@pytest.mark.parametrize("historical", [False, True])
def test_public_joint_snapshot(historical):
    config = {"market_mode": okx.MODE, "okx_interval": "4h", "okx_multi_timeframe": True,
              "okx_derivatives_enabled": True, "okx_derivatives_history_points": 10,
              "okx_max_lag_bars": 0}
    day = "2026-10-01" if historical else datetime.now(UTC).strftime("%Y-%m-%d")
    b = okx.build_bundle("BTC-USDT-SWAP", day, config)
    assert b["status"] == "VALID", okx.describe(b)
    assert okx.check_bundle(b, config, b["symbol"], day)["allow_new_risk"]
    assert not b["execution_enabled"]
    assert set(b["timeframes"]) == {"1h", "4h", "1d"}
    for p, c in b["timeframes"].items():
        step = okx.BARS[p][1]
        assert len(c["records"]) == 400 and c["status"] == "VALID"
        assert c["cutoff_ms"] == b["cutoff_ms"]
        assert all(r["ts"] + step <= b["cutoff_ms"] for r in c["records"])
        assert c["latest_close_ms"] == b["cutoff_ms"] // step * step
        # Independent manual SMA check, not only the rendered LLM text.
        assert b["timeframe_analysis"][p]["indicators"]["close_200_sma"] == pytest.approx(
            sum(r["Close"] for r in c["records"][-200:]) / 200)
    e = evidence_view(b["derivatives"])
    assert e["research_cutoff_ms"] == b["cutoff_ms"]
    if historical:
        assert b["historical"]
        assert e["components"]["oi_current"]["status"] == "DATA_UNAVAILABLE"
        assert e["components"]["orderbook"]["status"] == "DATA_UNAVAILABLE"
    else:
        for name in ("oi_current", "funding_current", "orderbook", "mark_current", "index_current"):
            assert e["components"][name]["status"] == "VALID", e["components"][name]
    print("HISTORICAL", historical, "CUTOFF", b["cutoff_ms"], "CONFLUENCE", b["confluence"],
          "LATEST", {p: c["latest_close_ms"] for p, c in b["timeframes"].items()})
