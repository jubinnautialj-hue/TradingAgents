"""Deterministic conditional research plans, never executable orders.

LLMs select existing candidates. All numeric plan evidence is recomputed here;
live revalidation is a separate snapshot and never changes the research cutoff.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from tradingagents.agents.perpetual_schemas import (
    DirectionCase,
    EstimatedCosts,
    PerpetualChoice,
    PerpetualResearchResult,
    PriceEvidence,
    ResearchAction,
    RiskReward,
)
from tradingagents.dataflows.vendors import okx, okx_derivatives as d

OPEN = {ResearchAction.OPEN_LONG, ResearchAction.OPEN_SHORT}
REFS = {
    "1d:trend",
    "4h:structure",
    "1h:momentum",
    "derivatives:oi_history",
    "derivatives:funding_history",
    "derivatives:orderbook",
}
CRITICAL = (
    "oi_current",
    "funding_current",
    "mark_current",
    "index_current",
    "trade_current",
    "orderbook",
)


def enabled(config):
    return okx.enabled(config) and config.get("okx_trade_plan_enabled", False)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def round_tick(value, tick, up):
    value, tick = Decimal(str(value)), Decimal(str(tick))
    return float(
        (value / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick
    )


def position_context(portfolio, symbol):
    if portfolio is None:
        return "UNKNOWN"
    positions = [p for p in portfolio.positions if p.ticker.upper() == symbol.upper()]
    if len(positions) > 1 or any(not math.isfinite(p.quantity) for p in positions):
        return "UNKNOWN"  # Never net an unknown hedge book.
    quantity = positions[0].quantity if positions else 0
    return "LONG" if quantity > 0 else "SHORT" if quantity < 0 else "FLAT"


def candidates(bundle, config):
    """Two conditional breakout/retest structures using ONLY verified levels."""
    from tradingagents.dataflows.plan_lifecycle import timing

    plans = {}
    if not bundle.get("allow_new_risk") or not bundle.get("multi_timeframe"):
        return plans
    a = bundle["timeframe_analysis"]
    structure, trigger = a["4h"], a["1h"]
    tick = bundle["instrument"]["tickSz"]
    extension = d.number(config.get("okx_plan_target_atr", 3), positive=True)
    buffer = d.number(config.get("okx_plan_stop_atr_buffer", 0.25), nonnegative=True)
    if not 1 <= int(config.get("okx_plan_ttl_seconds", 900)) <= 900 or extension > 10 or buffer > 2:
        raise ValueError("Plan lifetime/ATR settings outside bounded research limits")
    for side, name in (("LONG", "LONG_BREAKOUT_RETEST"), ("SHORT", "SHORT_BREAKOUT_RETEST")):
        long = side == "LONG"
        key = "support_candidates" if long else "resistance_candidates"
        levels = structure[key]
        if not levels:
            continue
        edge = structure["previous_range"]["high" if long else "low"]
        entry = round_tick(edge + (float(tick) if long else -float(tick)), tick, long)
        stop_ref = max(levels) if long else min(levels)
        stop = round_tick(
            stop_ref + (-1 if long else 1) * buffer * trigger["indicators"]["atr"], tick, not long
        )
        target = round_tick(
            entry + (1 if long else -1) * extension * structure["indicators"]["atr"], tick, not long
        )
        snapshot = bundle["timeframes"]["4h"]["snapshot_id"]
        plans[name] = {
            "direction": side,
            "strategy_type": "CONFIRMED_BREAKOUT_RETEST",
            "timing": timing(bundle["cutoff_ms"], config).model_dump(),
            "entry": {
                "price": entry,
                "source_snapshot_id": snapshot,
                "rule": "previous 4h range boundary plus/minus one tick; conservative tick rounding",
                "reference": "4h:previous_range",
            },
            "stop": {
                "price": stop,
                "source_snapshot_id": snapshot,
                "rule": f"confirmed 4h pivot level buffered by {buffer} x 1h ATR; round away from entry",
                "reference": f"4h:{key};1h:atr",
            },
            "target": {
                "price": target,
                "source_snapshot_id": snapshot,
                "rule": f"entry plus/minus {extension} x 4h ATR; round toward entry; not guaranteed resistance/support",
                "reference": "4h:atr",
            },
            "entry_conditions": [
                f"A future confirmed UTC 1h close {'above' if long else 'below'} {entry}",
                "Subsequent closed-bar retest must hold the same boundary; reject intrabar-only touches",
                "Revalidate all three timeframes, prices, costs, liquidity and position context before any new-risk decision",
            ],
            "invalidation_conditions": [
                f"Confirmed 1h close {'below' if long else 'above'} pivot invalidation {stop}",
                "A required timeframe fails, main regime changes, liquidity degrades or plan expires",
            ],
        }
    return plans


def validate_prices(plan, info):
    long = plan["direction"] == "LONG"
    entry, stop, target = [
        d.number(plan[k]["price"], positive=True) for k in ("entry", "stop", "target")
    ]
    if not (stop < entry < target if long else target < entry < stop):
        raise ValueError("Stop/entry/target direction invalid")
    tick = Decimal(info["tickSz"])
    if any(Decimal(str(v)) % tick for v in (entry, stop, target)):
        raise ValueError("Plan price is off instrument tick")
    return entry, stop, target


def calculate_costs(plan, info, components, config, start_ms):
    """Conservative taker fee + displayed-depth proxy + forecast funding.

    Exit depth is a CURRENT proxy, not a historical/future fill guarantee.
    Unknown rates beyond the known upcoming event are not fabricated.
    """
    entry, stop, target = validate_prices(plan, info)
    fee = config.get("okx_plan_taker_fee_bps")
    if fee is None:
        raise ValueError("Explicit taker fee assumption required")
    fee = d.number(fee, nonnegative=True)
    maker = config.get("okx_plan_maker_fee_bps")
    if maker is not None:
        maker = d.number(maker)
    notional = d.number(config.get("okx_liquidity_notional_usdt", 10000), positive=True)
    lot = Decimal(info["lotSz"])
    contracts = (Decimal(str(notional / entry / d.multiplier(info))) / lot).to_integral_value(
        rounding=ROUND_FLOOR
    ) * lot
    if contracts < Decimal(info["minSz"]) or contracts % lot:
        raise ValueError("Reference size below minSz or off lotSz")
    base = float(contracts) * d.multiplier(info)
    book = components["orderbook"]["data"]
    if book["abnormal_spread"]:
        raise ValueError("Abnormal spread")
    long = plan["direction"] == "LONG"
    entry_side, exit_side = ("asks", "bids") if long else ("bids", "asks")
    entry_fill = d.estimate_fill(book[entry_side], base, 1 if long else -1)
    exit_fill = d.estimate_fill(book[exit_side], base, -1 if long else 1)
    if entry_fill["status"] != "VALID" or exit_fill["status"] != "VALID":
        raise ValueError("Insufficient displayed depth for reference size")
    mid = book["mid"]
    entry_bps = max(0, (1 if long else -1) * (entry_fill["vwap"] / mid - 1))
    exit_bps = max(0, (-1 if long else 1) * (exit_fill["vwap"] / mid - 1))
    funding = components["funding_current"]["data"]
    end = start_ms + int(d.number(config.get("okx_plan_horizon_hours", 4), positive=True) * 3600000)
    if end >= funding["next_funding_time_ms"] or funding["funding_time_ms"] <= start_ms:
        raise ValueError(
            "Funding schedule/rate beyond known upcoming event; no invented future settlements"
        )
    events = [funding["funding_time_ms"]] if funding["funding_time_ms"] <= end else []
    cashflow = d.funding_cashflow(base, entry, funding["forecast_rate"], plan["direction"]) * len(
        events
    )
    adverse = max(0, -cashflow)  # Never inflate net reward with forecast receipts.
    costs = EstimatedCosts(
        taker_fee_bps=fee,
        maker_fee_bps=maker,
        fees_usdt_at_target=base * (entry + target) * fee / 10000,
        fees_usdt_at_stop=base * (entry + stop) * fee / 10000,
        slippage_usdt_at_target=base * (entry * entry_bps + target * exit_bps),
        slippage_usdt_at_stop=base * (entry * entry_bps + stop * exit_bps),
        adverse_funding_usdt=adverse,
        forecast_funding_cashflow_usdt=cashflow,
        forecast_settlement_times_ms=events,
        assumptions=[
            "Caller fee assumption, NOT fetched account fees; both legs conservatively taker",
            "Reference quantity, not an order or portfolio sizing decision",
            "Current full-depth costs versus mid proxy both future legs; cancellation/latency excluded",
            "Funding uses entry notional proxy and only known upcoming event; forecast income not credited to RR",
            "Trigger timing/holding period can change fees and funding; recompute before any decision",
        ],
    )
    loss, profit = base * abs(entry - stop), base * abs(target - entry)
    net_loss = loss + costs.fees_usdt_at_stop + costs.slippage_usdt_at_stop + adverse
    net_profit = profit - costs.fees_usdt_at_target - costs.slippage_usdt_at_target - adverse
    rr = RiskReward(
        gross=profit / loss,
        net=net_profit / net_loss,
        gross_loss_usdt=loss,
        gross_profit_usdt=profit,
        net_loss_usdt=net_loss,
        net_profit_usdt=net_profit,
    )
    return costs, rr, float(contracts), base


def revalidate_live(bundle, config, client=None):
    """Separate public-only observation; no replacement of original evidence."""
    client = client or okx.OKXPublicClient()
    result = {
        "status": "DATA_UNAVAILABLE",
        "research_snapshot_id": bundle.get("snapshot_id"),
        "instrument": bundle["symbol"],
        "components": {},
        "issues": [],
        "execution_enabled": False,
    }
    try:
        info = client.instrument(bundle["symbol"])
        if any(
            info.get(k) != bundle["instrument"].get(k)
            for k in ("ctVal", "ctMult", "ctValCcy", "tickSz", "lotSz", "minSz")
        ):
            raise ValueError("Instrument specifications changed")
        symbol = bundle["symbol"]
        for name, path in (
            ("oi_current", d.OI),
            ("funding_current", d.FUNDING),
            ("mark_current", d.MARK),
            ("index_current", d.INDEX),
            ("trade_current", d.TICKER),
            ("orderbook", d.BOOK),
        ):
            inst = info["instFamily"] if name == "index_current" else symbol
            params = {"instId": inst}
            if name in {"oi_current", "mark_current"}:
                params["instType"] = "SWAP"
            if name == "orderbook":
                params["sz"] = "100"
            rows = client.get(path, params)
            ceiling = okx.utc_ms() + 60000
            if name == "orderbook":
                data = d.parse_book(
                    rows[0],
                    info,
                    ceiling,
                    config.get("okx_liquidity_notional_usdt", 10000),
                    d.number(config.get("okx_max_spread_bps", 50), positive=True),
                )
            else:
                row = d.single(
                    rows, inst, name in {"oi_current", "funding_current", "mark_current"}
                )
                if name == "oi_current":
                    data = d.parse_oi(row, info, ceiling)
                elif name == "funding_current":
                    data = d.parse_funding(row, ceiling)
                else:
                    field = {
                        "mark_current": "markPx",
                        "index_current": "idxPx",
                        "trade_current": "last",
                    }[name]
                    data = {
                        "ts": d.timestamp(row["ts"], ceiling),
                        "price": d.number(row[field], positive=True),
                    }
            result["components"][name] = {
                "status": "VALID",
                "source": path,
                "data": data,
                "as_of_ms": data["ts"],
                "expires_at_ms": data["ts"]
                + int(config.get("okx_derivatives_ttl_seconds", 300)) * 1000,
            }
        now = int(client.get("/api/v5/public/time")[0]["ts"])
        if abs(now - okx.utc_ms()) > 60000:
            raise ValueError("Server/local clock mismatch")
        if any(
            p["as_of_ms"] > now or p["expires_at_ms"] < now for p in result["components"].values()
        ):
            raise ValueError("Fresh observation timestamps invalid")
        result.update(
            status="VALID",
            checked_at_ms=now,
            checked_at_utc=iso(now),
            expires_at_ms=min(p["expires_at_ms"] for p in result["components"].values()),
        )
    except Exception as exc:
        result["issues"] = [f"Public revalidation failed: {type(exc).__name__}"]
    result["validation_id"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    return result


def compare_revalidation(bundle, validation, config):
    """Changes are measured against ORIGINAL observations, not successive refreshes."""
    if validation.get("status") != "VALID":
        return ["Fresh public evidence unavailable"]
    content = {k: v for k, v in validation.items() if k != "validation_id"}
    if (
        validation.get("research_snapshot_id") != bundle.get("snapshot_id")
        or validation.get("instrument") != bundle["symbol"]
        or hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest()
        != validation.get("validation_id")
    ):
        return ["Revalidation identity/integrity mismatch"]
    old = bundle.get("derivatives", {}).get("components", {})
    fresh = validation["components"]
    changes = []
    try:
        atr = bundle["timeframe_analysis"]["1h"]["indicators"]["atr"]
        if any(
            abs(fresh[name]["data"]["price"] - old[name]["data"]["price"]) > 0.5 * atr
            for name in ("trade_current", "mark_current", "index_current")
        ):
            changes.append("Trade/mark/index price moved more than 0.5 original 1h ATR")
        oi = old["oi_current"]["data"]["contracts"]
        if oi <= 0 or abs(fresh["oi_current"]["data"]["contracts"] / oi - 1) > 0.05:
            changes.append("OI changed more than 5% or original denominator unavailable")
        of, nf = old["funding_current"]["data"], fresh["funding_current"]["data"]
        if (
            of["funding_time_ms"] != nf["funding_time_ms"]
            or abs(of["forecast_rate"] - nf["forecast_rate"]) > 0.0001
        ):
            changes.append("Upcoming funding event changed or forecast moved more than 1bp")
        ob, nb = old["orderbook"]["data"], fresh["orderbook"]["data"]
        if nb["abnormal_spread"] or abs(nb["imbalance"] - ob["imbalance"]) > 0.3:
            changes.append("Liquidity spread/imbalance changed materially")
    except (KeyError, TypeError, ZeroDivisionError):
        changes.append("Original comparison evidence missing")
    return changes


def default_choice(reason):
    case = DirectionCase(
        thesis="Evidence not assessed", supporting_evidence_ids=[], opposing_evidence_ids=[]
    )
    return PerpetualChoice(
        proposed_action=ResearchAction.WAIT,
        candidate_id=None,
        long_case=case,
        short_case=case,
        wait_case=reason,
        reasoning=reason,
    )


def finalize_result(state, bundle, config, *, refresh=False):
    """Rebuild and validate delivery every time; never trust LLM validity fields."""
    from tradingagents.dataflows.plan_lifecycle import timing

    bundle = okx.check_bundle(bundle, config, state["company_of_interest"], state.get("trade_date"))
    now = okx.utc_ms()
    missing = []
    try:
        raw = state.get("portfolio_choice")
        choice = (
            PerpetualChoice.model_validate(raw)
            if raw
            else default_choice("No schema-validated final model assessment")
        )
        if not raw:
            missing.append("No schema-validated final model assessment")
    except Exception:
        choice = default_choice("Invalid final model schema; legacy SELL is not an opening short")
        missing.append("Invalid final model schema")
    action, status = choice.proposed_action, "WAIT"
    plans = candidates(bundle, config)
    selected = None
    costs = rr = contracts = base = None
    validation = state.get("entry_revalidation", {})
    snapshot = bundle.get("snapshot_id")
    expiry = bundle.get("cutoff_ms", now) + int(config.get("okx_plan_ttl_seconds", 900)) * 1000
    research_expiry = (
        bundle.get("fetched_at_ms", now) + int(config.get("okx_bundle_ttl_seconds", 900)) * 1000
    )
    expiry = min(expiry, research_expiry)
    position = state.get("position_context", "UNKNOWN")
    model_refs = (
        choice.long_case.supporting_evidence_ids
        + choice.long_case.opposing_evidence_ids
        + choice.short_case.supporting_evidence_ids
        + choice.short_case.opposing_evidence_ids
    )
    if set(model_refs) - REFS:
        missing.append("Model used unknown evidence IDs")
    if state.get("portfolio_schema_valid") is False:
        missing.append("Final model schema unavailable")
    if not bundle.get("allow_new_risk"):
        status, action = "RESEARCH_INVALID", ResearchAction.WAIT
        missing.append("Core multi-timeframe research invalid")
    elif state.get("llm_status") == "LLM_UNAVAILABLE":
        status, action = "LLM_UNAVAILABLE", ResearchAction.WAIT
        missing.append("Model workflow unavailable")
    elif now >= expiry:
        status, action = "EXPIRED", ResearchAction.WAIT
        missing.append("Research plan lifetime expired")
    elif action in OPEN:
        expected = (
            "LONG_BREAKOUT_RETEST"
            if action == ResearchAction.OPEN_LONG
            else "SHORT_BREAKOUT_RETEST"
        )
        if choice.candidate_id != expected or expected not in plans:
            missing.append("Missing or mismatched verified structure candidate")
        else:
            selected = plans[expected]
        confluence = bundle.get("confluence", {})
        wanted = "UP" if action == ResearchAction.OPEN_LONG else "DOWN"
        if (
            confluence.get("state") != "TREND_RESONANCE"
            or confluence.get("primary_regime") != wanted
        ):
            missing.append("Required directional timeframe resonance absent; no forced entry")
        raw_components = bundle.get("derivatives", {}).get("components", {})
        for ref in set(model_refs):
            if ref.startswith("derivatives:") and raw_components.get(ref.split(":", 1)[1], {}).get(
                "status"
            ) not in {"VALID", "PARTIAL"}:
                missing.append(f"Cited derivative evidence unavailable: {ref}")
        active_case = choice.long_case if action == ResearchAction.OPEN_LONG else choice.short_case
        if not active_case.supporting_evidence_ids:
            missing.append("Selected direction lacks cited supporting evidence")
        view = (
            d.evidence_view(bundle["derivatives"], now)
            if bundle.get("derivatives")
            else {"components": {}}
        )
        original_valid = all(raw_components.get(n, {}).get("status") == "VALID" for n in CRITICAL)
        if not original_valid:
            missing.append("Critical original derivative evidence unavailable")
        stale = any(view["components"].get(n, {}).get("status") != "VALID" for n in CRITICAL)
        if (
            refresh
            and selected
            and original_valid
            and (not validation)
            and not bundle.get("historical")
            and not missing
        ):
            validation = revalidate_live(bundle, config)
        components = raw_components
        if validation:
            changes = compare_revalidation(bundle, validation, config)
            if changes:
                status = (
                    "RESEARCH_CHANGED" if validation.get("status") == "VALID" else "PLAN_INCOMPLETE"
                )
                missing.extend(changes)
            elif validation.get("expires_at_ms", 0) <= now:
                status = "EXPIRED"
                missing.append("Entry revalidation expired")
            else:
                components = validation["components"]
                expiry = min(expiry, validation["expires_at_ms"])
        elif stale:
            missing.append(
                "Critical live evidence expired; independent public revalidation required"
            )
        else:
            expiry = min(
                expiry,
                min(
                    (
                        p.get("expires_at_ms", expiry)
                        for n, p in raw_components.items()
                        if n in CRITICAL
                    ),
                    default=expiry,
                ),
            )
        if bundle.get("historical"):
            missing.append("Historical research lacks historical order book; no live plan")
        if selected and not missing:
            try:
                costs, rr, contracts, base = calculate_costs(
                    selected, bundle["instrument"], components, config, now
                )
                if rr.net < d.number(config.get("okx_plan_min_net_rr", 1.5), positive=True):
                    missing.append("Cost-adjusted risk/reward below configured minimum")
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                missing.append(
                    f"Plan calculation blocked: {str(exc) if isinstance(exc, ValueError) else type(exc).__name__}"
                )
                selected = None
        if not missing:
            status = "VALID_CANDIDATE"
        else:
            action = ResearchAction.WAIT
            if status not in {"RESEARCH_CHANGED", "EXPIRED"}:
                status = "PLAN_INCOMPLETE"
    elif action in {ResearchAction.REDUCE_LONG, ResearchAction.REDUCE_SHORT, ResearchAction.EXIT}:
        allowed = (
            {
                ResearchAction.REDUCE_LONG: {"LONG"},
                ResearchAction.REDUCE_SHORT: {"SHORT"},
                ResearchAction.EXIT: {"LONG", "SHORT"},
            }
        )[action]
        if position not in allowed:
            missing.append(
                "Position context does not support reduction/exit; never invent holdings"
            )
            action, status = ResearchAction.WAIT, "PLAN_INCOMPLETE"
        else:
            status = "POSITION_REVIEW"
    if missing and status == "WAIT":
        status = "PLAN_INCOMPLETE"
    # Public revalidation can itself take time. Recheck AFTER that I/O, not
    # only against the timestamp taken before the network request.
    now = okx.utc_ms()
    checked = okx.check_bundle(
        bundle, config, state["company_of_interest"], state.get("trade_date")
    )
    if not checked.get("allow_new_risk"):
        bundle = checked
        status, action = "RESEARCH_INVALID", ResearchAction.WAIT
        if "Core multi-timeframe research invalid" not in missing:
            missing.append("Core research expired or changed during final validation")
    elif status == "VALID_CANDIDATE" and now >= expiry:
        status, action = "EXPIRED", ResearchAction.WAIT
        missing.append("Entry evidence expired during final validation")
    if status != "VALID_CANDIDATE":
        costs = rr = contracts = base = None
    original_view = (
        d.evidence_view(bundle["derivatives"], now)
        if bundle.get("derivatives")
        else {"components": {}}
    )
    diagnostics = [
        f"Original derivative {name}: {part['status']}: {part.get('issues', [])}"
        for name, part in original_view["components"].items()
        if part["status"] != "VALID"
    ]
    if config.get("okx_plan_taker_fee_bps") is None:
        diagnostics.append("Taker fee assumption not supplied; no complete costed entry plan")
    result = PerpetualResearchResult(
        signal_timing=timing(bundle["cutoff_ms"], config).model_dump() if bundle.get("cutoff_ms") else None,
        signal_state="PENDING_BREAKOUT" if selected else "WAIT",
        research_id=state.get("research_id", "uninitialized-research"),
        instrument=state["company_of_interest"],
        research_cutoff_utc=iso(bundle["cutoff_ms"]) if bundle.get("cutoff_ms") else None,
        snapshot_id=snapshot,
        market_regime=bundle.get("confluence", {}),
        timeframe_analysis=bundle.get("timeframe_analysis", {}),
        long_case=choice.long_case,
        short_case=choice.short_case,
        wait_case=choice.wait_case,
        proposed_action=action,
        model_proposed_action=choice.proposed_action,
        strategy_type=selected["strategy_type"] if selected else None,
        position_context=position,
        entry_conditions=selected["entry_conditions"] if selected else [],
        entry_reference=PriceEvidence(**selected["entry"]) if selected else None,
        stop_loss=PriceEvidence(**selected["stop"]) if selected else None,
        take_profit=[PriceEvidence(**selected["target"])] if selected else [],
        invalidation_conditions=selected["invalidation_conditions"] if selected else [],
        estimated_costs=costs,
        risk_reward=rr,
        reference_contracts=contracts,
        reference_base_quantity=base,
        confidence_evidence=sorted(set(model_refs) & REFS),
        evidence={
            "cost_configuration": {
                k: config.get(k)
                for k in (
                    "okx_plan_taker_fee_bps",
                    "okx_plan_maker_fee_bps",
                    "okx_plan_horizon_hours",
                    "okx_plan_min_net_rr",
                )
            },
            "contract_spec": {
                k: bundle.get("instrument", {}).get(k)
                for k in ("ctVal", "ctMult", "tickSz", "lotSz", "minSz")
            },
            "scope": "Original research immutable; revalidation is separate; all levels are conditional research",
            "original_derivative_quality": original_view,
            "entry_revalidation": validation,
            "model_reasoning": choice.reasoning,
            "reference_quantity_scope": "Scenario estimate, not holdings, account risk sizing or an order",
        },
        missing_data=missing + diagnostics,
        blocking_reasons=missing,
        data_quality=bundle.get("status", "RESEARCH_INVALID"),
        plan_status=status,
        plan_valid=status == "VALID_CANDIDATE",
        requires_revalidation=action in OPEN or bool(missing) or status == "POSITION_REVIEW",
        expires_at_utc=iso(expiry),
        research_expires_at_utc=iso(research_expiry),
        allow_new_risk=False,
        execution_enabled=False,
    )
    return result, validation


def render_result(result):
    if not isinstance(result, PerpetualResearchResult):
        result = PerpetualResearchResult.model_validate(result)
    header = (
        f"## Perpetual research action: {result.proposed_action.value}\n\n"
        f"Plan status: {result.plan_status}; plan_valid={result.plan_valid}; "
        f"requires_revalidation={result.requires_revalidation}; execution_enabled=False\n\n"
        "Conditional research only. No execution authorization; pending triggers require a NEW validation.\n\n"
    )
    lines = [
        header,
        f"Research ID: {result.research_id}; cutoff UTC: {result.research_cutoff_utc}",
        f"Original snapshot: {result.snapshot_id}",
        f"Position context: {result.position_context}; data quality: {result.data_quality}",
        "Signal clock (separate from current evidence validity): " + json.dumps(result.signal_timing),
        f"Signal state: {result.signal_state}; fresh evidence must be checked AFTER confirmation",
        f"Evidence/research delivery expires UTC: {result.research_expires_at_utc}; plan expires UTC: {result.expires_at_utc}",
        "### Long case",
        result.long_case.thesis,
        "Supporting IDs: " + ", ".join(result.long_case.supporting_evidence_ids),
        "Opposing IDs: " + ", ".join(result.long_case.opposing_evidence_ids),
        "### Short case",
        result.short_case.thesis,
        "Supporting IDs: " + ", ".join(result.short_case.supporting_evidence_ids),
        "Opposing IDs: " + ", ".join(result.short_case.opposing_evidence_ids),
        "### Independent wait case",
        result.wait_case,
        "### Program-checked conditional levels",
    ]
    for name, value in (("Entry", result.entry_reference), ("Stop", result.stop_loss)):
        if value:
            lines.append(
                f"- {name}: {value.price} USDT; rule: {value.rule}; source: {value.source_snapshot_id}"
            )
    for target in result.take_profit:
        lines.append(f"- Target: {target.price} USDT; rule: {target.rule}")
    lines += result.entry_conditions + result.invalidation_conditions
    if result.risk_reward:
        lines.append(
            f"Gross RR: {result.risk_reward.gross:.6g}; net RR: {result.risk_reward.net:.6g}"
        )
        lines.append(
            "Cost estimate (USDT; forecast funding, not realized fees): "
            + result.estimated_costs.model_dump_json()
        )
    else:
        lines.append(
            "No complete costed entry plan. Explicit scenario fee assumptions are archived separately from actual account fees."
        )
    lines += [
        "### Missing/expired evidence and blockers",
        *(result.missing_data or ["None recorded"]),
        "### Delivery boundary",
        "allow_new_risk=False; execution_enabled=False. No order or execution authorization.",
        "Full schema-validated evidence and cost assumptions: research_result.json; Schema: research_result.schema.json.",
    ]
    return "\n\n".join(lines)
