"""Market-only Responses terminal evidence admission; no transport or retries."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import hashlib
import os
from types import SimpleNamespace

from tradingagents.llm_clients.reliability import LLMUnavailable
from tradingagents.agents.analysts.market_contract import argument_error, diagnose
from tradingagents.dataflows.audit_file import append_audit
from tradingagents.llm_clients.diagnostics import CONTEXT

ACTIVE = ContextVar("market_safe_response", default=None)


def reject(code):
    # Static codes only: no provider data or arbitrary exception text escapes.
    evidence = ACTIVE.get()
    if evidence is not None:
        evidence.diagnostic(code)
    diagnose({"name": None, "args": {}}, "MARKET_SAFE_REJECTION", code)
    raise LLMUnavailable("MARKET_TOOL_PROTOCOL_" + code, partial=True) from None


class Evidence:
    def __init__(self, tools):
        self.tools = {t.name: t for t in tools}
        self.added, self.done, self.deltas, self.slots = {}, {}, {}, {}
        self.arguments_done = {}
        self.completed = None
        self.response_id = None
        self.error = None
        self.events = self.size = 0
        self.logical_call_hash = None
        self.event_counts, self.event_identities = {}, []
        self.first_error_event = None
        self.current_event = None
        self.admission_item = None

    @staticmethod
    def identity(value):
        # Identity hashes only. Never hash parameters or provider body text.
        valid = type(value) is str and 0 < len(value) <= 256
        return {"state": "PRESENT" if valid else "MISSING" if value is None else "INVALID",
                "sha256": hashlib.sha256(value.encode()).hexdigest() if valid else None}

    def bind_context(self):
        ident = self.identity((CONTEXT.get() or {}).get("call_id"))
        if self.logical_call_hash is None:
            self.logical_call_hash = ident["sha256"]

    def diagnostic(self, code):
        path = os.environ.get("TA_SINGLE_AUDIT_FILE")
        if not path:
            return
        terminal, terminal_identities = set(), []
        for index, item in enumerate(getattr(self.completed, "output", ())):
            if getattr(item, "type", None) != "function_call":
                continue
            ident = getattr(item, "id", None)
            if self.identifier(ident):
                terminal.add(ident)
            if len(terminal_identities) < 64:
                terminal_identities.append({"item": self.identity(ident),
                    "call": self.identity(getattr(item, "call_id", None)),
                    "tool_sha256": self.identity(getattr(item, "name", None))["sha256"], "index": index})
        added, done, delta, final = map(set, (self.added, self.done, self.deltas, self.arguments_done))
        differences = {"terminal_without_added": terminal - added, "added_without_terminal": added - terminal,
            "terminal_without_done": terminal - done, "done_without_terminal": done - terminal,
            "orphan_delta": delta - terminal, "orphan_arguments_done": final - terminal}
        def identities(values):
            return {"count": len(values), "item_sha256": sorted(self.identity(v)["sha256"] for v in values)[:64],
                    "truncated": len(values) > 64}
        row = {"kind": "MARKET_EVENT_DIAGNOSTIC", "version": "market-event-diagnostics-v1",
            "code": code, "logical_call_id_sha256": self.logical_call_hash,
            "response_id": self.identity(self.response_id),
            "legacy_rejection_call_id_is_placeholder": True,
            "counts": {"terminal": len(terminal), "added": len(added), "done": len(done),
                       "delta": len(delta), "arguments_done": len(final)},
            "conditions": {"terminal_added_mismatch": terminal != added,
                           "terminal_done_mismatch": terminal != done,
                           "orphan_argument_events": bool((delta | final) - terminal)},
            "unmatched_check_reached": code in {"UNMATCHED_EVENT", "TERMINAL_VERIFIED"},
            "differences": {k: identities(v) for k, v in differences.items()},
            "terminal_identities": terminal_identities,
            "event_counts": self.event_counts, "event_identities": self.event_identities,
            "event_identities_truncated": sum(self.event_counts.values()) > len(self.event_identities),
            "first_error_event": self.first_error_event,
            "admission_item": self.admission_item,
            "completed_event_observed": self.completed is not None}
        append_audit(path, (json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode(), 4 * 1024**2)

    def fail(self, code):
        if self.error is None:
            self.first_error_event = self.current_event
        self.error = self.error or code

    def observe(self, event):
        # Do not interrupt the SDK before its usage tap can account for an
        # accepted response. Admission occurs only AFTER invoke/reconciliation.
        self.bind_context()
        kind = getattr(event, "type", None)
        allowed = {"response.created", "response.in_progress", "response.completed", "response.failed",
                   "response.incomplete", "error", "response.output_item.added", "response.output_item.done",
                   "response.function_call_arguments.delta", "response.function_call_arguments.done"}
        kind = kind if type(kind) is str and kind in allowed else "OTHER"
        item = getattr(event, "item", None)
        index = getattr(event, "output_index", None)
        self.current_event = {"event_type": kind,
            "item": self.identity(getattr(item, "id", None) if item is not None else getattr(event, "item_id", None)),
            "call": self.identity(getattr(item, "call_id", None)),
            "tool_sha256": self.identity(getattr(item, "name", None))["sha256"],
            "index": index if type(index) is int and 0 <= index < 4096 else None}
        self.event_counts[kind] = min(self.event_counts.get(kind, 0) + 1, 8193)
        if len(self.event_identities) < 64:
            self.event_identities.append(self.current_event)
        try:
            self._observe(event)
        except (ValueError, TypeError, AttributeError, RecursionError):
            self.fail("EVENT_STRUCTURE")

    def _observe(self, event):
        self.events += 1
        if self.events > 8192:
            self.fail("LIMIT")
        if self.error:
            return
        if self.completed is not None:
            self.fail("AFTER_COMPLETED")
            return
        kind = getattr(event, "type", None)
        if kind in {"response.created", "response.in_progress", "response.completed"}:
            ident = getattr(getattr(event, "response", None), "id", None)
            if not self.identifier(ident) or not ident.startswith("resp_") or self.response_id not in {None, ident}:
                self.fail("RESPONSE_ID")
            self.response_id = ident
        if kind == "response.completed":
            self.completed = event.response
            return
        if kind in {"response.failed", "response.incomplete", "error"}:
            self.fail("INCOMPLETE")
            return
        if kind not in {"response.output_item.added", "response.output_item.done",
                        "response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            return
        item = getattr(event, "item", None)
        item_id = getattr(item, "id", None) if item is not None else getattr(event, "item_id", None)
        index = getattr(event, "output_index", None)
        if type(index) is not int or not 0 <= index < 4096 or not self.identifier(item_id):
            self.fail("IDENTITY")
            return
        if self.slots.setdefault(index, item_id) != item_id:
            self.fail("INDEX_CONFLICT")
        if any(i != index and ident == item_id for i, ident in self.slots.items()):
            self.fail("INDEX_CONFLICT")
        if kind == "response.function_call_arguments.done":
            value = getattr(event, "arguments", None)
            if item_id in self.arguments_done or type(value) is not str or len(value.encode()) > 65536:
                self.fail("ARGUMENT_FINAL")
                return
            self.arguments_done[item_id] = value
            return
        if kind == "response.function_call_arguments.delta":
            value = getattr(event, "delta", None)
            if type(value) is not str:
                self.fail("ARGUMENT_TYPE")
                return
            self.size += len(value.encode())
            if self.size > 1024 * 1024:
                self.fail("LIMIT")
                return
            self.deltas.setdefault(item_id, []).append(value)
            return
        if getattr(item, "type", None) != "function_call":
            if getattr(item, "type", None) not in {"message", "reasoning"}:
                self.fail("UNEXPECTED_ITEM")
            return
        target = self.added if kind.endswith("added") else self.done
        if item_id in target or len(target) >= 64:
            self.fail("DUPLICATE_OR_LIMIT")
            return
        args = getattr(item, "arguments", None)
        if type(args) is not str or len(args.encode()) > 65536:
            self.fail("ARGUMENT_TYPE")
            return
        target[item_id] = (index, getattr(item, "call_id", None), getattr(item, "name", None), args,
                           getattr(item, "status", None))

    @staticmethod
    def identifier(value):
        return type(value) is str and 0 < len(value) <= 256

    @staticmethod
    def parse(value):
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = item
            return result
        try:
            result = json.loads(value, object_pairs_hook=unique,
                                parse_constant=lambda _: reject("JSON_CONSTANT"))
            if type(result) is not dict:
                reject("ARGUMENT_OBJECT")
            return result
        except (ValueError, TypeError, RecursionError):
            reject("ARGUMENT_JSON")

    @staticmethod
    def same(left, right):
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)

    def admit(self, message):
        if self.error:
            reject(self.error)
        response = self.completed
        if response is None or getattr(response, "status", None) != "completed" or getattr(response, "error", None):
            reject("INCOMPLETE")
        if message.response_metadata.get("id", response.id) != response.id:
            reject("RESPONSE_ID")
        # Reuse the native raw-usage validator and exact normalized comparison.
        # Session.guards has already reconciled the real ledger before invoke
        # returns. The same checks also fail closed outside a guarded session.
        from tradingagents.guarded_research import normalize_usage, normalized_matches
        usage = getattr(response, "usage", None)
        raw = normalize_usage(usage.model_dump()) if hasattr(usage, "model_dump") else None
        result = SimpleNamespace(generations=[SimpleNamespace(message=message)])
        if not raw or not raw.get("valid") or not normalized_matches(result, raw):
            reject("USAGE_UNCONFIRMED")
        calls, seen_items, seen_calls = [], set(), set()
        for output_index, item in enumerate(getattr(response, "output", ())):
            if getattr(item, "type", None) in {"message", "reasoning"}:
                continue
            if getattr(item, "type", None) != "function_call":
                reject("UNEXPECTED_ITEM")
            ident, cid, name = item.id, item.call_id, item.name
            self.admission_item = {"item": self.identity(ident), "call": self.identity(cid),
                                   "tool_sha256": self.identity(name)["sha256"], "index": output_index}
            if not self.identifier(ident) or not self.identifier(cid) or ident in seen_items or cid in seen_calls:
                reject("DUPLICATE_ID")
            seen_items.add(ident); seen_calls.add(cid)
            if ident not in self.added or ident not in self.done:
                reject("MISSING_FINAL_EVIDENCE")
            added, done = self.added[ident], self.done[ident]
            if (added[:3] != done[:3] or done[1:3] != (cid, name)
                    or added[4] not in {None, "in_progress", "completed"}
                    or done[4] not in {None, "completed"} or getattr(item, "status", None) not in {None, "completed"}):
                reject("IDENTITY_OR_STATUS")
            if done[0] != output_index:
                reject("COMPLETE_INDEX")
            if name not in self.tools or type(item.arguments) is not str or len(item.arguments.encode()) > 65536:
                reject("TOOL_OR_ARGUMENT")
            args = self.parse(item.arguments)
            if not self.same(self.parse(done[3]), args):
                reject("FINAL_CONFLICT")
            if ident in self.arguments_done and not self.same(self.parse(self.arguments_done[ident]), args):
                reject("FINAL_CONFLICT")
            fragments = "".join(self.deltas.get(ident, ()))
            # Empty added arguments are normal. Nonempty added arguments are
            # either a complete value or the initial prefix, never a default.
            initial = added[3]
            if fragments:
                if not self.same(self.parse(initial + fragments), args):
                    reject("DELTA_CONFLICT")
            elif initial and not self.same(self.parse(initial), args):
                reject("ADDED_CONFLICT")
            if name == "get_indicators" and argument_error(args):
                reject("SCHEMA")
            try:
                schema = self.tools[name].tool_call_schema
                if set(args) - set(schema.model_fields):
                    reject("SCHEMA_FIELDS")
                schema.model_validate(args)
            except (ValueError, TypeError):
                reject("SCHEMA")
            calls.append({"name": name, "args": args, "id": cid, "type": "tool_call"})
        if (seen_items != set(self.added) or seen_items != set(self.done)
                or (set(self.deltas) | set(self.arguments_done)) - seen_items):
            reject("UNMATCHED_EVENT")
        # Native Market diagnostics use the existing private bounded side-lock
        # sink. Audit failure propagates before any tools can be dispatched.
        for call in calls:
            diagnose(call, "MARKET_SAFE_ADMISSION", "TERMINAL_VERIFIED")
        self.diagnostic("TERMINAL_VERIFIED")
        message.tool_calls = calls
        message.invalid_tool_calls = []
        return message


@contextmanager
def scope(tools):
    if ACTIVE.get() is not None:
        reject("NESTED_SCOPE")
    evidence = Evidence(tools)
    token = ACTIVE.set(evidence)
    try:
        yield evidence
    finally:
        ACTIVE.reset(token)


class ObservedResponse:
    def __init__(self, response, evidence):
        self.response, self.evidence = response, evidence

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    def __iter__(self):
        try:
            for event in self.response:
                self.evidence.observe(event)
                yield event
        except BaseException:
            self.evidence.diagnostic("STREAM_INTERRUPTED")
            raise
        if self.evidence.completed is None:
            self.evidence.diagnostic("STREAM_WITHOUT_COMPLETED")


class ObservedResponses:
    def __init__(self, responses, evidence):
        self.responses, self.evidence = responses, evidence

    def create(self, **kwargs):
        self.evidence.bind_context()
        if kwargs.get("stream") is not True:
            reject("STREAM_REQUIRED")
        return ObservedResponse(self.responses.create(**kwargs), self.evidence)


class ObservedRoot:
    def __init__(self, root, evidence):
        self.responses = ObservedResponses(root.responses, evidence)

