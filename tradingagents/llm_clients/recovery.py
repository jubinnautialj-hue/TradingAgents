"""Research-only, serialized retries and complete-response checkpoints.

No tools, orders, account operations, credential persistence or partial replay.
The caller must disable SDK retries so this is the sole retry owner.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.messages import AIMessage

from .reliability import LLMUnavailable, classify_failure

TERMINAL = {"QUOTA_EXHAUSTED", "AUTHENTICATION_OR_PERMISSION", "AUTH_REFRESH_UNAVAILABLE", "TLS_ERROR"}
RETRYABLE = {"CONNECT_FAILURE", "RATE_LIMIT", "TRANSIENT_SERVICE_ERROR"}


def can_retry(summary):
    if summary.get("partial_response") or summary.get("kind") not in RETRYABLE:
        return False
    if summary["kind"] == "RATE_LIMIT":
        fields = summary.get("diagnostics", {})
        # HTTP 429 alone cannot distinguish a transient limit from quota loss.
        return bool({fields.get("provider_code"), fields.get("provider_type")} &
                    {"rate_limit_exceeded", "rate_limit_error", "slow_down"})
    return True


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode()).hexdigest()


class RecoveryController:
    """One in-flight request per campaign, including backoff; fail closed."""

    def __init__(self, *, max_attempts=3, min_interval=2., max_delay=30.,
                 sleep=time.sleep, clock=time.monotonic, jitter=random.random):
        if not 1 <= max_attempts <= 3 or not all(math.isfinite(x) and x >= 0 for x in (min_interval, max_delay)):
            raise ValueError("Invalid bounded retry policy")
        self.max_attempts, self.min_interval, self.max_delay = max_attempts, min_interval, max_delay
        self.sleep, self.clock, self.jitter = sleep, clock, jitter
        self.lock, self.stopped, self.last_start = threading.Lock(), None, None


class RoleJournal:
    """Single-writer journal. STARTED survives crashes; never silently replay it."""

    def __init__(self, path, identity, *, imported=()):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data["identity"] != identity or self.data.get("imported") != list(imported):
                raise ValueError("Checkpoint identity changed")
        else:
            self.data = {"identity": copy.deepcopy(identity), "roles": [],
                         "imported": copy.deepcopy(list(imported)), "execution_enabled": False}
            self.save()
        if self.data.get("execution_enabled") is not False:
            raise ValueError("Unsafe checkpoint")
        self.index = 0

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix+".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(tmp, self.path)


class RecoveringModel:
    """Facade used inside the original AuditedModel/native nine-role graph.

    Reconstruct graph state by replaying complete outputs against exact original
    prompt/tier/schema signatures. A failed/partial output is never a checkpoint.
    """

    def __init__(self, backend, journal, controller, tier, schema=None):
        self.backend, self.journal, self.controller = backend, journal, controller
        self.tier, self.schema = tier, schema

    def bind_tools(self, *args, **kwargs):
        raise ValueError("Recovery is text/schema research only")

    def with_structured_output(self, schema, *, include_raw=False):
        if not include_raw:
            raise ValueError("Recovery requires raw response audit")
        return RecoveringModel(self.backend, self.journal, self.controller, self.tier, schema)

    def _validated(self, record):
        if record.get("model_metadata", {}).get("finish_reason") in {"length", "content_filter"}:
            raise ValueError("Truncated role cannot be reused")
        if not record.get("output") and not self.schema:
            raise ValueError("Empty completed role")
        tool_calls = record.get("schema_tool_calls", [])
        if tool_calls and not self.schema:
            raise ValueError("Unexpected external tool")
        parsed = self.schema.model_validate(record["parsed"]) if self.schema else None
        if self.schema and any(c.get("name") != self.schema.__name__ for c in tool_calls):
            raise ValueError("Unexpected schema tool")
        raw = AIMessage(content=record.get("output", ""), tool_calls=tool_calls,
                        usage_metadata=record.get("usage"), response_metadata=record.get("model_metadata", {}))
        return {"raw": raw, "parsed": parsed, "parsing_error": None} if self.schema else raw

    def invoke(self, prompt):
        if not isinstance(prompt, str):
            raise ValueError("String prompts required")
        journal, control = self.journal, self.controller
        signature = {"prompt_sha256": digest(prompt), "tier": self.tier,
                     "schema": self.schema.model_json_schema() if self.schema else None}
        index = journal.index
        if index >= 9:
            raise ValueError("Native role budget exceeded")
        if index < len(journal.data["imported"]):
            old = journal.data["imported"][index]
            if old["input"] != prompt or old["tier"] != self.tier or old.get("schema") != (self.schema.__name__ if self.schema else None):
                raise ValueError("Imported role prompt/tier/schema mismatch")
            if old["status"] != "COMPLETED":
                raise ValueError("Partial role cannot be reused")
            value = self._validated(old)
            journal.index += 1
            return value
        slot = index-len(journal.data["imported"])
        roles = journal.data["roles"]
        if slot == len(roles):
            roles.append({"signature": signature, "attempts": [], "status": "PENDING"})
            journal.save()
        record = roles[slot]
        if record["signature"] != signature:
            raise ValueError("Checkpoint prompt/tier/schema mismatch")
        if record["status"] == "COMPLETED":
            if record.get("result_hash") != digest(record["result"]):
                raise ValueError("Completed role checkpoint changed")
            value = self._validated(record["result"])
            journal.index += 1
            return value
        attempts = record["attempts"]
        if attempts:
            previous = attempts[-1]
            failure = previous.get("failure", {})
            if previous["status"] == "STARTED" or not can_retry(failure):
                raise LLMUnavailable("UNSAFE_AUTOMATIC_REPLAY", partial=failure.get("partial_response", False))
        with control.lock:
            while len(attempts) < control.max_attempts:
                if control.stopped:
                    raise LLMUnavailable(control.stopped)
                if attempts:
                    failure = attempts[-1]["failure"]
                    delay = max(2**(len(attempts)-1)+control.jitter(),
                                failure.get("diagnostics", {}).get("retry_after_seconds", 0))
                    if delay > control.max_delay:
                        raise LLMUnavailable("RETRY_AFTER_EXCEEDS_BUDGET")
                    control.sleep(delay)
                if control.last_start is not None:
                    control.sleep(max(0., control.min_interval-(control.clock()-control.last_start)))
                control.last_start = control.clock()
                attempt = {"number": len(attempts)+1, "status": "STARTED", "usage": None,
                           "started_at_utc": datetime.now(UTC).isoformat(),
                           "completed_roles": len(journal.data["imported"])+sum(r["status"] == "COMPLETED" for r in roles)}
                attempts.append(attempt)
                journal.save()  # Before contacting the service.
                start = control.clock()
                try:
                    if self.schema:
                        envelope = self.backend.with_structured_output(self.schema, include_raw=True).invoke(prompt)
                        raw = envelope["raw"]
                    else:
                        raw = self.backend.invoke(prompt)
                        envelope = None
                    attempt["usage"] = getattr(raw, "usage_metadata", None)
                    if envelope and (envelope.get("parsing_error") or envelope.get("parsed") is None):
                        raise ValueError("STRUCTURED_FORMAT_ERROR")
                    parsed = envelope["parsed"] if envelope else None
                    result = {"output": raw.content, "schema_tool_calls": getattr(raw, "tool_calls", []),
                              "parsed": parsed.model_dump(mode="json") if hasattr(parsed, "model_dump") else parsed,
                              "usage": attempt["usage"],
                              "model_metadata": {k: v for k, v in getattr(raw, "response_metadata", {}).items()
                                                 if k in {"model_name", "model", "finish_reason", "system_fingerprint"}}}
                    if result["model_metadata"].get("finish_reason") in {"length", "content_filter"}:
                        raise LLMUnavailable("STREAM_INCOMPLETE", partial=True)
                    value = self._validated(result)
                    record.update(status="COMPLETED", result=result, result_hash=digest(result))
                    attempt["status"] = "COMPLETED"
                    journal.index += 1
                    return value
                except Exception as exc:
                    failure = classify_failure(exc)
                    attempt.update(status="FAILED", exception_type=type(exc).__name__)
                    record["status"] = "FAILED"
                    if failure:
                        attempt["failure"] = failure.summary()
                        if failure.kind in TERMINAL:
                            control.stopped = failure.kind
                        if can_retry(failure.summary()) and len(attempts) < control.max_attempts:
                            continue
                        raise failure from None
                    raise
                finally:
                    attempt["elapsed_seconds"] = control.clock()-start
                    journal.save()
            raise LLMUnavailable("RETRY_BUDGET_EXHAUSTED")
