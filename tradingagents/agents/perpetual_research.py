"""Stage-C branches of existing agents; no new agent or workflow node."""

import json

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.perpetual_schemas import PerpetualChoice
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.perpetual_plan import REFS, default_choice


def choose_research(llm, state, role):
    if state.get("candidate_filter_mode"):
        from tradingagents.agents.candidate_filter import choose_filter
        return choose_filter(llm, state, role)
    context = get_instrument_context_from_state(state)
    cfg = get_config()
    cost_context = {
        k: cfg.get(k)
        for k in (
            "okx_plan_taker_fee_bps",
            "okx_plan_maker_fee_bps",
            "okx_plan_horizon_hours",
            "okx_plan_min_net_rr",
        )
    }
    prompt = (
        f"You are the existing {role} for OKX perpetual RESEARCH only.\n{context}\n"
        "Independently assess LONG, SHORT and WAIT even with no holdings. Never map legacy Sell/Underweight "
        "to an opening short. Unknown holdings are UNKNOWN, not a long book or a flat book. "
        "Use OPEN_LONG_CANDIDATE, OPEN_SHORT_CANDIDATE, WAIT, REDUCE_LONG, REDUCE_SHORT or EXIT_CANDIDATE. "
        "Reductions/exits require explicit matching caller position context. You do NOT decide numerical levels, "
        "fees, validity, leverage, quantity, risk permission or execution. Select only a provided candidate_id; "
        "WAIT is appropriate if structural, directional or cost evidence is missing. No forced opportunities. "
        "Candidate triggers have NOT executed; they require future closed-bar confirmation and another validation. "
        "Do not promise a fill or present scenario quantity as account sizing.\n"
        f"Position context: {state.get('position_context', 'UNKNOWN')}\n"
        f"Explicit cost assumptions (not account fees): {json.dumps(cost_context)}. "
        "Unknown actual account fees/holdings do not prevent conditional research; never confuse an explicit scenario assumption with a fetched account fee. Program cost validation still applies.\n"
        f"Allowed evidence IDs: {sorted(REFS)}\n"
        f"Deterministic candidate templates: {json.dumps(state.get('candidate_plans', {}), ensure_ascii=False)}\n"
        f"Market research: {state.get('market_report', '')}\n"
        f"Bull/Bear debate: {state.get('investment_debate_state', {}).get('history', '')}\n"
        f"Research Manager: {state.get('investment_plan', '')}\n"
        f"Trader: {state.get('trader_investment_plan', '')}\n"
        f"Risk debate: {state.get('risk_debate_state', {}).get('history', '')}\n"
        + NO_EXTERNAL_TOOLS
        + get_language_instruction()
    )
    response = invoke_structured(bind_structured(llm, PerpetualChoice, role), prompt, role)
    parsed = True
    try:
        if response is None:
            raise ValueError("No typed response")
        choice = PerpetualChoice.model_validate(
            response.model_dump() if hasattr(response, "model_dump") else response
        )
    except (ValueError, TypeError):
        parsed = False
        choice = default_choice(
            "Schema-validated assessment unavailable; no legacy action conversion"
        )
    text = (
        f"**Research Action**: {choice.proposed_action.value}\n\n"
        "Research suggestion only; execution disabled. Prices/costs are program-generated candidates, not orders.\n\n"
        + choice.model_dump_json(indent=2)
    )
    return choice.model_dump(mode="json"), text, parsed
