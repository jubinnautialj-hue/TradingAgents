"""Deterministic preflight/final research gates shared by CLI and Python graphs."""

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.vendors.okx import build_bundle, check_bundle, describe, enabled


def preflight(state):
    config = get_config()
    bundle = build_bundle(state["company_of_interest"], state["trade_date"], config)
    bundle = check_bundle(bundle, config, state["company_of_interest"], state["trade_date"])
    update = {"market_snapshot": bundle, "data_quality": bundle["status"],
              "allow_new_risk": bundle["allow_new_risk"], "execution_enabled": False}
    if config.get("okx_trade_plan_enabled", False):
        from uuid import uuid4

        from tradingagents.dataflows.perpetual_plan import candidates

        update.update(plan_mode=True, research_id=state.get("research_id") or uuid4().hex,
                      candidate_plans=candidates(bundle, config))
    if not bundle["allow_new_risk"]:
        update.update(blocked(bundle))
        update["workflow_status"] = "BLOCKED_DATA"
    return update


def blocked(bundle):
    report = describe(bundle) + "\n\nRESEARCH_INVALID — not usable for new trading risk. No position changes are performed."
    return {"market_report": report, "final_trade_decision": report, "final_rating": "REVIEW",
            "allow_new_risk": False, "execution_enabled": False}


def model_failure_update(failure, bundle):
    details = "\n".join(f"- {key}: {value}" for key, value in failure.items())
    report = (describe(bundle) + "\n\n## LLM_UNAVAILABLE\n\n" + details
              + "\n\nRESEARCH_INVALID: model call failed; no completed research recommendation. "
                "Partial responses are discarded. No new risk permitted; no positions changed.")
    return {"llm_status": "LLM_UNAVAILABLE", "workflow_status": "FAILED",
            "llm_failure": failure, "allow_new_risk": False, "execution_enabled": False,
            "final_rating": "REVIEW", "final_trade_decision": report}


def final_gate(state, config=None, *, revalidate=False):
    config = get_config() if config is None else config
    if not enabled(config):
        return {}
    bundle = check_bundle(state.get("market_snapshot", {}), config,
                          state["company_of_interest"], state["trade_date"])
    update = {"market_snapshot": bundle, "data_quality": bundle.get("status", "RESEARCH_INVALID"),
              "allow_new_risk": bundle["allow_new_risk"], "execution_enabled": False}
    if not bundle["allow_new_risk"]:
        update.update(blocked(bundle))
    if state.get("llm_status") == "LLM_UNAVAILABLE":
        update.update(model_failure_update(state.get("llm_failure", {}), bundle))
    elif state.get("llm_status") == "AVAILABLE":
        update["workflow_status"] = "COMPLETED" if bundle["allow_new_risk"] else "BLOCKED_DATA"
    if config.get("okx_trade_plan_enabled", False):
        from tradingagents.dataflows.perpetual_plan import finalize_result, render_result

        result, validation = finalize_result({**state, **update}, bundle, config, refresh=revalidate)
        update.update(research_result=result.model_dump(mode="json"), entry_revalidation=validation,
                      final_trade_decision=render_result(result), final_rating="REVIEW", allow_new_risk=False,
                      execution_enabled=False)
    return update
