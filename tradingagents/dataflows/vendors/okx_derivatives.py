"""Optional, credential-free derivative evidence. No account or order capability.

History is bounded, never filled, and not substituted with current observations.
Snapshot depth is an indicative estimate, not an executable quote or taker flow.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

from tradingagents.dataflows.vendors.okx import BARS, utc_ms, validate_instrument

OI = "/api/v5/public/open-interest"
OI_HISTORY = "/api/v5/rubik/stat/contracts/open-interest-history"
FUNDING = "/api/v5/public/funding-rate"
FUNDING_HISTORY = "/api/v5/public/funding-rate-history"
MARK = "/api/v5/public/mark-price"
INDEX = "/api/v5/market/index-tickers"
TICKER = "/api/v5/market/ticker"
BOOK = "/api/v5/market/books"
MARK_HISTORY = "/api/v5/market/history-mark-price-candles"
INDEX_HISTORY = "/api/v5/market/history-index-candles"


def number(value, *, positive=False, nonnegative=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError("Invalid numeric field")
    return result


def timestamp(value, ceiling):
    result = int(value)
    if result <= 0 or result > ceiling:
        raise ValueError("Timestamp beyond observation/research bound")
    return result


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def single(rows, instrument, inst_type=False):
    if len(rows) != 1 or rows[0].get("instId") != instrument:
        raise ValueError("Instrument identity mismatch")
    if inst_type and rows[0].get("instType") != "SWAP":
        raise ValueError("Instrument type mismatch")
    return rows[0]


def multiplier(info):
    return number(info["ctVal"], positive=True) * number(info.get("ctMult") or 1, positive=True)


def parse_oi(row, info, ceiling):
    contracts = number(row["oi"], nonnegative=True)
    base = number(row["oiCcy"], nonnegative=True)
    if not math.isclose(base, contracts * multiplier(info), rel_tol=1e-6, abs_tol=1e-8):
        raise ValueError("OI contract/base units mismatch")
    return {
        "ts": timestamp(row["ts"], ceiling),
        "contracts": contracts,
        "base_quantity": base,
        "notional_usd": number(row["oiUsd"], nonnegative=True),
    }


def parse_oi_history(rows, info, ceiling, interval):
    step = BARS[interval][1]
    records, seen = [], set()
    for row in rows:
        if len(row) != 4:
            raise ValueError("OI history requires ts/contracts/coin/USD")
        parsed = parse_oi(
            dict(zip(("ts", "oi", "oiCcy", "oiUsd"), row, strict=True)), info, ceiling
        )
        if parsed["ts"] in seen or parsed["ts"] % step:
            raise ValueError("Duplicate or unaligned OI history timestamp")
        seen.add(parsed["ts"])
        records.append(parsed)
    records.sort(key=lambda r: r["ts"])
    changes = []
    for before, after in zip(records, records[1:], strict=False):
        # No interpolation, no cross-gap rate, no changing-price USD denominator.
        if after["ts"] - before["ts"] == step and before["contracts"] > 0:
            changes.append(
                {
                    "from_ms": before["ts"],
                    "to_ms": after["ts"],
                    "change_fraction": after["contracts"] / before["contracts"] - 1,
                }
            )
    gaps = any(b["ts"] - a["ts"] != step for a, b in zip(records, records[1:], strict=False))
    return {
        "records": records,
        "changes": changes,
        "has_gaps": gaps,
        "interpretation": "OI changes do not identify aggressive long/short opening or closing",
    }


def funding_cashflow(base_quantity, settlement_price, rate, side):
    """USDT cashflow, positive means receipt; caller supplies actual event price.

    A fixed notional illustration is not a reconstructed historical position.
    """
    if side not in {"LONG", "SHORT"}:
        raise ValueError("Funding side must be LONG or SHORT")
    value = (
        number(base_quantity, nonnegative=True)
        * number(settlement_price, positive=True)
        * number(rate)
    )
    return -value if side == "LONG" else value


def parse_funding(row, ceiling):
    ts = timestamp(row["ts"], ceiling)
    funding_time = int(row["fundingTime"])
    next_time = int(row["nextFundingTime"])
    if funding_time <= ts or next_time <= funding_time:
        raise ValueError("Invalid upcoming funding schedule")
    return {
        "ts": ts,
        "forecast_rate": number(row["fundingRate"]),
        "funding_time_ms": funding_time,
        "next_funding_time_ms": next_time,
        "observed_interval_hours": (next_time - funding_time) / 3_600_000,
        "method": row.get("method"),
        "settlement_state": row.get("settState"),
        "settlement_state_scope": "settState describes the settFundingRate settlement cycle, not forecast_rate realization",
        "forecast_only": True,
    }


def parse_funding_history(rows, symbol, ceiling):
    records, seen = [], set()
    for row in rows:
        if row.get("instId") != symbol or row.get("instType") != "SWAP":
            raise ValueError("Funding instrument mismatch")
        ts = timestamp(row["fundingTime"], ceiling)
        if ts in seen:
            raise ValueError("Duplicate funding settlement")
        seen.add(ts)
        # Never substitute fundingRate (forecast) for a missing realizedRate.
        rate = number(row["realizedRate"])
        records.append(
            {
                "ts": ts,
                "realized_rate": rate,
                "forecast_rate": number(row["fundingRate"]),
                "long_cashflow_per_1000_usdt_notional": -1000 * rate,
                "short_cashflow_per_1000_usdt_notional": 1000 * rate,
            }
        )
    return {
        "records": sorted(records, key=lambda r: r["ts"]),
        "cashflow_scope": "Illustration per 1000 USDT event notional, not a historical portfolio backtest",
    }


def parse_price_history(rows, interval, ceiling):
    step = BARS[interval][1]
    parsed, seen = [], set()
    for row in rows:
        if len(row) != 6 or str(row[5]) not in {"0", "1"}:
            raise ValueError("Price candle requires six fields including confirm")
        ts = int(row[0])
        if ts <= 0 or ts % step or ts in seen:
            raise ValueError("Invalid/duplicate price candle timestamp")
        seen.add(ts)
        o, h, low, c = [number(v, positive=True) for v in row[1:5]]
        if not low <= min(o, c) <= max(o, c) <= h:
            raise ValueError("Invalid historical price OHLC")
        if str(row[5]) == "1" and ts + step <= ceiling:
            parsed.append(
                {"ts": ts, "close_ms": ts + step, "open": o, "high": h, "low": low, "close": c}
            )
    parsed.sort(key=lambda r: r["ts"])
    return {
        "records": parsed,
        "has_gaps": any(
            b["ts"] - a["ts"] != step for a, b in zip(parsed, parsed[1:], strict=False)
        ),
    }


def estimate_fill(levels, target_base, direction):
    remaining, value, filled = target_base, 0.0, 0.0
    for level in levels:
        take = min(remaining, level["base_quantity"])
        value += take * level["price"]
        filled += take
        remaining -= take
        if remaining <= target_base * 1e-12:
            break
    complete = remaining <= target_base * 1e-12
    vwap = value / filled if filled else None
    best = levels[0]["price"]
    return {
        "status": "VALID" if complete else "INSUFFICIENT_DEPTH",
        "filled_base": filled,
        "requested_base": target_base,
        "vwap": vwap,
        "slippage_bps_vs_best": (direction * (vwap / best - 1) * 10000) if complete else None,
    }


def parse_book(row, info, ceiling, notional, max_spread_bps=50):
    result = {"ts": timestamp(row["ts"], ceiling)}
    for side, reverse in (("asks", False), ("bids", True)):
        levels = []
        for level in row[side]:
            if len(level) != 4 or int(level[3]) < 0:
                raise ValueError("Malformed book level")
            price, contracts = number(level[0], positive=True), number(level[1], positive=True)
            if Decimal(str(level[0])) % Decimal(info["tickSz"]) != 0:
                raise ValueError("Book price off instrument tick")
            base = contracts * multiplier(info)
            levels.append(
                {
                    "price": price,
                    "contracts": contracts,
                    "base_quantity": base,
                    "notional_usdt": base * price,
                }
            )
        prices = [r["price"] for r in levels]
        if (
            not prices
            or len(set(prices)) != len(prices)
            or prices != sorted(prices, reverse=reverse)
        ):
            raise ValueError("Empty, duplicate or unsorted book")
        result[side] = levels
    bid, ask = result["bids"][0]["price"], result["asks"][0]["price"]
    if bid >= ask:
        raise ValueError("Crossed or locked book")
    mid = (bid + ask) / 2
    bid_base = sum(r["base_quantity"] for r in result["bids"])
    ask_base = sum(r["base_quantity"] for r in result["asks"])
    # Quote size is a mid-price reference; both sides consume the SAME base size.
    target_base = number(notional, positive=True) / mid
    lot = Decimal(info["lotSz"])
    contracts = max(Decimal(str(target_base / multiplier(info))), Decimal(info["minSz"]))
    contracts = (contracts / lot).to_integral_value(rounding=ROUND_CEILING) * lot
    target_base = float(contracts) * multiplier(info)
    result.update(
        best_bid=bid,
        best_ask=ask,
        mid=mid,
        spread_usdt=ask - bid,
        spread_bps=(ask - bid) / mid * 10000,
        bid_depth_base=bid_base,
        ask_depth_base=ask_base,
        bid_depth_usdt=sum(r["notional_usdt"] for r in result["bids"]),
        ask_depth_usdt=sum(r["notional_usdt"] for r in result["asks"]),
        imbalance=(bid_base - ask_base) / (bid_base + ask_base),
        requested_notional_usdt=notional,
        rounded_contracts=float(contracts),
        rounded_mid_notional_usdt=target_base * mid,
        buy_estimate=estimate_fill(result["asks"], target_base, 1),
        sell_estimate=estimate_fill(result["bids"], target_base, -1),
        abnormal_spread=(ask - bid) / mid * 10000 > max_spread_bps,
        scope="Displayed depth only; cancellations, latency, hidden liquidity and fees excluded; resting orders are not executed flow",
    )
    result["liquidity_status"] = (
        "INSUFFICIENT_DEPTH"
        if any(result[k]["status"] != "VALID" for k in ("buy_estimate", "sell_estimate"))
        else "ABNORMAL_SPREAD"
        if result["abnormal_spread"]
        else "VALID"
    )
    for name, direction in (("buy_estimate", 1), ("sell_estimate", -1)):
        fill = result[name]
        fill["cost_bps_vs_mid"] = (
            direction * (fill["vwap"] / mid - 1) * 10000 if fill["status"] == "VALID" else None
        )
    return result


def history_pages(client, path, params, cursor, *, cursor_field, time_key, count=100, max_pages=4):
    """Bounded backward pagination. Overlap is rejected rather than hidden."""
    records, seen = [], set()
    for _ in range(max_pages):
        page = client.get(
            path,
            {**params, cursor_field: str(cursor), "limit": str(min(100, count - len(records)))},
        )
        if not page:
            break
        stamps = [int(time_key(row)) for row in page]
        if (
            min(stamps) >= cursor
            or any(t >= cursor or t in seen for t in stamps)
            or len(set(stamps)) != len(stamps)
        ):
            raise ValueError("History pagination did not progress or overlapped")
        seen.update(stamps)
        records.extend(page)
        cursor = min(stamps)
        if len(records) >= count:
            break
    return records[:count]


def collect_derivatives(client, info, cutoff, interval, historical, config):
    """Fetch once per research. All consumers read this pinned evidence.

    Live observations are captured before the caller freezes its final cutoff.
    Historical research does not call current OI/funding/price/book endpoints.
    """
    validate_instrument(info, info["instId"])
    symbol, index_id = info["instId"], info["instFamily"]
    count = int(config.get("okx_derivatives_history_points", 100))
    ttl = int(config.get("okx_derivatives_ttl_seconds", 300))
    notional = number(config.get("okx_liquidity_notional_usdt", 10000), positive=True)
    if not 2 <= count <= 300 or not 1 <= ttl <= 900:
        raise ValueError("Invalid derivative history count or TTL")
    result = {
        "source": "OKX API V5 public REST",
        "exchange": "OKX",
        "instrument": symbol,
        "index_instrument": index_id,
        "historical": historical,
        "interval": interval,
        "research_cutoff_ms": cutoff,
        "components": {},
        "execution_enabled": False,
        "limitations": [
            "Optional derivative evidence does not grant execution permission",
            "OI cannot identify which side opened aggressively",
            "Historical contract specifications are not reconstructed",
        ],
    }
    result["history_limits"] = {
        "oi_history": "Official endpoint: latest 1440 entries; this request is bounded to configured points",
        "funding_history": "Official endpoint: up to three months; no assumption of fixed settlement frequency",
        "price_history": "Bounded closed candles; not an assertion of full requested strategy coverage",
        "orderbook": "Current-only snapshot; no historical order book reconstruction",
    }

    def capture(name, path, loader, units, live=False):
        fetched = utc_ms()
        part = {
            "source": path,
            "exchange": "OKX",
            "instrument": index_id if name.startswith("index") else symbol,
            "scope": "INDEX" if name.startswith("index") else "SWAP",
            "units": units,
            "fetched_at_ms": fetched,
            "as_of_ms": None,
            "status": "DATA_UNAVAILABLE",
            "issues": [],
            "interval": interval if "history" in name else None,
        }
        result["components"][name] = part
        if historical and live:
            part["issues"] = [
                "Historical observations unavailable from this current-only endpoint; not requested"
            ]
            return
        try:
            data = loader()
            records = data.get("records")
            if records is not None and not records:
                raise ValueError("No observations in requested historical range")
            as_of = (
                data.get("ts")
                if records is None
                else max(r.get("close_ms", r["ts"]) for r in records)
            )
            if as_of is None or as_of > (utc_ms() + 60_000 if live else cutoff):
                raise ValueError("Observation is later than allowed research bound")
            fetched = utc_ms()
            part.update(
                data=data,
                as_of_ms=as_of,
                as_of_utc=iso(as_of),
                status="VALID",
                fetched_at_ms=fetched,
            )
            if live:
                part["expires_at_ms"] = as_of + ttl * 1000
                if fetched - as_of > ttl * 1000:
                    part.update(
                        status="STALE_DATA", issues=["Current observation exceeds freshness budget"]
                    )
            else:
                part["coverage"] = {
                    "first_ms": records[0]["ts"],
                    "last_ms": records[-1]["ts"],
                    "count": len(records),
                    "requested_count": count,
                    "scope": "Bounded recent history, not a full archive",
                }
                step = BARS[interval][1]
                if (
                    len(records) < count
                    or data.get("has_gaps")
                    or (name != "funding_history" and cutoff - as_of > step)
                ):
                    part.update(
                        status="PARTIAL",
                        issues=["Insufficient count, gaps or lag in bounded history"],
                    )
        except Exception as exc:
            # No vendor body, auth value or arbitrary exception text reaches LLMs.
            part.update(
                status="DATA_UNAVAILABLE",
                issues=[f"Public evidence unavailable: {type(exc).__name__}"],
            )

    def current(path, inst=symbol, extra=None):
        return single(
            client.get(path, {"instId": inst, **(extra or {})}), inst, path in {OI, FUNDING, MARK}
        )

    capture(
        "oi_current",
        OI,
        lambda: parse_oi(current(OI, extra={"instType": "SWAP"}), info, utc_ms() + 60_000),
        {"contracts": "contracts", "base_quantity": info["ctValCcy"], "notional_usd": "USD"},
        True,
    )
    capture(
        "funding_current",
        FUNDING,
        lambda: parse_funding(current(FUNDING), utc_ms() + 60_000),
        {"forecast_rate": "fraction per observed settlement event"},
        True,
    )

    for name, path, inst, price_field in (
        ("mark_current", MARK, symbol, "markPx"),
        ("index_current", INDEX, index_id, "idxPx"),
        ("trade_current", TICKER, symbol, "last"),
    ):

        def price_loader(path=path, inst=inst, price_field=price_field):
            row = current(path, inst, {"instType": "SWAP"} if path == MARK else None)
            return {
                "ts": timestamp(row["ts"], utc_ms() + 60_000),
                "price": number(row[price_field], positive=True),
            }

        capture(name, path, price_loader, {"price": "USDT"}, True)
    capture(
        "orderbook",
        BOOK,
        lambda: parse_book(
            client.get(BOOK, {"instId": symbol, "sz": "100"})[0],
            info,
            utc_ms() + 60_000,
            notional,
            number(config.get("okx_max_spread_bps", 50), positive=True),
        ),
        {
            "contracts": "contracts",
            "base_quantity": info["ctValCcy"],
            "notional": "USDT",
            "spread": "bps",
        },
        True,
    )

    capture(
        "oi_history",
        OI_HISTORY,
        lambda: parse_oi_history(
            history_pages(
                client,
                OI_HISTORY,
                {"instId": symbol, "period": BARS[interval][0]},
                cutoff + 1,
                cursor_field="end",
                time_key=lambda r: r[0],
                count=count,
            ),
            info,
            cutoff,
            interval,
        ),
        {
            "contracts": "contracts",
            "base_quantity": info["ctValCcy"],
            "notional_usd": "USD",
            "change_fraction": "contracts ratio, only contiguous observations",
        },
    )
    capture(
        "funding_history",
        FUNDING_HISTORY,
        lambda: parse_funding_history(
            history_pages(
                client,
                FUNDING_HISTORY,
                {"instId": symbol},
                cutoff + 1,
                cursor_field="after",
                time_key=lambda r: r["fundingTime"],
                count=count,
            ),
            symbol,
            cutoff,
        ),
        {"realized_rate": "fraction per actual settlement", "cashflow": "USDT"},
    )
    for name, path, inst in (
        ("mark_history", MARK_HISTORY, symbol),
        ("index_history", INDEX_HISTORY, index_id),
    ):

        def price_history_loader(path=path, inst=inst):
            data = parse_price_history(
                history_pages(
                    client,
                    path,
                    {"instId": inst, "bar": BARS[interval][0]},
                    cutoff,
                    cursor_field="after",
                    time_key=lambda r: r[0],
                    count=count + 1,
                ),
                interval,
                cutoff,
            )
            data["records"] = data["records"][-count:]
            return data

        capture(name, path, price_history_loader, {"price": "USDT"})
    return result


def evidence_view(evidence, now=None):
    """Non-mutating view: revalidate expiry on tool/report reads, no refetch."""
    now = utc_ms() if now is None else now
    view = json.loads(json.dumps(evidence, allow_nan=False))
    for part in view["components"].values():
        if part.get("as_of_ms") is not None and part["as_of_ms"] > view["research_cutoff_ms"]:
            part.update(status="RESEARCH_INVALID", issues=["Observation exceeds pinned cutoff"])
        elif part.get("expires_at_ms", now) < now:
            part.update(
                status="STALE_DATA",
                issues=["Optional live evidence expired; no refresh inside pinned research"],
            )
        if part["status"] not in {"VALID", "PARTIAL"}:
            part.pop("data", None)
    components = view["components"]
    # Saved views may be read again later: never retain derived values after
    # their underlying observations become invalid or expire.
    view.pop("price_comparison", None)
    if components["oi_history"]["status"] not in {"VALID", "PARTIAL"}:
        view["price_oi_evidence"] = []
    names = ("mark_current", "index_current", "trade_current")
    if all(components[n]["status"] == "VALID" for n in names):
        observations = [components[n] for n in names]
        skew = max(p["as_of_ms"] for p in observations) - min(p["as_of_ms"] for p in observations)
        if skew <= 5000:
            mark, index, trade = [p["data"]["price"] for p in observations]
            view["price_comparison"] = {
                "max_timestamp_skew_ms": skew,
                "scope": "Near-time observations, not atomic",
                "mark_vs_index_bps": (mark / index - 1) * 10000,
                "trade_vs_index_bps": (trade / index - 1) * 10000,
            }
    usable = [p for p in components.values() if p["status"] in {"VALID", "PARTIAL"}]
    view["status"] = ("DATA_UNAVAILABLE" if not usable else
                      "VALID" if all(p["status"] == "VALID" for p in components.values()) else "PARTIAL")
    return view


def render_derivatives(evidence, detailed=False):
    view = evidence_view(evidence)
    if not detailed:
        lines = [
            "## OKX optional derivative evidence",
            "",
            "Optional source availability is separate from core candle quality; unknown is not bullish/bearish.",
            "Call get_verified_market_snapshot for values, units and bounded history. Full capture: derivatives_snapshot.json.",
            "",
            "| Component | Status | As-of UTC | Issues |",
            "|---|---|---|---|",
        ]
        lines.extend(
            f"| {name} | {p['status']} | {p.get('as_of_utc', 'unavailable')} | {p.get('issues', [])} |"
            for name, p in view["components"].items()
        )
        return "\n".join(lines)
    view["price_oi_evidence"] = view.get("price_oi_evidence", [])[-3:]
    # Exact archive remains in the state/JSON. Keep prompts bounded: indicator
    # tools repeat describe(), and must not repeat hundreds of rows per call.
    for part in view["components"].values():
        data = part.get("data", {})
        for key in ("records", "changes"):
            if key in data:
                data[key] = data[key][-3:]
                data["display_note"] = (
                    "Latest three observations shown; full captured history in derivatives_snapshot.json"
                )
        for key in ("asks", "bids"):
            if key in data:
                data[key] = data[key][:3]
    return (
        "## OKX optional derivative evidence\n\n"
        "Only VALID/PARTIAL observations below are evidence; missing optional data is not bearish/bullish. "
        "Use timestamps and coverage, never infer missing history. Mark price informs liquidation risk but "
        "cannot determine account liquidation without account/margin data. Depth is not taker flow. "
        "Funding forecasts are not settled fees. No execution permission.\n\n```json\n"
        + json.dumps(view, ensure_ascii=False, allow_nan=False)
        + "\n```"
    )


def price_oi_evidence(evidence, candles, interval):
    """Join only exact OI observation times to candle CLOSE times, no asof fill."""
    part = evidence["components"]["oi_history"]
    if part["status"] not in {"VALID", "PARTIAL"}:
        return []
    prices = {r["ts"] + BARS[interval][1]: r["Close"] for r in candles}
    joint = []
    for change in part["data"]["changes"]:
        start, end = change["from_ms"], change["to_ms"]
        if start in prices and end in prices:
            joint.append(
                {
                    **change,
                    "price_change_fraction": prices[end] / prices[start] - 1,
                    "scope": "Aligned descriptive change, not causal long/short flow evidence",
                }
            )
    return joint
