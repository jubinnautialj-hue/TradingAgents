"""One research cutoff, independent existing candle validators, no execution.

The parent is a compatibility view of the caller's anchor interval, NOT a
synthetic mixed-frequency series. All evidence is explicitly timeframe keyed.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

from tradingagents.dataflows.vendors import okx

PERIODS = ("1h", "4h", "1d")
ROLES = {"1d": "primary regime", "4h": "structure", "1h": "short-term confirmation"}


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def structure_rules(config):
    rules = {
        "version": "closed-pivots-v1",
        "pivot_window": int(config.get("okx_pivot_window", 2)),
        "range_window": int(config.get("okx_structure_window", 20)),
        "retest_atr_fraction": float(config.get("okx_retest_atr_fraction", 0.1)),
    }
    if not 1 <= rules["pivot_window"] <= 10 or not 5 <= rules["range_window"] <= 100:
        raise ValueError("Invalid pivot/range window")
    if not 0 <= rules["retest_atr_fraction"] <= 1:
        raise ValueError("Invalid retest ATR fraction")
    return rules


def analyze_timeframe(bundle, rules):
    """Closed-bar evidence, strictly confirmed pivots; no probability or order."""
    rows = bundle["records"]
    calc = okx.indicator_frame(bundle)
    names = (
        "close_10_ema",
        "close_50_sma",
        "close_200_sma",
        "rsi",
        "macd",
        "macds",
        "macdh",
        "atr",
        "vwma",
    )
    values = {name: float(calc[name].iloc[-1]) for name in names}
    close = rows[-1]["Close"]
    slope = float(calc["close_50_sma"].iloc[-1] - calc["close_50_sma"].iloc[-6])
    trend = (
        "UP"
        if close > values["close_50_sma"] > values["close_200_sma"] and slope > 0
        else "DOWN"
        if close < values["close_50_sma"] < values["close_200_sma"] and slope < 0
        else "MIXED"
    )
    width, window = rules["pivot_window"], rules["range_window"]
    step = okx.BARS[bundle["interval"]][1]
    pivots = {"highs": [], "lows": []}
    for i in range(width, len(rows) - width):
        neighbours = rows[i - width : i] + rows[i + 1 : i + width + 1]
        for field, key, compare in (("High", "highs", max), ("Low", "lows", min)):
            value = rows[i][field]
            extreme = compare(r[field] for r in neighbours)
            if (field == "High" and value > extreme) or (field == "Low" and value < extreme):
                pivots[key].append(
                    {
                        "price": value,
                        "bar_open_ms": rows[i]["ts"],
                        "confirmed_at_ms": rows[i + width]["ts"] + step,
                    }
                )
    highs, lows = pivots["highs"], pivots["lows"]
    pattern = "INSUFFICIENT_PIVOTS"
    if len(highs) >= 2 and len(lows) >= 2:
        pattern = (
            "HH_HL"
            if highs[-1]["price"] > highs[-2]["price"] and lows[-1]["price"] > lows[-2]["price"]
            else "LH_LL"
            if highs[-1]["price"] < highs[-2]["price"] and lows[-1]["price"] < lows[-2]["price"]
            else "MIXED_STRUCTURE"
        )
    previous = rows[-window - 1 : -1]
    resistance, support = max(r["High"] for r in previous), min(r["Low"] for r in previous)
    last, prev = rows[-1], rows[-2]
    old_range = rows[-window - 2 : -2]
    old_high, old_low = max(r["High"] for r in old_range), min(r["Low"] for r in old_range)
    tolerance = rules["retest_atr_fraction"] * values["atr"]
    events = {
        "close_breakout_up": close > resistance,
        "close_breakout_down": close < support,
        "intrabar_up_break_rejected": last["High"] > resistance and close <= resistance,
        "intrabar_down_break_rejected": last["Low"] < support and close >= support,
        "up_breakout_retest_held": prev["Close"] > old_high
        and abs(last["Low"] - old_high) <= tolerance
        and close > old_high,
        "down_breakout_retest_rejected": prev["Close"] < old_low
        and abs(last["High"] - old_low) <= tolerance
        and close < old_low,
    }
    recent = rows[-window - 1 : -1]
    mean_volume = sum(r["Volume"] for r in recent) / len(recent)
    return {
        "interval": bundle["interval"],
        "role": ROLES[bundle["interval"]],
        "source": bundle["source"],
        "cutoff_ms": bundle["cutoff_ms"],
        "latest_close_ms": bundle["latest_close_ms"],
        "latest_close_utc": iso(bundle["latest_close_ms"]),
        "snapshot_id": bundle["snapshot_id"],
        "bars": len(rows),
        "coverage_start_utc": rows[0]["Date"],
        "status": bundle["status"],
        "close": close,
        "indicators": values,
        "sma50_change_5_bars": slope,
        "trend": trend,
        "pivot_structure": pattern,
        "trend_structure_relation": (
            "CONTINUATION_CANDIDATE"
            if (trend, pattern) in {("UP", "HH_HL"), ("DOWN", "LH_LL")}
            else "REVERSAL_CANDIDATE"
            if (trend, pattern) in {("UP", "LH_LL"), ("DOWN", "HH_HL")}
            else "UNRESOLVED"
        ),
        "confirmed_pivots": {k: v[-4:] for k, v in pivots.items()},
        "support_candidates": sorted(
            {r["price"] for r in lows if r["price"] < close}, reverse=True
        )[:3],
        "resistance_candidates": sorted({r["price"] for r in highs if r["price"] > close})[:3],
        "previous_range": {
            "bars": window,
            "high": resistance,
            "low": support,
            "start_ms": previous[0]["ts"],
            "end_close_ms": previous[-1]["ts"] + step,
        },
        "retest_reference": {"high": old_high, "low": old_low, "tolerance_usdt": tolerance},
        "events": events,
        "event_confirmed_at_ms": bundle["latest_close_ms"],
        "volume_base": last["Volume"],
        "volume_units": bundle["volume_units"]["Volume"],
        "volume_vs_previous_window": last["Volume"] / mean_volume if mean_volume else None,
        "atr_vs_5_bars_ago": values["atr"] / float(calc["atr"].iloc[-6])
        if float(calc["atr"].iloc[-6])
        else None,
        "limitations": "Rule-based descriptive evidence; pivots require right-hand confirmation; candidate levels are not validated orders or guaranteed support",
    }


def confluence(analyses):
    """All required trend filters must agree; never majority-vote BUY/SELL."""
    trends = {p: analyses[p]["trend"] for p in PERIODS}
    values = set(trends.values())
    state = (
        "TREND_RESONANCE"
        if len(values) == 1 and "MIXED" not in values
        else "TREND_CONFLICT"
        if {"UP", "DOWN"} <= values
        else "INSUFFICIENT_EVIDENCE"
    )
    return {
        "state": state,
        "trends": trends,
        "primary_regime": trends["1d"],
        "priority": ["1d", "4h", "1h"],
        "reason": "1d defines regime, 4h describes structure, 1h supplies short-term confirmation. Conflicts are retained, not outvoted.",
        "scope": "Descriptive research context only; not a LONG/SHORT signal or a trade plan",
    }


class _PinnedClient:
    """Reuse the public client but fix its time/identity answers for all children."""

    def __init__(self, client, server_ms, info):
        self.client, self.server_ms, self.info = client, server_ms, info

    def get(self, path, params=None):
        if path != "/api/v5/public/time":
            raise ValueError("Pinned candle child only permits server time")
        return [{"ts": str(self.server_ms)}]

    def instrument(self, symbol):
        okx.validate_instrument(self.info, symbol)
        return copy.deepcopy(self.info)

    def candles(self, symbol, interval, cutoff, count):
        return self.client.candles(symbol, interval, cutoff, count)


def build_multitimeframe(symbol, trade_date, config, client=None):
    root = {
        "multi_timeframe": True,
        "source": "OKX API V5 public REST",
        "exchange": "OKX",
        "symbol": symbol,
        "interval": config.get("okx_interval", "4h"),
        "status": "DATA_UNAVAILABLE",
        "allow_new_risk": False,
        "execution_enabled": False,
        "issues": [],
        "timeframes": {},
    }
    try:
        okx.validate_symbol(symbol)
        if root["interval"] not in PERIODS:
            raise ValueError("Invalid anchor interval")
        rules = structure_rules(config)
        client = client or okx.OKXPublicClient()
        server_ms = int(client.get("/api/v5/public/time")[0]["ts"])
        if abs(server_ms - okx.utc_ms()) > 60000:
            raise ValueError("Local/OKX clock skew")
        initial_cutoff = okx.research_cutoff(trade_date, server_ms)
        info = client.instrument(symbol)
        extra = None
        if config.get("okx_derivatives_enabled", False):
            from tradingagents.dataflows.vendors.okx_derivatives import collect_derivatives

            extra = collect_derivatives(
                client, info, initial_cutoff, root["interval"], initial_cutoff < server_ms, config
            )
            if initial_cutoff == server_ms:
                server_ms = int(client.get("/api/v5/public/time")[0]["ts"])
        cutoff = okx.research_cutoff(trade_date, server_ms)
        root.update(cutoff_ms=cutoff, server_ms=server_ms, structure_rules=rules)
        pinned = _PinnedClient(client, server_ms, info)
        for period in PERIODS:
            child_config = {
                **config,
                "okx_multi_timeframe": False,
                "okx_derivatives_enabled": False,
                "okx_interval": period,
            }
            child = okx.build_bundle(symbol, trade_date, child_config, pinned)
            root["timeframes"][period] = okx.check_bundle(child, child_config, symbol, trade_date)
        failed = {p: b for p, b in root["timeframes"].items() if not b["allow_new_risk"]}
        if failed:
            root.update(
                status="RESEARCH_INVALID",
                issues=[f"Required {p}: {b['status']}: {b['issues']}" for p, b in failed.items()],
            )
            return root
        children = root["timeframes"]
        # Parent's OHLCV remains explicitly the anchor's; never concatenate bars.
        root.update(
            {
                k: copy.deepcopy(v)
                for k, v in children[root["interval"]].items()
                if k != "snapshot_id"
            }
        )
        analyses = {p: analyze_timeframe(b, rules) for p, b in children.items()}
        root.update(
            timeframe_analysis=analyses,
            confluence=confluence(analyses),
            status="VALID",
            allow_new_risk=True,
        )
        if extra is not None:
            from tradingagents.dataflows.vendors.okx_derivatives import price_oi_evidence

            extra["research_cutoff_ms"] = cutoff
            extra["price_oi_evidence"] = price_oi_evidence(extra, root["records"], root["interval"])
            root["derivatives"] = extra
        root["snapshot_id"] = okx.fingerprint(root)
        return check_multitimeframe(root, config, symbol, trade_date)
    except Exception as exc:
        return {
            **root,
            "status": "RESEARCH_INVALID",
            "allow_new_risk": False,
            "execution_enabled": False,
            "issues": [f"Multi-timeframe snapshot unavailable: {type(exc).__name__}"],
        }


def check_multitimeframe(bundle, config, symbol=None, trade_date=None):
    result = dict(bundle)
    try:
        if (
            not config.get("okx_multi_timeframe", False)
            or not bundle.get("multi_timeframe")
            or set(bundle.get("timeframes", {})) != set(PERIODS)
        ):
            raise ValueError("All three required periods must be present")
        if bundle.get("status") != "VALID":
            return {**result, "allow_new_risk": False, "execution_enabled": False}
        if bundle["interval"] != config.get("okx_interval", "1h") or bundle[
            "snapshot_id"
        ] != okx.fingerprint(bundle):
            raise ValueError("Parent identity/fingerprint mismatch")
        checked = {}
        for period in PERIODS:
            child = bundle["timeframes"][period]
            if (
                child["cutoff_ms"] != bundle["cutoff_ms"]
                or child["server_ms"] != bundle["server_ms"]
                or child["interval"] != period
            ):
                raise ValueError("Cross-timeframe cutoff/identity mismatch")
            cfg = {**config, "okx_multi_timeframe": False, "okx_interval": period}
            checked[period] = okx.check_bundle(child, cfg, symbol or bundle["symbol"], trade_date)
        result["timeframes"] = checked
        failed = {p: b for p, b in checked.items() if not b["allow_new_risk"]}
        if failed:
            return {
                **result,
                "status": "RESEARCH_INVALID",
                "allow_new_risk": False,
                "execution_enabled": False,
                "issues": [
                    f"Required {p}: {b['status']}: {b['issues']}" for p, b in failed.items()
                ],
            }
        return {**result, "allow_new_risk": True, "execution_enabled": False}
    except Exception as exc:
        return {
            **result,
            "status": "RESEARCH_INVALID",
            "allow_new_risk": False,
            "execution_enabled": False,
            "issues": [f"Multi-timeframe integrity failure: {type(exc).__name__}"],
        }


def describe_multitimeframe(bundle):
    lines = [
        "## Multi-timeframe quality (required: 1d / 4h / 1h)",
        "",
        f"Unified cutoff UTC: {iso(bundle['cutoff_ms']) if bundle.get('cutoff_ms') else 'unavailable'}",
        "",
        "| Period | Status | Latest closed UTC | Snapshot ID | Issues |",
        "|---|---|---|---|---|",
    ]
    for period in PERIODS:
        child = bundle.get("timeframes", {}).get(period, {})
        lines.append(
            f"| {period} | {child.get('status', 'DATA_UNAVAILABLE')} | "
            f"{iso(child['latest_close_ms']) if child.get('latest_close_ms') else 'unavailable'} | "
            f"{child.get('snapshot_id', 'unavailable')} | {child.get('issues', [])} |"
        )
    lines.append("")
    lines.append(
        "Research priority: 1d regime → 4h structure → 1h short-term confirmation; never majority vote."
    )
    if bundle.get("allow_new_risk"):
        lines.append("Confluence: " + json.dumps(bundle.get("confluence", {}), ensure_ascii=False))
    return "\n".join(lines)


def render_multitimeframe(bundle):
    header = okx.describe(bundle)
    if not bundle.get("allow_new_risk"):
        return (
            header
            + "\n\nRESEARCH_INVALID: required timeframe failed. No directional conclusion or new risk permitted."
        )
    parts = [
        header,
        "## Deterministic timeframe evidence\n\n```json\n"
        + json.dumps(
            {
                "rules": bundle["structure_rules"],
                "timeframes": bundle["timeframe_analysis"],
                "confluence": bundle["confluence"],
            },
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n```",
    ]
    for period in ("1d", "4h", "1h"):
        parts.append(
            f"## Verified {period} evidence (never reinterpret as another timeframe)\n"
            + okx.render_snapshot(bundle["timeframes"][period])
        )
    if bundle.get("derivatives"):
        from tradingagents.dataflows.vendors.okx_derivatives import render_derivatives

        parts.append(render_derivatives(bundle["derivatives"], detailed=True))
    return "\n\n".join(parts)
