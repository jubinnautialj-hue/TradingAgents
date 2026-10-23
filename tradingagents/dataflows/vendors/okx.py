"""Credential-free OKX V5 research data. No account or execution API exists here.

Only literal public GET paths below are reachable. A research bundle
is serializable so analysts and checkpoint replay use precisely the same bars.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import random
import re
import threading
import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests
from stockstats import wrap

from tradingagents.dataflows.errors import VendorUnavailableError

MODE = "okx_usdt_perpetual"
BASE_URL = "https://www.okx.com"
PUBLIC_PATHS = frozenset({
    "/api/v5/market/history-trades",
    "/api/v5/public/market-data-history",
    "/api/v5/public/time", "/api/v5/public/instruments", "/api/v5/market/history-candles",
    "/api/v5/public/open-interest", "/api/v5/rubik/stat/contracts/open-interest-history",
    "/api/v5/public/funding-rate", "/api/v5/public/funding-rate-history",
    "/api/v5/public/mark-price", "/api/v5/market/index-tickers", "/api/v5/market/ticker",
    "/api/v5/market/books", "/api/v5/market/history-mark-price-candles",
    "/api/v5/market/history-index-candles",
})
# OKX's plain 1D uses UTC+8. Explicitly choose UTC+0 for research.
BARS = {"1h": ("1H", 3_600_000), "4h": ("4H", 14_400_000), "1d": ("1Dutc", 86_400_000)}
INDICATORS = ("close_10_ema", "close_50_sma", "close_200_sma", "rsi", "macd",
              "macds", "macdh", "boll", "boll_ub", "boll_lb", "atr", "vwma", "mfi")
_PACE_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


def enabled(config: dict) -> bool:
    return config.get("market_mode") == MODE


def validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]+-USDT-SWAP", symbol):
        raise ValueError("OKX research requires an exact instrument ID, e.g. BTC-USDT-SWAP")
    return symbol


def utc_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def research_cutoff(trade_date: str, server_ms: int) -> int:
    day = datetime.strptime(trade_date, "%Y-%m-%d").replace(tzinfo=UTC)
    # Historical dates mean end of the UTC day; current dates mean server now.
    return min(int((day + timedelta(days=1)).timestamp() * 1000), server_ms)


class DataQualityError(ValueError):
    def __init__(self, status: str, reason: str):
        self.status = status
        super().__init__(reason)


class OKXPublicClient:
    """Public GET allowlist, bounded retries and process-wide request pacing.

    No supplied session, auth, API key, MCP transport or user-chosen URL. Redirects
    are refused. Network exceptions are reported by class, without URL/headers.
    """

    def get(self, path: str, params: dict | None = None) -> list:
        if path not in PUBLIC_PATHS:
            raise ValueError("Non-public or unapproved OKX path blocked")
        global _LAST_REQUEST
        for attempt in range(3):
            with _PACE_LOCK:
                wait = max(0.0, 0.25 - (time.monotonic() - _LAST_REQUEST))
                time.sleep(wait)
                _LAST_REQUEST = time.monotonic()
            retry_after = None
            try:
                # Session avoids ambient .netrc authentication; no cookies persist
                # between calls, and no credential-bearing headers are accepted.
                with requests.Session() as session:
                    session.auth = lambda request: request  # suppress .netrc auth, keep proxy support
                    response = session.get(BASE_URL + path, params=params or {},
                                           timeout=(5, 20), allow_redirects=False)
                if response.status_code == 429 or response.status_code >= 500:
                    with contextlib.suppress(ValueError):
                        retry_after = min(30.0, max(0.0, float(response.headers.get("Retry-After", "0"))))
                    failure = f"HTTP {response.status_code}"
                elif response.status_code != 200:
                    raise VendorUnavailableError(f"OKX public request rejected: HTTP {response.status_code}")
                else:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise VendorUnavailableError("OKX malformed public response")
                    if str(payload.get("code")) == "0" and isinstance(payload.get("data"), list):
                        return payload["data"]
                    code = str(payload.get("code", "missing"))
                    if code not in {"50011", "50040", "50013"}:
                        raise VendorUnavailableError(f"OKX public API error code {code}")
                    failure = f"API code {code}"
            except (requests.Timeout, requests.ConnectionError) as exc:
                failure = type(exc).__name__
            except (requests.RequestException, ValueError) as exc:
                raise VendorUnavailableError(f"OKX unreadable public response: {type(exc).__name__}") from None
            if attempt < 2:
                time.sleep(max(retry_after or 0.0, 2 ** attempt + random.uniform(0, 0.25)))
        raise VendorUnavailableError(f"OKX public request unavailable after 3 attempts: {failure}")

    def instrument(self, symbol: str) -> dict:
        symbol = validate_symbol(symbol)
        rows = self.get("/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})
        if len(rows) != 1:
            raise DataQualityError("RESEARCH_INVALID", "Instrument not uniquely found")
        info = rows[0]
        validate_instrument(info, symbol)
        return info

    def candles(self, symbol: str, interval: str, cutoff_ms: int, count: int) -> list:
        validate_symbol(symbol)
        bar, step = BARS[interval]
        rows, cursor = [], cutoff_ms
        # Bound pagination independently of what a server returns.
        for _ in range(math.ceil(count / 100) + 2):
            page = self.get("/api/v5/market/history-candles", {
                "instId": symbol, "bar": bar, "after": str(cursor), "limit": "100",
            })
            if not page:
                break
            try:
                stamps = [int(row[0]) for row in page]
                confirmed = [r for r in page if str(r[8]) == "1" and int(r[0]) + step <= cutoff_ms]
            except (IndexError, TypeError, ValueError):
                raise DataQualityError("RESEARCH_INVALID", "Malformed candle page") from None
            if min(stamps) >= cursor:
                raise DataQualityError("RESEARCH_INVALID", "Pagination did not move backward")
            rows.extend(page)
            cursor = min(stamps)
            usable = [r for r in rows if len(r) == 9 and str(r[8]) == "1"
                      and int(r[0]) + step <= cutoff_ms]
            if confirmed and len(usable) >= count:
                break
        return rows


def validate_instrument(info: dict, symbol: str) -> None:
    base = validate_symbol(symbol).split("-")[0]
    if any(info.get(k) != v for k, v in {
        "instId": symbol, "instType": "SWAP", "settleCcy": "USDT", "ctType": "linear",
        "state": "live", "ctValCcy": base,
    }.items()):
        raise DataQualityError("RESEARCH_INVALID", "Not a live linear USDT-settled OKX swap")
    # SWAP quoteCcy/baseCcy may be empty. instFamily supplies the actual pair.
    if info.get("instFamily") != f"{base}-USDT" or info.get("quoteCcy") not in (None, "", "USDT"):
        raise DataQualityError("RESEARCH_INVALID", "Instrument quote/family mismatch")
    for field in ("ctVal", "minSz", "lotSz", "tickSz"):
        try:
            value = float(info[field])
            if not math.isfinite(value) or value <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise DataQualityError("RESEARCH_INVALID", f"Invalid instrument field {field}") from None


def validate_candles(rows: list, interval: str, cutoff_ms: int, min_bars: int,
                     count: int, max_lag_bars: int = 1) -> tuple[pd.DataFrame, list[str]]:
    if interval not in BARS:
        raise DataQualityError("RESEARCH_INVALID", "Unsupported candle interval")
    step = BARS[interval][1]
    parsed, seen, notes = [], set(), []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 9:
            raise DataQualityError("RESEARCH_INVALID", "Candle must contain all nine V5 fields")
        try:
            ts = int(row[0])
            values = [float(v) for v in row[1:8]]
            if ts <= 0 or ts % step or str(row[8]) not in {"0", "1"}:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise DataQualityError("RESEARCH_INVALID", "Invalid timestamp, UTC alignment or confirm") from None
        if ts in seen:
            raise DataQualityError("RESEARCH_INVALID", "Duplicate candle timestamp")
        seen.add(ts)
        o, h, low, c, contracts, base_vol, quote_vol = values
        if (not all(math.isfinite(v) for v in values) or min(o, h, low, c) <= 0
                or min(contracts, base_vol, quote_vol) < 0 or not low <= min(o, c) <= max(o, c) <= h):
            raise DataQualityError("RESEARCH_INVALID", "Invalid OHLC or volume values")
        if str(row[8]) == "0":
            notes.append("Unconfirmed candle excluded")
            continue
        if ts + step > cutoff_ms:
            notes.append("Candle beyond decision cutoff excluded")
            continue
        parsed.append([ts, o, h, low, c, base_vol, contracts, quote_vol])
    if not parsed:
        raise DataQualityError("DATA_UNAVAILABLE", "No confirmed closed candles")
    original = [r[0] for r in parsed]
    ordered = sorted(parsed)
    if original != sorted(original) and original != sorted(original, reverse=True):
        notes.append("Out-of-order response sorted by timestamp")
    parsed = ordered[-count:]
    if len(parsed) < min_bars:
        raise DataQualityError("PARTIAL", f"Warmup needs {min_bars} closed bars; received {len(parsed)}")
    if any(b[0] - a[0] != step for a, b in zip(parsed, parsed[1:], strict=False)):
        raise DataQualityError("PARTIAL", "Missing candle interval; no gaps are filled")
    expected = cutoff_ms // step * step - step
    if expected - parsed[-1][0] > max_lag_bars * step:
        raise DataQualityError("STALE_DATA", "Latest closed candle exceeds freshness budget")
    df = pd.DataFrame(parsed, columns=["ts", "Open", "High", "Low", "Close", "Volume",
                                       "VolumeContracts", "VolumeQuote"])
    df.insert(0, "Date", pd.to_datetime(df["ts"], unit="ms", utc=True))
    return df, sorted(set(notes))


def frame(bundle: dict) -> pd.DataFrame:
    df = pd.DataFrame(bundle["records"])
    df["Date"] = pd.to_datetime(df["Date"], utc=True)
    return df


def fingerprint(bundle: dict) -> str:
    data = {k: bundle[k] for k in ("instrument", "symbol", "interval", "cutoff_ms", "records",
                                  "fetched_at_ms", "server_ms", "latest_close_ms", "historical", "volume_units")}
    if "derivatives" in bundle:
        data["derivatives"] = bundle["derivatives"]
    if bundle.get("multi_timeframe"):
        data.update({k: bundle[k] for k in ("multi_timeframe", "timeframes", "timeframe_analysis",
                                           "confluence", "structure_rules")})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def indicator_frame(bundle: dict) -> pd.DataFrame:
    return wrap(frame(bundle).copy())


def build_bundle(symbol: str, trade_date: str, config: dict, client=None) -> dict:
    """One preflight fetch; failures remain structured and never become Hold."""
    if config.get("okx_multi_timeframe", False):
        from tradingagents.dataflows.vendors.okx_multitimeframe import build_multitimeframe

        return build_multitimeframe(symbol, trade_date, config, client)
    interval = config.get("okx_interval", "1h")
    bundle = {"source": "OKX API V5 public REST", "exchange": "OKX", "symbol": symbol,
              "interval": interval, "fetched_at_ms": utc_ms(), "status": "DATA_UNAVAILABLE",
              "allow_new_risk": False, "execution_enabled": False, "issues": [], "records": []}
    try:
        if interval not in BARS:
            raise DataQualityError("RESEARCH_INVALID", "Only 1h, 4h and 1d are enabled")
        min_bars = int(config.get("okx_min_bars", 250))
        count = int(config.get("okx_history_bars", 400))
        lag = int(config.get("okx_max_lag_bars", 0))
        if min_bars < 250 or count < min_bars or count > 5000 or lag < 0:
            raise DataQualityError("RESEARCH_INVALID", "Invalid warmup, history or freshness settings")
        client = client or OKXPublicClient()
        server = client.get("/api/v5/public/time")
        server_ms = int(server[0]["ts"])
        if abs(server_ms - utc_ms()) > 60_000:
            raise DataQualityError("RESEARCH_INVALID", "Local/OKX clock skew exceeds 60 seconds")
        if trade_date > datetime.fromtimestamp(server_ms / 1000, UTC).strftime("%Y-%m-%d"):
            raise DataQualityError("RESEARCH_INVALID", "Research date is in the UTC future")
        cutoff = research_cutoff(trade_date, server_ms)
        info = client.instrument(symbol)
        df, notes = validate_candles(client.candles(symbol, interval, cutoff, count),
                                      interval, cutoff, min_bars, count, lag)
        # Validate reported base volume against the actual contract multiplier.
        multiplier = float(info.get("ctMult") or 1) * float(info["ctVal"])
        if not math.isfinite(multiplier) or multiplier <= 0:
            raise DataQualityError("RESEARCH_INVALID", "Invalid contract multiplier")
        if not ((df.Volume - df.VolumeContracts * multiplier).abs()
                <= (df.Volume.abs() * 1e-6 + 1e-8)).all():
            raise DataQualityError("RESEARCH_INVALID", "Contract/base volume unit mismatch")
        records = df.assign(Date=df.Date.map(lambda v: v.isoformat())).to_dict("records")
        if config.get("okx_derivatives_enabled", False):
            from tradingagents.dataflows.vendors.okx_derivatives import (
                collect_derivatives,
                price_oi_evidence,
            )

            extra = collect_derivatives(client, info, cutoff, interval, cutoff < server_ms, config)
            if cutoff == server_ms:
                # Freeze live research AFTER collecting optional observations, so
                # their timestamps never masquerade as evidence before cutoff.
                server_ms = int(client.get("/api/v5/public/time")[0]["ts"])
                if abs(server_ms - utc_ms()) > 60_000:
                    raise DataQualityError("RESEARCH_INVALID", "Local/OKX clock skew exceeds 60 seconds")
                cutoff = research_cutoff(trade_date, server_ms)
                # Reuse the original bars; crossing a close boundary fails closed.
                validate_candles([[r["ts"], r["Open"], r["High"], r["Low"], r["Close"],
                                   r["VolumeContracts"], r["Volume"], r["VolumeQuote"], "1"]
                                  for r in records], interval, cutoff, min_bars, count, lag)
            extra["research_cutoff_ms"] = cutoff
            extra["price_oi_evidence"] = price_oi_evidence(extra, records, interval)
            bundle["derivatives"] = extra
        bundle.update(instrument=info, cutoff_ms=cutoff, server_ms=server_ms, records=records,
                      fetched_at_ms=utc_ms(), latest_close_ms=int(df.ts.iloc[-1]) + BARS[interval][1],
                      volume_units={"Volume": info["ctValCcy"], "VolumeContracts": "contracts",
                                    "VolumeQuote": "USDT"}, issues=notes,
                      instrument_metadata_scope="Current exchange metadata; historical contract settings are not reconstructed",
                      historical=cutoff < server_ms)
        calc = indicator_frame(bundle)
        for name in INDICATORS:
            if not math.isfinite(float(calc[name].iloc[-1])):
                raise DataQualityError("PARTIAL", f"Indicator {name} is not finite after warmup")
        bundle.update(status="VALID", allow_new_risk=True)
        bundle["snapshot_id"] = fingerprint(bundle)
    except DataQualityError as exc:
        bundle.update(status=exc.status, issues=[str(exc)])
    except Exception as exc:
        # Never forward vendor text, credentials or HTTP response bodies to agents.
        bundle.update(status="DATA_UNAVAILABLE", issues=[f"OKX public fetch failed: {type(exc).__name__}"])
    return bundle


def check_bundle(bundle: dict, config: dict, symbol: str | None = None,
                 trade_date: str | None = None) -> dict:
    """Recheck at finalization/output, including checkpoint age and identity."""
    if bundle.get("multi_timeframe") or config.get("okx_multi_timeframe", False):
        from tradingagents.dataflows.vendors.okx_multitimeframe import check_multitimeframe

        return check_multitimeframe(bundle, config, symbol, trade_date)
    result = dict(bundle)
    try:
        if bundle.get("status") != "VALID":
            return {**result, "status": bundle.get("status", "RESEARCH_INVALID"),
                    "issues": bundle.get("issues") or ["No validated OKX research snapshot"],
                    "allow_new_risk": False, "execution_enabled": False}
        if (bundle.get("interval") != config.get("okx_interval", "1h")
                or (symbol is not None and symbol != bundle.get("symbol"))):
            raise DataQualityError("RESEARCH_INVALID", "Cross-instrument or cross-interval bundle")
        validate_instrument(bundle["instrument"], bundle["symbol"])
        if fingerprint(bundle) != bundle["snapshot_id"]:
            raise DataQualityError("RESEARCH_INVALID", "Research snapshot changed")
        if trade_date is not None and research_cutoff(trade_date, bundle["server_ms"]) != bundle["cutoff_ms"]:
            raise DataQualityError("RESEARCH_INVALID", "Research date/cutoff mismatch")
        age = utc_ms() - bundle["fetched_at_ms"]
        if age < -60_000 or age > int(config.get("okx_bundle_ttl_seconds", 900)) * 1000:
            raise DataQualityError("STALE_DATA", "Research bundle expired; fetch a new snapshot")
        rows = [[r["ts"], r["Open"], r["High"], r["Low"], r["Close"],
                 r["VolumeContracts"], r["Volume"], r["VolumeQuote"], "1"] for r in bundle["records"]]
        validate_candles(rows, bundle["interval"], bundle["cutoff_ms"],
                         max(250, int(config.get("okx_min_bars", 250))),
                         int(config.get("okx_history_bars", 400)), int(config.get("okx_max_lag_bars", 0)))
        if not bundle.get("historical"):
            step = BARS[bundle["interval"]][1]
            if utc_ms() // step * step - bundle["latest_close_ms"] > int(config.get("okx_max_lag_bars", 0)) * step:
                raise DataQualityError("STALE_DATA", "A newer closed candle is required")
        return {**result, "allow_new_risk": True, "execution_enabled": False}
    except DataQualityError as exc:
        return {**result, "status": exc.status, "issues": [str(exc)], "allow_new_risk": False}
    except Exception:
        return {**result, "status": "RESEARCH_INVALID", "issues": ["Invalid research bundle"], "allow_new_risk": False}


def describe(bundle: dict) -> str:
    keys = ("status", "source", "exchange", "symbol", "interval", "volume_units", "fetched_at_ms",
            "cutoff_ms", "latest_close_ms", "snapshot_id", "instrument_metadata_scope",
            "allow_new_risk", "execution_enabled", "issues")
    lines = [f"- {k}: {bundle[k]}" for k in keys if k in bundle]
    info = bundle.get("instrument", {})
    fields = ("instId", "instType", "instFamily", "ctType", "settleCcy", "ctVal",
              "ctMult", "ctValCcy", "minSz", "lotSz", "tickSz", "state")
    if info:
        lines += ["", "### Verified contract metadata", ""]
        lines.extend(f"- {k}: {info[k]}" for k in fields if k in info)
    text = "## OKX research data quality\n\n" + "\n".join(lines)
    if bundle.get("multi_timeframe"):
        from tradingagents.dataflows.vendors.okx_multitimeframe import describe_multitimeframe

        text += "\n\n" + describe_multitimeframe(bundle)
    if bundle.get("derivatives"):
        from tradingagents.dataflows.vendors.okx_derivatives import render_derivatives

        text += "\n\n" + render_derivatives(bundle["derivatives"])
    return text


def render_snapshot(bundle: dict) -> str:
    if bundle.get("multi_timeframe"):
        from tradingagents.dataflows.vendors.okx_multitimeframe import render_multitimeframe

        return render_multitimeframe(bundle)
    header = describe(bundle)
    if not bundle.get("allow_new_risk"):
        return header + "\n\nRESEARCH_INVALID: unavailable data says nothing about market direction. No new risk permitted."
    df = frame(bundle)
    calc = indicator_frame(bundle)
    if bundle.get("derivatives"):
        from tradingagents.dataflows.vendors.okx_derivatives import render_derivatives

        header += "\n\n" + render_derivatives(bundle["derivatives"], detailed=True)
    indicators = "\n".join(f"{name}: {float(calc[name].iloc[-1]):.12g}" for name in INDICATORS)
    return header + "\n\n### Closed OHLCV (UTC; Volume is base currency)\n" + df.tail(30).to_csv(index=False) + "\n### Indicators\n" + indicators


def render_prices(bundle: dict, start_date: str, end_date: str) -> str:
    if not bundle.get("allow_new_risk"):
        return render_snapshot(bundle)
    if bundle.get("multi_timeframe"):
        return describe(bundle) + "\n\n" + "\n\n".join(
            f"### {period} prices (UTC; separate timeframe)\n" + render_prices(child, start_date, end_date)
            for period, child in bundle["timeframes"].items())
    df = frame(bundle)
    start = pd.Timestamp(start_date, tz="UTC")
    end = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    selected = df[(df.Date >= start) & (df.Date < end)]
    note = "\nRequested start precedes retained snapshot; history coverage is partial.\n" if start < df.Date.iloc[0] else "\n"
    return describe(bundle) + note + selected.to_csv(index=False)


def render_indicator(bundle: dict, indicator: str, look_back_days: int = 30) -> str:
    if not bundle.get("allow_new_risk"):
        return render_snapshot(bundle)
    if bundle.get("multi_timeframe"):
        return "\n\n".join(f"### {period} {indicator} (periods count {period} candles)\n"
                             + render_indicator(child, indicator, look_back_days)
                             for period, child in bundle["timeframes"].items())
    if indicator not in INDICATORS:
        return f"Unsupported OKX indicator: {indicator}"
    if not 1 <= look_back_days <= 3650:
        return "Invalid indicator lookback: choose 1..3650 days"
    calc = indicator_frame(bundle)
    series = frame(bundle)[["Date"]].copy()
    series[indicator] = calc[indicator].to_numpy()
    # Do not render misleading early-window values before the common warmup.
    series = series.iloc[249:]
    lower = pd.Timestamp(bundle["cutoff_ms"], unit="ms", tz="UTC") - pd.Timedelta(days=look_back_days)
    note = "\nIndicator lookback coverage is partial after warmup.\n" if series.Date.iloc[0] > lower else "\n"
    return describe(bundle) + note + series[series.Date >= lower].to_csv(index=False)
