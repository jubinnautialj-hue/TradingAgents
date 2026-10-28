"""Market-only presentation, never a replacement for archived calculation data.

Original tools still validate and calculate on their full pinned bundles. Only
numeric CSV presentation is bounded; prose, quality flags and exact verified
snapshot evidence are retained. No network, model summarisation or state writes.
"""
from __future__ import annotations

import csv
import hashlib
import inspect
import io
import json
import math
from decimal import Decimal, InvalidOperation
from functools import wraps

from langchain_core.messages import AIMessage, ToolMessage

VERSION = "market-display-v1"
TAIL_ROWS = 30
TOOLS = frozenset({"get_stock_data", "get_indicators", "get_verified_market_snapshot"})


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _table_summary(rows):
    """Exact decimal extrema and missing counts over ALL displayed-range rows."""
    summary = {}
    for key in rows[0]:
        if key == "Date":
            continue
        values = []
        missing = []
        for row in rows:
            try:
                value = Decimal(row[key])
                if not value.is_finite():
                    raise InvalidOperation
                values.append((value, row["Date"], row[key]))
            except (InvalidOperation, TypeError, ValueError):
                missing.append({"at": row["Date"], "value": row[key]})
        field = {"first": rows[0][key], "last": rows[-1][key], "missing_or_nonfinite": missing}
        if values:
            low, high = min(values, key=lambda x: x[0]), max(values, key=lambda x: x[0])
            field.update(min=low[2], min_at=low[1], max=high[2], max_at=high[1])
            if key.startswith("Volume"):
                field["sum_over_valid_rows"] = str(sum((x[0] for x in values), Decimal(0)))
        summary[key] = field
    return summary


def bound_csv_tables(text):
    """Retain all non-table evidence and a traceable summary + last 30 rows.

    Unrecognised/malformed text is left intact, never silently truncated. Hashes
    identify the full original rendered table, not a new research snapshot.
    """
    lines = text.splitlines(keepends=True)
    output, index = [], 0
    while index < len(lines):
        if not lines[index].startswith("Date,"):
            output.append(lines[index])
            index += 1
            continue
        end = index + 1
        columns = next(csv.reader([lines[index]]))
        while end < len(lines):
            fields = next(csv.reader([lines[end]]))
            if len(fields) != len(columns) or not fields[0][:4].isdigit():
                break
            end += 1
        original = "".join(lines[index:end])
        rows = list(csv.DictReader(io.StringIO(original)))
        if len(rows) <= TAIL_ROWS or len(set(columns)) != len(columns):
            output.append(original)
        else:
            evidence = {
                "presentation": VERSION, "full_table_sha256": _hash(original),
                "full_rows": len(rows), "shown_tail_rows": TAIL_ROWS,
                "range_start_utc": rows[0]["Date"], "range_end_utc": rows[-1]["Date"],
                "all_range_fields": _table_summary(rows),
                "limitation": "Earlier row sequence omitted from model display; do not infer unshown patterns. "
                              "Full rows remain in pinned snapshot; indicator warmup/calculation unchanged.",
            }
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows[-TAIL_ROWS:])
            output.append("[Bounded deterministic table]\n" + json.dumps(evidence, ensure_ascii=False)
                          + "\n" + buffer.getvalue())
        index = end
    return "".join(output)


def _moving_averages(bundle):
    """Supplemental SMA20/60/120 from FULL retained closes, no strategy changes."""
    from tradingagents.dataflows.vendors import okx

    children = bundle.get("timeframes") if bundle.get("multi_timeframe") else {bundle.get("interval"): bundle}
    result = {}
    for period, child in children.items():
        closes = okx.frame(child).Close
        result[period] = {
            "snapshot_id": child.get("snapshot_id"),
            "latest_close_ms": child.get("latest_close_ms"),
            "full_input_rows": len(closes),
            "supplemental_sma_not_strategy_signal": {
                f"MA{window}": float(closes.rolling(window, min_periods=window).mean().iloc[-1])
                if len(closes) >= window else None for window in (20, 60, 120)
            },
        }
    return "\nSupplemental full-input deterministic moving averages (periods are candles):\n" + json.dumps(result)


