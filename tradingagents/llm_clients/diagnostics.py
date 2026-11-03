"""Opt-in metadata only, using the existing private, bounded audit sink."""
import hashlib
import json
import os
import re
import time
from contextvars import ContextVar

from tradingagents.dataflows.audit_file import append_audit

CONTEXT = ContextVar("model_diagnostic_context", default=None)
STAGES = {"HTTP_RESPONSE", "HTTP_ERROR", "STREAM_FIRST_CHUNK", "STREAM_END", "STREAM_ERROR",
          "SDK_RESULT", "SDK_PARSE_ERROR", "USAGE_CONFIRMED", "CALL_ERROR",
          "RAW_TOOL_ARGUMENTS", "OI_UNIT_CHECK"}


def identifier(value):
    if not isinstance(value, str):
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else hashlib.sha256(value.encode()).hexdigest()


def context_metadata():
    ctx = CONTEXT.get() or {}
    return {k: ctx[k] for k in ("call_id", "wire_id", "transport_http_status", "request_id_sha256") if k in ctx}


def emit(stage, *, received=None, completed=None, confirmed=None, attempt=None,
         failure=None, tool_id=None, argument_summary=None, unit_summary=None):
    """Fixed schema: callers cannot pass arbitrary exception text or headers."""
    if stage not in STAGES:
        raise ValueError("Unregistered diagnostic stage")
    path = os.environ.get("TA_SINGLE_AUDIT_FILE")
    if not path:
        return
    row = {"kind": "RESEARCH_DIAGNOSTIC", "version": "research-diagnostics-v1",
           "stage": stage, "at_ms": time.time_ns() // 1_000_000, **context_metadata()}
    for key, value in (("chunk_received", received), ("stream_completed", completed), ("usage_confirmed", confirmed)):
        if type(value) is bool:
            row[key] = value
    if type(attempt) is int and 0 <= attempt <= 100:
        row["attempt"] = attempt
    if tool_id is not None:
        row["tool_call_id"] = identifier(tool_id)
    if failure is not None:
        # safe_diagnostics is the existing provider-code allowlist, not str(exc).
        from .reliability import safe_diagnostics
        chain, seen, current = [], set(), failure
        while current is not None and id(current) not in seen and len(chain) < 12:
            seen.add(id(current))
            chain.append(current)
            current = current.__cause__ or current.__context__
        row["error"] = safe_diagnostics(chain)
        allowed = {
            "APIError", "LLMUnavailable", "ValueError", "TypeError", "KeyError",
            "APIConnectionError", "APITimeoutError", "RateLimitError", "ConnectError",
            "ConnectTimeout", "ReadTimeout", "ReadError", "RemoteProtocolError", "SSLError"}
        row["exception_types"] = [type(e).__name__ if type(e).__name__ in allowed else "OTHER" for e in chain]
    if argument_summary is not None:
        row["arguments"] = argument_summary
    if unit_summary is not None:
        row["units"] = unit_summary
    append_audit(path, (json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode(), 4 * 1024**2)


def arguments(value):
    """Never hash/log values; summarize only bounded JSON object structure."""
    state = "OBJECT"
    if isinstance(value, str):
        if len(value) > 65536:
            return {"state": "LIMIT_UNKNOWN"}
        if not value.strip():
            return {"state": "EMPTY_STRING"}
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {"state": "INVALID_JSON"}
    if type(value) is not dict:
        return {"state": "MISSING" if value is None else "NON_OBJECT"}
    keys = ("indicator", "curr_date", "look_back_days")
    types = {k: type(value[k]).__name__ if type(value[k]) in (str, int, float, bool, list, dict, type(None))
             else "OTHER" for k in keys if k in value}
    shape = {"container": "dict", "fields": types, "unknown_fields": sum(k not in keys for k in value)}
    return {"state": "EMPTY_OBJECT" if not value else state,
            "present": {k: k in value for k in keys},
            "structure_sha256": hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()}


def observe_response_event(chunk):
    """Called before the native Responses event-to-message converter."""
    if getattr(chunk, "type", None) == "response.output_item.done":
        item = getattr(chunk, "item", None)
        if getattr(item, "type", None) == "function_call" and getattr(item, "name", None) == "get_indicators":
            emit("RAW_TOOL_ARGUMENTS", tool_id=getattr(item, "call_id", None),
                 argument_summary=arguments(getattr(item, "arguments", None)))
