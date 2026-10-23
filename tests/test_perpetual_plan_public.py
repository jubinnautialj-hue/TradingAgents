"""Opt-in real public data; the choice here is explicitly simulated, no LLM/order."""

import copy
from datetime import UTC, datetime

import pytest

from tradingagents.agents.perpetual_schemas import PerpetualResearchResult
from tradingagents.dataflows import perpetual_plan as plan
from tradingagents.dataflows.vendors import okx
from tradingagents.reporting import write_report_tree


@pytest.mark.integration
def test_public_revalidation_and_schema_delivery(tmp_path):
    cfg = {
        "market_mode": okx.MODE,
        "okx_interval": "4h",
        "okx_multi_timeframe": True,
        "okx_derivatives_enabled": True,
        "okx_trade_plan_enabled": True,
        "okx_derivatives_history_points": 10,
        "okx_plan_taker_fee_bps": 5,
        "okx_max_lag_bars": 0,
    }
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    b = okx.build_bundle("BTC-USDT-SWAP", day, cfg)
    assert b["status"] == "VALID", okx.describe(b)
    original = copy.deepcopy(b)
    validation = plan.revalidate_live(b, cfg)
    assert validation["status"] == "VALID", validation["issues"]
    assert validation["research_snapshot_id"] == b["snapshot_id"]
    assert b == original
    state = {
        "company_of_interest": b["symbol"],
        "trade_date": day,
        "research_id": "public-test-simulated-choice",
        "market_snapshot": b,
        "position_context": "UNKNOWN",
        "portfolio_choice": plan.default_choice(
            "Simulated WAIT with real public evidence"
        ).model_dump(mode="json"),
        "entry_revalidation": validation,
        "llm_status": "AVAILABLE",
        "data_quality": "VALID",
        "allow_new_risk": False,
        "execution_enabled": False,
    }
    r, _ = plan.finalize_result(state, b, cfg)
    state.update(
        research_result=r.model_dump(mode="json"),
        final_trade_decision=plan.render_result(r),
        final_rating="REVIEW",
    )
    report = write_report_tree(state, b["symbol"], tmp_path, settings=cfg)
    parsed = PerpetualResearchResult.model_validate_json(
        (report.parent / "research_result.json").read_text(encoding="utf-8")
    )
    assert parsed.proposed_action.value == "WAIT" and not parsed.execution_enabled
    assert (report.parent / "research_result.schema.json").exists()
    print(
        "PUBLIC_REVALIDATION",
        validation["checked_at_utc"],
        "ORIGINAL_CUTOFF",
        r.research_cutoff_utc,
        "STATUS",
        r.plan_status,
        "MARKET",
        r.market_regime,
    )
