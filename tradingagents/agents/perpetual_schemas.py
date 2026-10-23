"""Research-only schemas. No exchange order fields or execution permissions."""

from decimal import Decimal
from enum import StrEnum
from math import isclose
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ResearchAction(StrEnum):
    OPEN_LONG = "OPEN_LONG_CANDIDATE"
    OPEN_SHORT = "OPEN_SHORT_CANDIDATE"
    WAIT = "WAIT"
    REDUCE_LONG = "REDUCE_LONG"
    REDUCE_SHORT = "REDUCE_SHORT"
    EXIT = "EXIT_CANDIDATE"


class DirectionCase(ResearchModel):
    thesis: str
    supporting_evidence_ids: list[str]
    opposing_evidence_ids: list[str]


class PerpetualChoice(ResearchModel):
    """LLM selects evidence IDs; it cannot supply a price, fee, validity or size."""

    proposed_action: ResearchAction
    candidate_id: Literal["LONG_BREAKOUT_RETEST", "SHORT_BREAKOUT_RETEST"] | None
    long_case: DirectionCase
    short_case: DirectionCase
    wait_case: str
    reasoning: str


class PriceEvidence(ResearchModel):
    price: float = Field(gt=0)
    source_snapshot_id: str
    rule: str
    reference: str


class RiskReward(ResearchModel):
    gross: float = Field(gt=0)
    net: float
    gross_loss_usdt: float = Field(gt=0)
    gross_profit_usdt: float = Field(gt=0)
    net_loss_usdt: float = Field(gt=0)
    net_profit_usdt: float


class EstimatedCosts(ResearchModel):
    fee_currency: Literal["USDT"] = "USDT"
    taker_fee_bps: float = Field(ge=0)
    maker_fee_bps: float | None = None
    fee_source: Literal["CALLER_ASSUMPTION_NOT_ACCOUNT_FEE"] = "CALLER_ASSUMPTION_NOT_ACCOUNT_FEE"
    fees_usdt_at_target: float = Field(ge=0)
    fees_usdt_at_stop: float = Field(ge=0)
    slippage_usdt_at_target: float = Field(ge=0)
    slippage_usdt_at_stop: float = Field(ge=0)
    adverse_funding_usdt: float = Field(ge=0)
    forecast_funding_cashflow_usdt: float
    forecast_settlement_times_ms: list[int]
    funding_scope: Literal["FORECAST_ESTIMATE_NOT_SETTLED_COST"] = (
        "FORECAST_ESTIMATE_NOT_SETTLED_COST"
    )
    assumptions: list[str]


