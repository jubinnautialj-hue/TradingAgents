"""Market argument admission and metadata-only diagnostics; no I/O clients."""
import hashlib
import json
import logging
import os
import re
import threading
from datetime import date

from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from tradingagents.agents.tools import get_indicators
from tradingagents.dataflows.vendors.okx import INDICATORS

logger = logging.getLogger(__name__)
VERSION = "market-tool-contract-v1"
AUDIT_LIMIT = 4 * 1024**2
_AUDIT_LOCK = threading.Lock()
FIELDS = {
    "get_indicators": ("indicator", "curr_date", "look_back_days"),
    "get_stock_data": ("start_date", "end_date"),
    "get_verified_market_snapshot": ("curr_date", "look_back_days"),
}
INJECTED = {"symbol", "trade_date", "market_snapshot"}


class IndicatorParameters(BaseModel):
    """Validate model arguments before native state injection."""
    model_config = ConfigDict(extra="forbid")
    indicator: StrictStr
    curr_date: StrictStr
    look_back_days: StrictInt = Field(default=30, ge=1, le=3650)

    @field_validator("indicator")
    @classmethod
    def indicator_names(cls, value):
        names = value.split(",")
        if not names or any(not n.strip() for n in names):
            raise PydanticCustomError("empty_indicator", "Nonempty indicator required")
        if any(n.strip().lower() not in INDICATORS for n in names):
            raise PydanticCustomError("unknown_indicator", "Unsupported indicator")
        return value  # Admission never rewrites model arguments.

    @field_validator("curr_date")
    @classmethod
    def research_date(cls, value):
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError:
            raise PydanticCustomError("invalid_date", "YYYY-MM-DD date required") from None
        return value


class MarketIndicatorArgs(get_indicators.args_schema, IndicatorParameters):
    """Retain injected annotations and public validators/constraints."""
    model_config = ConfigDict(extra="forbid")
    indicator: StrictStr
    curr_date: StrictStr
    look_back_days: StrictInt = Field(default=30, ge=1, le=3650)


def _identifier(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    # Preserve correlation for malformed string IDs without logging their text.
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else "MISSING_ID"


def diagnose(call, stage, code="OBSERVED"):
    """Log a fixed allowlist bitmap and type shape, never argument values/keys."""
    name = call.get("name")
    safe_name = name if name in FIELDS else "OTHER"
    args = call.get("args")
    keys = FIELDS.get(safe_name, ())
    shape = {k: type(args[k]).__name__ for k in keys if type(args) is dict and k in args}
    # Type names are restricted too; custom object class names are untrusted.
    shape = {k: v if v in {"str", "int", "float", "bool", "list", "dict", "NoneType"}
             else "OTHER" for k, v in shape.items()}
    description = {"container": "dict" if type(args) is dict else "OTHER", "fields": shape,
                   "unknown_fields": sum(k not in keys for k in args) if type(args) is dict else 0}
    event = {"version": VERSION, "stage": stage, "code": code,
             "call_id": _identifier(call.get("id")), "tool": safe_name,
             "present": {k: type(args) is dict and k in args for k in keys},
             "structure_sha256": hashlib.sha256(json.dumps(description, sort_keys=True).encode()).hexdigest()}
    # Reuse the existing opt-in metadata audit sink used by controlled research.
    # One bounded append; no raw arguments, exception text, or environment dump.
    path = os.environ.get("TA_SINGLE_AUDIT_FILE")
    if path:
        row = (json.dumps({"kind": "MARKET_TOOL_DIAGNOSTIC", **event}, sort_keys=True) + "\n").encode()
        with _AUDIT_LOCK:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                if os.fstat(fd).st_size + len(row) > AUDIT_LIMIT:
                    raise RuntimeError("MARKET_DIAGNOSTIC_CAPACITY")
                if os.write(fd, row) != len(row):
                    raise RuntimeError("MARKET_DIAGNOSTIC_INCOMPLETE")
                os.fsync(fd)
            finally:
                os.close(fd)
    logger.info("MARKET_TOOL_DIAGNOSTIC %s", json.dumps(event, sort_keys=True))
    return event


def observe_model_result(message):
    """Observe parsed AIMessage only, not raw provider/stream responses."""
    for call in getattr(message, "tool_calls", ()):
        diagnose(call, "MODEL_PARSED")
    for call in getattr(message, "invalid_tool_calls", ()):
        diagnose(call, "MODEL_PARSED", "UNPARSED_ARGUMENTS")


def argument_error(args):
    if type(args) is not dict:
        return "PARAMETER_OBJECT_REQUIRED"
    if INJECTED.intersection(args):
        return "INJECTED_ARGUMENT_CONFLICT"
    if any(k not in FIELDS["get_indicators"] for k in args):
        return "UNKNOWN_PARAMETER"
    try:
        IndicatorParameters.model_validate(args)
    except ValidationError as exc:
        errors = {e["type"] for e in exc.errors(include_input=False, include_url=False)}
        # Fixed precedence and codes; do not log exception strings or input.
        for kind, code in (("missing", "MISSING_REQUIRED"),
                           ("empty_indicator", "EMPTY_INDICATOR"),
                           ("unknown_indicator", "UNKNOWN_INDICATOR"),
                           ("invalid_date", "INVALID_DATE"),
                           ("int_type", "LOOKBACK_TYPE"),
                           ("greater_than_equal", "LOOKBACK_RANGE"),
                           ("less_than_equal", "LOOKBACK_RANGE")):
            if kind in errors:
                return code
        return "ARGUMENT_TYPE"
    return None


def admit_market_tool(request, execute):
    """ToolNode wrapper runs before native injection; never retry or mutate."""
    call = request.tool_call
    diagnose(call, "TOOLNODE_ENTRY")
    if call.get("name") == "get_indicators":
        error = argument_error(call.get("args"))
        if error:
            diagnose(call, "ARGUMENT_VALIDATION", error)
            return ToolMessage(content=f"MARKET_TOOL_ARGUMENT_ERROR: {error}. "
                               "Required: indicator, curr_date; optional: look_back_days (integer 1..3650). "
                               "Use registered indicator names; no injected or unknown fields.",
                               name="get_indicators", tool_call_id=call["id"], status="error")
    result = execute(request)
    diagnose(call, "TOOL_RESULT", "TOOL_ERROR" if getattr(result, "status", None) == "error" else "RETURNED")
    return result