def market_tool(original):
    """Same native tool schema/name and validators; OKX Market presentation only."""
    signature = inspect.signature(original.func)

    @wraps(original.func)
    def display(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        raw = original.func(*args, **kwargs)
        bundle = bound.arguments.get("market_snapshot")
        if not bundle or bundle.get("exchange") != "OKX":
            return raw  # Stocks and every other analyst retain the original tools.
        if original.name == "get_verified_market_snapshot":
            return raw  # Preserve full deterministic structure/derivative evidence.
        rendered = bound_csv_tables(raw)
        if (original.name == "get_stock_data" and bundle.get("allow_new_risk")
                and "- allow_new_risk: True" in raw
                and "- allow_new_risk: False" not in raw and "RESEARCH_INVALID:" not in raw):
            rendered += _moving_averages(bundle)
        return rendered

    return original.model_copy(update={"func": display})


def _argument_identity(args):
    """Canonical JSON only; unsupported values mean identity is unconfirmed."""
    def plain(value):
        if type(value) in (str, int, bool, type(None)):
            return True
        if type(value) is float:
            return math.isfinite(value)
        if type(value) is list:
            return all(plain(item) for item in value)
        if type(value) is dict:
            return all(type(key) is str and plain(item) for key, item in value.items())
        return False

    try:
        if type(args) is dict and plain(args):
            return json.dumps(args, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        pass
    return None


def _visible_identity(text):
    """Native pinned tools print this header; absence means unconfirmed evidence."""
    fields = ("source", "exchange", "symbol", "interval", "snapshot_id",
              "fetched_at_ms", "cutoff_ms", "latest_close_ms")
    found = {}
    for line in text.splitlines():
        if line.startswith("- ") and ": " in line:
            name, value = line[2:].split(": ", 1)
            if name in fields and name not in found:
                found[name] = value
    if any(found.get(field) in (None, "", "None", "UNKNOWN") for field in fields):
        return None
    snapshot = found["snapshot_id"]
    if len(snapshot) != 64 or any(c not in "0123456789abcdef" for c in snapshot):
        return None
    return tuple(found[field] for field in fields)


def project_history(messages):
    """Fold only confirmed same-call-arguments and same-body evidence.

    The first full evidence stays in the prompt. Preserve native tool_call_id,
    message IDs and ordering; never mutate graph history or dedup across runs.
    Any artifact or tool metadata is conservatively excluded, without comparing
    or stringifying it. Missing/ambiguous call associations are never folded.
    """
    calls, result_counts = {}, {}
    for position, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_id = call.get("id")
                if type(call_id) is str and call_id:
                    identity = _argument_identity(call.get("args"))
                    info = (position, call.get("name"), identity)
                    calls[call_id] = None if call_id in calls else info
        elif isinstance(message, ToolMessage):
            result_counts[message.tool_call_id] = result_counts.get(message.tool_call_id, 0) + 1

    seen, projected = {}, []
    for position, message in enumerate(messages):
        call = calls.get(message.tool_call_id) if isinstance(message, ToolMessage) else None
        if (isinstance(message, ToolMessage) and message.name in TOOLS
                and isinstance(message.content, str) and len(message.content) > 2048
                and message.artifact is None
                and not message.additional_kwargs and not message.response_metadata
                and message.status == "success"
                and result_counts[message.tool_call_id] == 1
                and call is not None and call[0] < position and call[1] == message.name
                and call[2] is not None and _visible_identity(message.content) is not None):
            key = (message.name, call[2], _hash(message.content))
            if key in seen:
                first_position, first_call = seen[key]
                content = (f"[Identical evidence retained above at message {first_position}; "
                           f"tool_call_id={first_call}; tool={message.name}; sha256={key[2]}; "
                           f"args_sha256={_hash(call[2])}; identity=market-tool-identity-v1] "
                           "No new observation or freshness extension. Use that original result, including all risks.")
                message = message.model_copy(update={"content": content})
            else:
                seen[key] = (position, message.tool_call_id)
        projected.append(message)
    return projected
