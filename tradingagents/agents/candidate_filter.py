"""D.4 output mode for existing roles, not an additional agent."""
import json
from typing import Literal
from pydantic import Field
from langchain_core.messages import AIMessage
from tradingagents.agents.perpetual_schemas import ResearchModel

PROMPT_VERSION = "historical-candidate-filter-v1"
EVIDENCE_IDS = {"1h", "4h", "1d", "candidate", "cost_assumptions", "missing_derivatives"}
INSTRUCTIONS = (
    "This is an exploratory historical candidate filter, not order execution. "
    "Use ONLY the supplied point-in-time evidence. No external knowledge, search, tools, memory, "
    "or later events. Missing OI/book/news/funding is UNKNOWN, not bullish or bearish. "
    "Evaluate the PROVIDED fixed candidate direction, not a replacement long/short strategy. "
    "Only ACCEPT, REJECT or ABSTAIN are permitted decisions; cite evidence IDs and concrete values. "
    "ACCEPT means observe the original future breakout/retest conditions, NOT enter now. "
    "You cannot alter direction, levels, size, timing, confirmation, costs or risk permissions. "
    "ABSTAIN means insufficient judgement, not a successful rejection. "
    "Never infer historical profits/outcomes or use knowledge of subsequent events. "
    "Keep the response concise (approximately 250 words or fewer). execution_enabled=False. "
)


class CandidateFilterChoice(ResearchModel):
    candidate_id: str
    decision: Literal["ACCEPT", "REJECT", "ABSTAIN"]
    evidence_ids: list[str] = Field(min_length=1, max_length=6)
    rationale: str = Field(min_length=10, max_length=4000)


def validate_choice(value, candidate_id):
    result = CandidateFilterChoice.model_validate(value)
    if result.candidate_id != candidate_id or not set(result.evidence_ids) <= EVIDENCE_IDS:
        raise ValueError("Unknown candidate/evidence reference")
    if result.decision == "ACCEPT" and ("candidate" not in result.evidence_ids or not set(result.evidence_ids) & {"1h", "4h", "1d"}):
        raise ValueError("ACCEPT requires candidate and timeframe evidence")
    return result


def market_filter_report(llm, state):
    prompt = ("You are the existing Market Analyst. " + INSTRUCTIONS +
              "Assess 1d regime, 4h structure, 1h confirmation, conflicts and missing evidence.\n" +
              json.dumps(state["candidate_filter_input"], ensure_ascii=False))
    response = llm.invoke(prompt)
    if getattr(response, "tool_calls", None) or not response.content:
        raise ValueError("Offline market report cannot call tools or be empty")
    return {"market_report": response.content, "messages": [response]}


def choose_filter(llm, state, role):
    evidence = state["candidate_filter_input"]
    prompt = (f"You are the existing {role}. " + INSTRUCTIONS +
              f"Evidence IDs: {sorted(EVIDENCE_IDS)}\n" +
              json.dumps({"input": evidence, "market": state.get("market_report", ""),
                  "debate": state["investment_debate_state"].get("history", ""),
                  "research_manager": state.get("investment_plan", ""),
                  "trader": state.get("trader_investment_plan", ""),
                  "risk_debate": state["risk_debate_state"].get("history", "")}, ensure_ascii=False))
    # No free-text repair or resampling to search for a favourable conclusion.
    value = llm.with_structured_output(CandidateFilterChoice).invoke(prompt)
    choice = validate_choice(value.model_dump() if hasattr(value, "model_dump") else value, evidence["candidate_id"])
    return choice.model_dump(), choice.model_dump_json(indent=2), True
