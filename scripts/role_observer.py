"""Administrative metadata-only observer. Never changes model or budget calls."""
import hashlib
import json
import os
import time
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook
from tradingagents.dataflows.audit_file import append_audit

ROLES = (
    "Market Analyst", "News Analyst", "Sentiment Analyst", "Bull Researcher",
    "Bear Researcher", "Research Manager", "Trader", "Aggressive Analyst",
    "Neutral Analyst", "Conservative Analyst", "Portfolio Manager",
)
AUDIT_LIMIT = 4 * 1024**2
VERSION = "role-observer-audit-v1"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def count(value):
    return value if type(value) is int and value >= 0 else None


def role_from(metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    node = metadata.get("langgraph_node")
    namespace = metadata.get("langgraph_checkpoint_ns")
    matches = [role for role in ROLES if node == role or
               (isinstance(namespace, str) and role + ":" in namespace)]
    return matches[0] if len(matches) == 1 else "UNKNOWN"


def write(event):
    """Only explicitly constructed metadata reaches this append-only file."""
    path = os.environ.get("TA_SINGLE_AUDIT_FILE")
    if not path:
        return
    event["at_ms"] = time.time_ns() // 1_000_000
    data = (json.dumps(event, sort_keys=True) + "\n").encode()
    append_audit(path, data, AUDIT_LIMIT)



class RoleObserver(BaseCallbackHandler):
    run_inline = True
    raise_error = True  # Audit failure must propagate through LangChain callbacks.

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        # No serialized model/config, raw metadata, argument values or bodies.
        stats = []
        for message in messages[0] if len(messages) == 1 else []:
            content = getattr(message, "content", None)
            item = {"type": getattr(message, "type", "UNKNOWN")}
            if isinstance(content, str):
                item.update(bytes=len(content.encode()), characters=len(content), sha256=sha(content))
            if item["type"] == "tool":
                name = getattr(message, "name", None)
                item["tool"] = name if name in {"get_stock_data", "get_indicators", "get_verified_market_snapshot"} else "OTHER"
                item["folded_reference"] = isinstance(content, str) and content.startswith("[Identical evidence retained")
                item["bounded_table"] = isinstance(content, str) and "[Bounded deterministic table]" in content
            stats.append(item)
        write({"kind": "MODEL_START", "callback_id": str(run_id), "role": role_from(metadata),
               "role_basis": "LANGGRAPH_METADATA_ONLY", "message_stats": stats})

    def on_llm_end(self, response, *, run_id, **kwargs):
        groups = getattr(response, "generations", [])
        message = groups[0][0].message if len(groups) == 1 and len(groups[0]) == 1 else None
        usage = getattr(message, "usage_metadata", None) or {}
        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        response_id = (getattr(message, "response_metadata", None) or {}).get("id")
        calls = getattr(message, "tool_calls", None) or []
        names = {"get_stock_data", "get_indicators", "get_verified_market_snapshot"}
        write({"kind": "MODEL_END", "callback_id": str(run_id),
               "response_hash": sha(response_id) if isinstance(response_id, str) and response_id.startswith("resp_") else None,
               "usage_source": "SDK_CALLBACK_NORMALIZED_NOT_AUTHORITATIVE_LEDGER",
               "input_tokens": count(usage.get("input_tokens")),
               "output_tokens": count(usage.get("output_tokens")),
               "total_tokens": count(usage.get("total_tokens")),
               "cached_input_tokens": count(input_details.get("cache_read")),
               "tool_names": [c.get("name") if c.get("name") in names else "OTHER" for c in calls],
               "reasoning_tokens": count(output_details.get("reasoning"))})

    def on_llm_error(self, error, *, run_id, **kwargs):
        write({"kind": "MODEL_ERROR", "callback_id": str(run_id),
               "exception_type": type(error).__name__})


def install():
    register_configure_hook(ContextVar("ta_single_observer", default=None), True,
                            RoleObserver, "TA_SINGLE_ROLE_OBSERVER")
