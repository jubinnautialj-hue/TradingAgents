"""Optional public taker flow, independently reconciled with closed minute bars.

Short-window evidence only, not a strategy, historical parameter reconstruction
or persistent CVD feed. Original core snapshots and plan rules are untouched.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from . import okx
from .okx_derivatives import multiplier

TRADES = "/api/v5/market/history-trades"
MINUTE = 60_000
VERSION = "okx-orderflow-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def number(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise ValueError("Invalid trade price/size")
    return n


def parse_trade(row, info, cutoff):
    if row["instId"] != info["instId"] or row["side"] not in {"buy", "sell"}:
        raise ValueError("Trade identity/side mismatch")
    ts, tid = int(row["ts"]), int(row["tradeId"])
    if ts <= 0 or ts > cutoff or tid <= 0:
        raise ValueError("Future/invalid trade time or identity")
    px, size = number(row["px"]), number(row["sz"])
    base = size * multiplier(info) if info["instType"] == "SWAP" else size
    if not math.isfinite(base) or not math.isfinite(base*px) or base <= 0:
        raise ValueError("Trade conversion overflow")
    return {"ts": ts, "trade_id": tid, "side": row["side"], "price": px,
            "base": base, "quote": base*px, "contracts": size if info["instType"] == "SWAP" else None,
            "source": row.get("source", "UNKNOWN")}


def aggregate(trades, start, end, bucket_ms):
    result, cvd, cvd_quote = [], 0., 0.
    for t in range(start, end, bucket_ms):
        rows = [r for r in trades if t <= r["ts"] < min(t+bucket_ms, end)]
        buy = sum(r["base"] for r in rows if r["side"] == "buy")
        sell = sum(r["base"] for r in rows if r["side"] == "sell")
        bq = sum(r["quote"] for r in rows if r["side"] == "buy")
        sq = sum(r["quote"] for r in rows if r["side"] == "sell")
        cvd += buy-sell; cvd_quote += bq-sq
        result.append({"start_ms": t, "end_ms": min(t+bucket_ms, end), "trade_count": len(rows),
            "buy_base": buy, "sell_base": sell, "delta_base": buy-sell, "cvd_base": cvd,
            "buy_usdt": bq, "sell_usdt": sq, "delta_usdt": bq-sq, "cvd_usdt": cvd_quote})
    return result


def divergence_evidence(rows, start, end):
    """Explicit two-half extrema comparison, NOT confirmed swing divergence."""
    halves = [[], []]; cvd = 0.
    for row in rows:
        cvd += row["base"] if row["side"] == "buy" else -row["base"]
        halves[int(row["ts"] >= (start+end)//2)].append({"ts": row["ts"], "price": row["price"], "cvd_base": cvd})
    if not all(halves):
        return {"status": "INSUFFICIENT_EVIDENCE", "reason": "Both half-windows need trades"}
    highs = [max(h, key=lambda r: r["price"]) for h in halves]
    lows = [min(h, key=lambda r: r["price"]) for h in halves]
    return {"status": "OBSERVED_SHORT_WINDOW_COMPARISON", "half_window_highs": highs,
            "half_window_lows": lows,
            "higher_price_high_lower_cvd": highs[1]["price"] > highs[0]["price"] and highs[1]["cvd_base"] < highs[0]["cvd_base"],
            "lower_price_low_higher_cvd": lows[1]["price"] < lows[0]["price"] and lows[1]["cvd_base"] > lows[0]["cvd_base"],
            "limitation": "Two half-window price extrema with contemporaneous window-reset CVD, not swing confirmation or a trading signal"}


def reconcile(client, info, rows, start, end):
    candles = client.get("/api/v5/market/history-candles", {"instId": info["instId"],
                         "bar": "1m", "after": str(end), "limit": "100"})
    selected = {}
    for row in candles:
        ts = int(row[0])
        if start <= ts < end:
            if ts in selected or str(row[8]) != "1" or ts % MINUTE:
                raise ValueError("Duplicate/unconfirmed minute bar")
            prices = [number(x) for x in row[1:5]]
            if not prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1]:
                raise ValueError("Invalid minute OHLC")
            base = float(row[6] if info["instType"] == "SWAP" else row[5])
            quote = float(row[7])
            if not all(math.isfinite(v) and v >= 0 for v in (base, quote)):
                raise ValueError("Invalid minute volume")
            if info["instType"] == "SWAP" and not math.isclose(float(row[5])*multiplier(info), base, rel_tol=1e-6, abs_tol=1e-8):
                raise ValueError("Minute contract/base mismatch")
            selected[ts] = (row, base, quote)
    if set(selected) != set(range(start, end, MINUTE)):
        raise ValueError("Minute candle coverage incomplete")
    checks = []
    for ts, (_, base, quote) in sorted(selected.items()):
        observed = [r for r in rows if ts <= r["ts"] < ts+MINUTE]
        ob, oq = sum(r["base"] for r in observed), sum(r["quote"] for r in observed)
        checks.append({"ts": ts, "observed_base": ob, "candle_base": base,
                       "observed_usdt": oq, "candle_usdt": quote,
                       "matched": math.isclose(ob, base, rel_tol=1e-6, abs_tol=1e-8)
                                  and math.isclose(oq, quote, rel_tol=1e-6, abs_tol=1e-6)})
    return checks, selected


def collect_market(client, info, start, end, cutoff, max_pages):
    seen, cursor, reached, pages = {}, None, False, 0
    result = {"instrument": info["instId"], "status": "PARTIAL", "issues": [], "statistics": None,
              "cvd": None, "start_ms": start, "end_ms": end, "fetched_at_ms": okx.utc_ms()}
    try:
        for _ in range(max_pages):
            params = {"instId": info["instId"], "limit": "100", "type": "1" if cursor else "2",
                      "after": str(cursor) if cursor else str(end)}
            page = client.get(TRADES, params); pages += 1
            if not page:
                result["issues"].append("Empty history page before start boundary")
                break
            parsed = [parse_trade(r, info, cutoff) for r in page]
            ids = [r["trade_id"] for r in parsed]
            times = [r["ts"] for r in parsed]
            if ids != sorted(ids, reverse=True) or times != sorted(times, reverse=True):
                raise ValueError("Unordered trade page")
            oldest = min(ids)
            if cursor is not None and oldest >= cursor:
                raise ValueError("Pagination did not progress")
            for row in parsed:
                old = seen.get(row["trade_id"])
                if old is not None and old != row:
                    raise ValueError("Conflicting duplicate trade")
                seen[row["trade_id"]] = row
            if min(r["ts"] for r in parsed) < start:
                reached = True
                break
            cursor = oldest
        rows = sorted((r for r in seen.values() if start <= r["ts"] < end), key=lambda r: (r["ts"], r["trade_id"]))
        result.update(trade_count=len(rows), pages=pages, reached_start=reached,
                      trade_digest=digest(rows), raw_trades=rows)
        checks, candles = reconcile(client, info, rows, start, end)
        result["volume_reconciliation"] = checks
        expected = sum(c["candle_base"] for c in checks)
        result["coverage"] = {"definition": "Observed base volume / closed-minute candle base volume; not proof of every exchange match",
                              "volume_ratio": sum(r["base"] for r in rows)/expected if expected else None,
                              "time_boundary_reached": reached}
        if not reached or not all(c["matched"] for c in checks):
            result["issues"].append("Page budget/boundary or volume mismatch: complete CVD suppressed")
            return result
        if not rows:
            result["issues"].append("No trades: no price/CVD interpretation")
            return result
        curve = aggregate(rows, start, end, 10_000)
        total = aggregate(rows, start, end, end-start)[0]
        total.update(first_price=rows[0]["price"], last_price=rows[-1]["price"],
                     price_return=rows[-1]["price"]/rows[0]["price"]-1,
                     delta_imbalance=total["delta_base"]/(total["buy_base"]+total["sell_base"]))
        # A limited directional disagreement, not a confirmed swing divergence.
        divergence = "PRICE_UP_DELTA_DOWN" if total["price_return"] > 0 and total["delta_base"] < 0 else (
            "PRICE_DOWN_DELTA_UP" if total["price_return"] < 0 and total["delta_base"] > 0 else "NONE")
        result.update(status="VALID", statistics=total, cvd={"origin_ms": start, "initial_value": 0,
            "scope": "Window-reset CVD only; no pre-window accumulation", "buckets_10s": curve,
            "buckets_30s": aggregate(rows, start, end, 30_000), "buckets_60s": aggregate(rows, start, end, MINUTE)},
            price_cvd_directional_disagreement=divergence,
            divergence=divergence_evidence(rows, start, end),
            candle_price_change=float(candles[max(candles)][0][4])/float(candles[min(candles)][0][1])-1)
    except Exception as exc:
        result.update(status="DATA_UNAVAILABLE", issues=["Public flow validation failed: "+type(exc).__name__],
                      statistics=None, cvd=None)
    finally:
        result["fetched_at_ms"] = okx.utc_ms()
    return result


def collect_orderflow(bundle, config, client=None):
    cutoff = bundle["cutoff_ms"]
    minutes = int(config.get("okx_orderflow_minutes", 1))
    pages = int(config.get("okx_orderflow_max_pages", 60))
    ttl = int(config.get("okx_orderflow_ttl_seconds", 300))
    if not 1 <= minutes <= 60 or not 1 <= pages <= 100 or not 1 <= ttl <= 300:
        raise ValueError("Invalid bounded orderflow configuration")
    end = cutoff//MINUTE*MINUTE; start = end-minutes*MINUTE
    root = {"version": VERSION, "source": "OKX API V5 public REST", "exchange": "OKX",
        "core_snapshot_id": bundle["snapshot_id"], "research_cutoff_ms": cutoff,
        "start_ms": start, "end_ms": end, "status": "DATA_UNAVAILABLE", "markets": {}, "linkage": None,
        "ttl_seconds": ttl, "fetched_at_ms": okx.utc_ms(), "issues": [], "execution_enabled": False,
        "observations_vs_interpretation": "Taker side observed; Delta/CVD/premium computed; any direction explanation is model interpretation, not causality",
        "limitations": ["Short closed-minute window, not 1h/4h/1d CVD", "No tradeId continuity guarantee; reconcile each minute with OHLCV",
                        "No historical contract-spec reconstruction; historical mode explicitly unavailable",
                        "OI/funding evidence has separate timestamps; do not infer synchronous changes or spot/perpetual leadership"]}
    if bundle.get("historical") or bundle.get("status") != "VALID":
        root["issues"] = ["Historical or invalid core: no live trade/spot substitution"]
    else:
        client = client or okx.OKXPublicClient()
        swap = bundle["instrument"]
        okx.validate_instrument(swap, bundle["symbol"])
        root["base_unit"], root["quote_unit"] = swap["ctValCcy"], "USDT"
        root["markets"]["swap"] = collect_market(client, swap, start, end, cutoff, pages)
        spot_id = bundle["symbol"].removesuffix("-SWAP")
        try:
            items = client.get("/api/v5/public/instruments", {"instType": "SPOT", "instId": spot_id})
            if len(items) != 1:
                raise ValueError("Missing matching spot")
            spot = items[0]
            if spot["instId"] != spot_id or spot["instType"] != "SPOT" or spot["state"] != "live" or spot["baseCcy"] != swap["ctValCcy"] or spot["quoteCcy"] != "USDT":
                raise ValueError("Spot identity mismatch")
            root["markets"]["spot"] = collect_market(client, spot, start, end, cutoff, pages)
        except Exception as exc:
            root["markets"]["spot"] = {"status": "DATA_UNAVAILABLE", "issues": ["Spot validation: "+type(exc).__name__], "statistics": None, "cvd": None}
        if all(m["status"] == "VALID" for m in root["markets"].values()):
            a, b = [root["markets"][k]["statistics"] for k in ("swap", "spot")]
            # VWAPs over exactly the same full window; not asynchronous last quotes.
            av = (a["buy_usdt"]+a["sell_usdt"])/(a["buy_base"]+a["sell_base"])
            bv = (b["buy_usdt"]+b["sell_usdt"])/(b["buy_base"]+b["sell_base"])
            root.update(status="VALID", linkage={"window_vwap_premium_bps": (av/bv-1)*10000,
                "swap_vwap": av, "spot_vwap": bv, "opposing_delta_signs": a["delta_base"]*b["delta_base"] < 0,
                "causal_leadership": "NOT_ESTABLISHED"})
        else:
            root.update(status="PARTIAL", issues=["Incomplete market evidence; linkage suppressed"])
    root["snapshot_id"] = digest(root)
    return root


def evidence_view(raw, now_ms=None):
    view = copy.deepcopy(raw); now = okx.utc_ms() if now_ms is None else now_ms
    if digest({k: v for k, v in raw.items() if k != "snapshot_id"}) != raw.get("snapshot_id"):
        return {"status": "RESEARCH_INVALID", "issues": ["Orderflow fingerprint mismatch"], "execution_enabled": False}
    if now < raw["fetched_at_ms"]-60000 or now-raw["end_ms"] > raw["ttl_seconds"]*1000:
        view.update(status="STALE_DATA", linkage=None, issues=raw["issues"]+["Closed-window orderflow evidence expired"])
        for m in view["markets"].values():
            m.update(status="STALE_DATA", statistics=None, cvd=None)
            for key in ("divergence", "price_cvd_directional_disagreement", "candle_price_change"):
                m.pop(key, None)
    for m in view["markets"].values():
        m.pop("raw_trades", None)
    return view


def render_orderflow(raw):
    view = evidence_view(raw)
    # Small evidence view shared by every existing role; raw trades remain archived.
    for m in view.get("markets", {}).values():
        if m.get("cvd"):
            m["cvd"] = {k: v[-6:] if isinstance(v, list) else v for k, v in m["cvd"].items() if k != "buckets_10s"}
            m["cvd"]["display_scope"] = "Last six buckets only; full series in orderflow_snapshot.json"
        if "volume_reconciliation" in m:
            checks = m.pop("volume_reconciliation")
            m["minute_reconciliation_summary"] = {"minutes": len(checks), "all_matched": all(c["matched"] for c in checks)}
    return "\n## Optional taker flow / spot-perpetual evidence\n"+json.dumps(view, ensure_ascii=True, allow_nan=False)