class PerpetualResearchResult(ResearchModel):
    schema_version: Literal["okx-research-plan-v1"] = "okx-research-plan-v1"
    signal_timing: dict | None = None
    signal_state: Literal["PENDING_BREAKOUT", "PENDING_RETEST", "ENTRY_ELIGIBLE", "EXPIRED", "INVALIDATED", "WAIT"] = "WAIT"
    research_id: str
    exchange: Literal["OKX"] = "OKX"
    instrument: str
    research_cutoff_utc: str | None
    snapshot_id: str | None
    market_regime: dict
    timeframe_analysis: dict
    long_case: DirectionCase
    short_case: DirectionCase
    wait_case: str
    proposed_action: ResearchAction
    model_proposed_action: ResearchAction
    strategy_type: str | None
    position_context: Literal["UNKNOWN", "FLAT", "LONG", "SHORT"]
    entry_conditions: list[str]
    entry_reference: PriceEvidence | None
    stop_loss: PriceEvidence | None
    take_profit: list[PriceEvidence]
    invalidation_conditions: list[str]
    estimated_costs: EstimatedCosts | None
    risk_reward: RiskReward | None
    reference_contracts: float | None
    reference_base_quantity: float | None
    confidence_evidence: list[str]
    evidence: dict
    missing_data: list[str]
    blocking_reasons: list[str]
    data_quality: str
    plan_status: Literal[
        "VALID_CANDIDATE",
        "WAIT",
        "PLAN_INCOMPLETE",
        "RESEARCH_INVALID",
        "EXPIRED",
        "RESEARCH_CHANGED",
        "POSITION_REVIEW",
        "LLM_UNAVAILABLE",
    ]
    plan_valid: bool
    requires_revalidation: bool
    expires_at_utc: str | None
    research_expires_at_utc: str | None
    allow_new_risk: Literal[False] = False
    execution_enabled: Literal[False] = False

    @model_validator(mode="after")
    def coherent_plan(self):
        if self.signal_timing is not None:
            from tradingagents.dataflows.plan_lifecycle import PlanTiming, HOUR
            clock = PlanTiming.model_validate(self.signal_timing)
            if clock.hypothesis_expires_ms != ((clock.cutoff_ms + HOUR - 1) // HOUR + clock.confirmation_bars) * HOUR:
                raise ValueError("Signal timing window mismatch")
            if self.research_cutoff_utc is not None:
                from datetime import datetime
                if round(datetime.fromisoformat(self.research_cutoff_utc).timestamp() * 1000) != clock.cutoff_ms:
                    raise ValueError("Signal timing cutoff differs from research")
        if self.plan_valid:
            if (
                self.plan_status != "VALID_CANDIDATE"
                or self.proposed_action not in {ResearchAction.OPEN_LONG, ResearchAction.OPEN_SHORT}
                or not self.entry_reference
                or not self.stop_loss
                or not self.take_profit
                or not self.risk_reward
                or not self.estimated_costs
                or not self.reference_contracts
                or not self.reference_base_quantity
                or self.blocking_reasons
                or self.data_quality != "VALID"
            ):
                raise ValueError("Valid research candidate requires complete checked evidence")
            entry, stop, target = (
                self.entry_reference.price,
                self.stop_loss.price,
                self.take_profit[0].price,
            )
            if not (
                stop < entry < target
                if self.proposed_action == ResearchAction.OPEN_LONG
                else target < entry < stop
            ):
                raise ValueError("Invalid directional price ordering")
            if not self.requires_revalidation:
                raise ValueError("Conditional entry candidates always require future revalidation")
            spec = self.evidence["contract_spec"]
            size = Decimal(str(self.reference_contracts))
            if size < Decimal(spec["minSz"]) or size % Decimal(spec["lotSz"]):
                raise ValueError("Reference quantity off contract minimum/lot")
            if any(Decimal(str(p)) % Decimal(spec["tickSz"]) for p in (entry, stop, target)):
                raise ValueError("Price off contract tick")
            base, c, rr = self.reference_base_quantity, self.estimated_costs, self.risk_reward
            loss, profit = base * abs(entry - stop), base * abs(target - entry)
            expected = {
                "gross_loss_usdt": loss,
                "gross_profit_usdt": profit,
                "net_loss_usdt": loss
                + c.fees_usdt_at_stop
                + c.slippage_usdt_at_stop
                + c.adverse_funding_usdt,
                "net_profit_usdt": profit
                - c.fees_usdt_at_target
                - c.slippage_usdt_at_target
                - c.adverse_funding_usdt,
            }
            expected.update(
                gross=profit / loss, net=expected["net_profit_usdt"] / expected["net_loss_usdt"]
            )
            if (
                not isclose(
                    base,
                    float(size) * float(spec["ctVal"]) * float(spec.get("ctMult") or 1),
                    rel_tol=1e-9,
                )
                or not isclose(
                    c.fees_usdt_at_target,
                    base * (entry + target) * c.taker_fee_bps / 10000,
                    rel_tol=1e-9,
                    abs_tol=1e-8,
                )
                or not isclose(
                    c.fees_usdt_at_stop,
                    base * (entry + stop) * c.taker_fee_bps / 10000,
                    rel_tol=1e-9,
                    abs_tol=1e-8,
                )
                or any(
                    not isclose(getattr(rr, k), v, rel_tol=1e-9, abs_tol=1e-8)
                    for k, v in expected.items()
                )
            ):
                raise ValueError("Cost, quantity or risk/reward arithmetic mismatch")
        elif self.plan_status == "VALID_CANDIDATE":
            raise ValueError("Candidate status and validity disagree")
        return self
