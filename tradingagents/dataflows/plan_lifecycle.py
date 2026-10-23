"""Pure, replayable research signal clock. No I/O, fills, ledger or execution.

Replay normalized CLOSED UTC hourly bars, never an LLM-supplied state.
A signal is not an entry authorization. Historical inputs remain point-in-time.
"""
from math import isfinite
from typing import Literal
from pydantic import Field
from tradingagents.agents.perpetual_schemas import ResearchModel

HOUR = 3_600_000


class PlanTiming(ResearchModel):
    cutoff_ms: int = Field(gt=0)
    hypothesis_expires_ms: int = Field(gt=0)
    confirmation_bars: int = Field(ge=2, le=48)
    market_data_ttl_seconds: int = Field(gt=0, le=900)
    revalidation_ttl_seconds: int = Field(gt=0, le=300)
    bar_interval: Literal["1h"] = "1h"
    rules_version: Literal["breakout-retest-v1"] = "breakout-retest-v1"


def timing(cutoff_ms, config):
    bars = int(config.get("okx_plan_confirmation_bars", 6))
    # Window counts complete bars opening at/after cutoff; partial cutoff bar is excluded.
    first_open = ((cutoff_ms + HOUR - 1) // HOUR) * HOUR
    return PlanTiming(cutoff_ms=cutoff_ms,
        hypothesis_expires_ms=first_open + bars * HOUR,
        confirmation_bars=bars,
        market_data_ttl_seconds=int(config.get("okx_derivatives_ttl_seconds", 300)),
        revalidation_ttl_seconds=int(config.get("okx_entry_revalidation_ttl_seconds", 60)))


class SignalResult(ResearchModel):
    state: Literal["PENDING_BREAKOUT", "PENDING_RETEST", "ENTRY_ELIGIBLE", "EXPIRED", "INVALIDATED", "WAIT"]
    reason: str
    breakout_confirmed_ms: int | None = None
    retest_confirmed_ms: int | None = None
    next_tradable_ms: int | None = None
    requires_revalidation: Literal[True] = True
    allow_new_risk: Literal[False] = False
    execution_enabled: Literal[False] = False


def evaluate_signal(plan, clock, bars, as_of_ms, *, structure_valid=True, data_quality="VALID"):
    """Replay contiguous bars [ts, open/high/low/close, confirm] in UTC.

    Confirm uses close > boundary (short: <); a later bar must touch boundary
    and close on the breakout side. A close through stop invalidates first.
    next_tradable_ms is the NEXT bar's open, never the confirming bar's open.
    ENTRY_ELIGIBLE describes only technical confirmation, not fresh evidence.
    """
    clock = PlanTiming.model_validate(clock)
    if clock.hypothesis_expires_ms != ((clock.cutoff_ms + HOUR - 1)//HOUR + clock.confirmation_bars)*HOUR:
        raise ValueError("Inconsistent confirmation window")
    if as_of_ms < clock.cutoff_ms:
        raise ValueError("Observation precedes research")
    if data_quality != "VALID":
        return SignalResult(state="WAIT", reason="Required data quality invalid")
    if not structure_valid:
        return SignalResult(state="INVALIDATED", reason="Original price structure changed")
    long = plan["direction"] == "LONG"
    if plan["direction"] not in {"LONG", "SHORT"}:
        raise ValueError("Unknown direction")
    entry, stop, target = [float(plan[k]["price"]) for k in ("entry", "stop", "target")]
    if not all(isfinite(x) and x > 0 for x in (entry, stop, target)) or not (stop < entry < target if long else target < entry < stop):
        raise ValueError("Invalid plan prices")
    state, breakout, retest = "PENDING_BREAKOUT", None, None
    first_open = ((clock.cutoff_ms + HOUR - 1)//HOUR)*HOUR
    previous = None
    for bar in bars:
        ts = bar["ts"]
        if type(ts) is not int or ts % HOUR:
            raise ValueError("Unaligned UTC hourly bar")
        if previous is not None and ts != previous + HOUR:
            raise ValueError("Duplicate, out-of-order or missing bar")
        if previous is None and ts != first_open:
            raise ValueError("Confirmation coverage must start at first eligible bar")
        previous = ts
        close_ms = ts + HOUR
        if close_ms > as_of_ms or bar["confirm"] != "1":
            raise ValueError("Future or unconfirmed bar")
        o, h, l, c = [float(bar[k]) for k in ("open", "high", "low", "close")]
        if not all(isfinite(v) and v > 0 for v in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
            raise ValueError("Invalid OHLC")
        if close_ms > clock.hypothesis_expires_ms:
            break
        if c <= stop if long else c >= stop:
            return SignalResult(state="INVALIDATED", reason="Confirmed close broke original stop structure", breakout_confirmed_ms=breakout)
        if state == "PENDING_BREAKOUT" and (c > entry if long else c < entry):
            state, breakout = "PENDING_RETEST", close_ms
        elif state == "PENDING_RETEST" and (l <= entry < c if long else h >= entry > c):
            state, retest = "ENTRY_ELIGIBLE", close_ms
    # Expiry dominates even a previously confirmed signal; no fill is implied.
    if as_of_ms >= clock.hypothesis_expires_ms:
        state = "EXPIRED"
    elif (previous + HOUR if previous is not None else first_open) < (as_of_ms // HOUR) * HOUR:
        state = "WAIT"
    return SignalResult(state=state, reason="Deterministic closed-bar replay; fresh entry validation still required",
        breakout_confirmed_ms=breakout, retest_confirmed_ms=retest,
        next_tradable_ms=retest if state == "ENTRY_ELIGIBLE" else None)


class HistoricalAssumptions(ResearchModel):
    mode: Literal["CONSERVATIVE_CANDLE_SIMULATION"] = "CONSERVATIVE_CANDLE_SIMULATION"
    taker_fee_bps: float = Field(ge=0, le=1000)
    slippage_bps_per_fill: float = Field(ge=0, le=1000)
    requires_orderbook: bool = False
    requires_oi: bool = False
    fill_rule: Literal["NEXT_BAR_OPEN_ADVERSE_SLIPPAGE"] = "NEXT_BAR_OPEN_ADVERSE_SLIPPAGE"
    same_bar_exit_rule: Literal["STOP_FIRST"] = "STOP_FIRST"
    funding_rule: Literal["REALIZED_ONLY_OR_BLOCK"] = "REALIZED_ONLY_OR_BLOCK"


def prepare_historical_signal(bundle, config, candidate_id, assumptions):
    """Prepare an offline signal template, NOT a valid live plan or a backtest.

    Caller must supply a historical validated bundle produced at its own cutoff.
    Cost numbers are explicit assumptions, never current book/fee substitutes.
    Contract validity/margin/funding coverage must still be checked by stage D.
    """
    from tradingagents.dataflows.perpetual_plan import candidates
    assumptions = HistoricalAssumptions.model_validate(assumptions)
    if not bundle.get("historical") or not bundle.get("snapshot_id") or bundle.get("status") != "VALID" or not bundle.get("allow_new_risk"):
        raise ValueError("Validated historical snapshot required")
    cutoff = bundle["cutoff_ms"]
    for child in bundle.get("timeframes", {}).values():
        if child.get("cutoff_ms") != cutoff or child.get("latest_close_ms", cutoff+1) > cutoff or child.get("status") != "VALID":
            raise ValueError("Historical timeframe quality/cutoff mismatch")
    if set(bundle.get("timeframes", {})) != {"1h", "4h", "1d"}:
        raise ValueError("Three historical timeframes required")
    components = bundle.get("derivatives", {}).get("components", {})
    for name, part in components.items():
        if part.get("data") and (part.get("as_of_ms") is None or part["as_of_ms"] > cutoff):
            raise ValueError("Derivative observation exceeds historical cutoff")
        if name.endswith("_current") or name == "orderbook":
            if part.get("data"):
                raise ValueError("Current derivative observations forbidden in historical template")
    for needed, name in ((assumptions.requires_orderbook, "orderbook_history"), (assumptions.requires_oi, "oi_history")):
        if needed:
            part = components.get(name, {})
            if part.get("status") != "VALID" or not part.get("data") or part.get("as_of_ms", cutoff+1) > cutoff:
                raise ValueError("Required historical strategy evidence unavailable: " + name)
    from tradingagents.dataflows.vendors import okx
    checked = okx.check_bundle(bundle, config, bundle.get("symbol"))
    if not checked.get("allow_new_risk"):
        raise ValueError("Historical source snapshot failed existing quality gate")
    templates = candidates(checked, config)
    if candidate_id not in templates:
        raise ValueError("Verified candidate unavailable")
    return {"mode": assumptions.mode, "snapshot_id": bundle["snapshot_id"], "cutoff_ms": cutoff,
        "candidate": templates[candidate_id], "timing": timing(cutoff, config).model_dump(),
        "assumptions": assumptions.model_dump(), "simulated_fills_not_actual_orders": True,
        "plan_valid": False, "requires_revalidation": True, "allow_new_risk": False,
        "execution_enabled": False, "remaining_checks": ["historical contract specification", "margin/liquidation rules", "realized funding coverage", "signal replay and next-bar fill"]}


def entry_evidence_ready(original_bundle, signal, validation, current_bundle, config, now_ms):
    """Pure live gate, requires newly checked full market bundle AFTER signal.

    No account or execution permission is granted, including when True.
    Uses existing snapshot binding/drift detection and original quality rules.
    """
    from tradingagents.dataflows.perpetual_plan import compare_revalidation
    from tradingagents.dataflows.vendors import okx
    signal = SignalResult.model_validate(signal)
    if signal.state != "ENTRY_ELIGIBLE" or signal.retest_confirmed_ms is None:
        return False
    clock = timing(original_bundle["cutoff_ms"], config)
    ttl = clock.revalidation_ttl_seconds * 1000
    checked = validation.get("checked_at_ms", 0)
    if not signal.retest_confirmed_ms <= checked <= now_ms < min(checked + ttl, clock.hypothesis_expires_ms, validation.get("expires_at_ms", 0)):
        return False
    if compare_revalidation(original_bundle, validation, config):
        return False
    if current_bundle.get("historical") or current_bundle.get("cutoff_ms", 0) < signal.retest_confirmed_ms:
        return False
    if current_bundle.get("fetched_at_ms", 0) < checked or now_ms - current_bundle.get("fetched_at_ms", 0) >= ttl:
        return False
    if current_bundle.get("symbol") != original_bundle.get("symbol"):
        return False
    verified = okx.check_bundle(current_bundle, config, original_bundle["symbol"])
    return bool(verified.get("allow_new_risk"))
