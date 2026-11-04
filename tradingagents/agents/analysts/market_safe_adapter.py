"""Market-only Responses terminal evidence admission; no transport or retries."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import hashlib
import os
import re
from functools import lru_cache
from typing import get_args
from types import SimpleNamespace

from tradingagents.llm_clients.reliability import LLMUnavailable
from tradingagents.agents.analysts.market_contract import argument_error, diagnose
from tradingagents.dataflows.audit_file import append_audit
from tradingagents.llm_clients.diagnostics import CONTEXT

ACTIVE = ContextVar("market_safe_response", default=None)


@lru_cache(maxsize=1)
def sdk_event_names():
    """Diagnostic vocabulary only; never an admission allowlist."""
    from openai.types.responses import ResponseStreamEvent
    return frozenset(name for cls in get_args(get_args(ResponseStreamEvent)[0])
                     for name in get_args(cls.model_fields['type'].annotation))


def event_type_summary(value):
    # Never log arbitrary provider text disguised as an event type.
    if value is None:
        return {"name": None, "classification": "MISSING"}
    if type(value) is not str:
        return {"name": None, "classification": "INVALID_TYPE"}
    if len(value) > 128 or not re.fullmatch(r"response\.[a-z_]+(?:\.[a-z_]+)*|error", value):
        return {"name": None, "classification": "REDACTED_NAME"}
    return {"name": value, "classification": "SDK_KNOWN" if value in sdk_event_names() else "UNKNOWN_NAME"}


def terminal_structure(response):
    """Metadata projection only; never a fallback source for admission."""
    from openai.types.responses import Response
    row = {"version": "market-terminal-structure-v1",
           "object_type": ("NONE" if response is None else "Response" if type(response) is Response
                           else "dict" if type(response) is dict else "SimpleNamespace"
                           if type(response) is SimpleNamespace else "OTHER"),
           "output_present": None, "output_container": "UNKNOWN", "output_length": None,
           "item_type_counts": {}, "items_scanned": 0, "truncated": False,
           "classification": "UNRECOGNIZED"}
    missing = object()
    try:
        # A dict is described as a dict; it is NOT coerced into an SDK object.
        output = response.get("output", missing) if type(response) is dict else getattr(response, "output", missing)
        row["output_present"] = output is not missing
        if output is missing:
            row.update(output_container="MISSING", classification="OUTPUT_MISSING")
        elif output is None:
            row.update(output_container="NONE", classification="OUTPUT_NONE")
        elif type(output) in (list, tuple):
            row.update(output_container="list" if type(output) is list else "tuple",
                       output_length=len(output), truncated=len(output) > 256)
            counts = {}
            for item in output[:256]:
                kind = item.get("type") if type(item) is dict else getattr(item, "type", None)
                kind = kind if type(kind) is str and kind in {"function_call", "message", "reasoning"} else "OTHER"
                counts[kind] = counts.get(kind, 0) + 1
            row.update(item_type_counts=counts, items_scanned=min(len(output), 256))
            if not row["truncated"]:
                row["classification"] = ("EMPTY_ARRAY" if not output else "CONTAINS_FUNCTION_CALL"
                    if counts.get("function_call") else "REASONING_ONLY" if set(counts) == {"reasoning"}
                    else "NON_TOOL_ITEMS" if "OTHER" not in counts else "UNRECOGNIZED")
        else:
            row["output_container"] = "OTHER"
    except Exception:
        # Property/shape failures are metadata-unknown, not new rejection causes.
        row.update(classification="UNRECOGNIZED", inspection_failed=True)
    return row


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
        self.sdk_terminal_structure = None
        self.stream_exhausted = False
        self.item_phases = {}
        self.last_sequence = None
        self.sequence_valid = True
        self.event_response_ids = set()
        self.reconciliation = None
        self.first_event_type = None
        self.other_events = []

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
        try:
            output = getattr(self.completed, "output", ())
        except Exception:
            output = ()
        # Diagnostics must not re-raise a malformed output's TypeError while
        # reporting the original failure. This does not affect admit().
        output = output[:256] if type(output) in (list, tuple) else ()
        for index, item in enumerate(output):
            try:
                if getattr(item, "type", None) != "function_call":
                    continue
                ident = getattr(item, "id", None)
                cid, name = getattr(item, "call_id", None), getattr(item, "name", None)
            except Exception:
                continue
            if self.identifier(ident):
                terminal.add(ident)
            if len(terminal_identities) < 64:
                terminal_identities.append({"item": self.identity(ident),
                    "call": self.identity(cid),
                    "tool_sha256": self.identity(name)["sha256"], "index": index})
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
            "unmatched_check_reached": code in {"UNMATCHED_EVENT", "TERMINAL_VERIFIED", "EVENTS_RECONCILED"},
            "differences": {k: identities(v) for k, v in differences.items()},
            "terminal_identities": terminal_identities,
            "event_counts": self.event_counts, "event_identities": self.event_identities,
            "event_identities_truncated": sum(self.event_counts.values()) > len(self.event_identities),
            "first_error_event": self.first_error_event,
            "admission_item": self.admission_item,
            "sdk_terminal_structure": self.sdk_terminal_structure,
            "admission_terminal_structure": terminal_structure(self.completed),
            "reconciliation": self.reconciliation,
            "other_event_diagnostics": {"version": "market-other-events-v1",
                "events": self.other_events,
                "truncated": self.event_counts.get("OTHER", 0) > len(self.other_events)},
            # Snapshot all operands, without changing the original OR gate or
            # treating an earlier diagnostic snapshot as a gate evaluation.
            "completion_proof": {"version": "market-completion-proof-diagnostics-v1",
                "gate_rejected": code == "EVENT_COMPLETION_PROOF",
                "failed_conditions": {
                    "stream_not_exhausted": not self.stream_exhausted,
                    "sequence_invalid": not self.sequence_valid,
                    "first_event_not_created": self.first_event_type != "response.created",
                    "created_count_not_one": self.event_counts.get("response.created") != 1,
                    "completed_count_not_one": self.event_counts.get("response.completed") != 1,
                    "other_events_present": bool(self.event_counts.get("OTHER", 0)),
                    "response_identity_mismatch": bool(self.event_response_ids - {self.response_id})}},
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
        raw_kind = kind
        allowed = {"response.created", "response.in_progress", "response.completed", "response.failed",
                   "response.incomplete", "error", "response.output_item.added", "response.output_item.done",
                   "response.function_call_arguments.delta", "response.function_call_arguments.done"}
        kind = kind if type(kind) is str and kind in allowed else "OTHER"
        if self.events == 0:
            self.first_event_type = kind
        if kind == "response.completed" and self.sdk_terminal_structure is None:
            try:
                self.sdk_terminal_structure = terminal_structure(getattr(event, "response", None))
            except Exception:
                self.sdk_terminal_structure = terminal_structure(None)
                self.sdk_terminal_structure.update(classification="UNRECOGNIZED", inspection_failed=True,
                                                   output_present=None, output_container="UNKNOWN")
        item = getattr(event, "item", None)
        index = getattr(event, "output_index", None)
        self.current_event = {"event_type": kind,
            "item": self.identity(getattr(item, "id", None) if item is not None else getattr(event, "item_id", None)),
            "call": self.identity(getattr(item, "call_id", None)),
            "tool_sha256": self.identity(getattr(item, "name", None))["sha256"],
            "index": index if type(index) is int and 0 <= index < 4096 else None}
        self.event_counts[kind] = min(self.event_counts.get(kind, 0) + 1, 8193)
        if kind == "OTHER" and len(self.other_events) < 32:
            try:
                sequence = getattr(event, "sequence_number", None)
                self.other_events.append({"type": event_type_summary(raw_kind),
                    "event_ordinal": min(self.events, 8193),
                    "sequence_number": sequence if type(sequence) is int and 0 <= sequence < 2**63 else None,
                    "sequence_state": "VALID" if type(sequence) is int and 0 <= sequence < 2**63
                                      else "MISSING" if sequence is None else "INVALID",
                    "response_id": self.identity(getattr(event, "response_id", None)),
                    "nested_response_id": self.identity(getattr(getattr(event, "response", None), "id", None)),
                    "item": self.current_event["item"], "index": self.current_event["index"],
                    "call": self.identity(getattr(item, "call_id", None)),
                    "event_call": self.identity(getattr(event, "call_id", None))})
            except Exception:
                # New metadata inspection must never replace an existing
                # protocol result; audit append failures still propagate.
                self.other_events.append({"inspection_failed": True})
        if len(self.event_identities) < 64:
            self.event_identities.append(self.current_event)
        try:
            # Additional proof for the empty-terminal path only. Do not infer
            # missing sequence numbers or reinterpret unregistered events.
            if self.events < 8192:
                sequence = getattr(event, "sequence_number", None)
                if (type(sequence) is not int or sequence < 0
                        or self.last_sequence is not None and sequence <= self.last_sequence):
                    self.sequence_valid = False
                if type(sequence) is int:
                    self.last_sequence = sequence
                response_id = getattr(event, "response_id", None)
                if response_id is not None:
                    if self.identifier(response_id):
                        self.event_response_ids.add(response_id)
                    else:
                        self.sequence_valid = False
                if kind in {"response.output_item.added", "response.output_item.done",
                            "response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                    ident = getattr(item, "id", None) if item is not None else getattr(event, "item_id", None)
                    if self.identifier(ident):
                        self.item_phases.setdefault(ident, []).append(kind)
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

    def verified_output(self, response):
        """Never mutate the SDK response or use LangChain's partial arguments.

        Nonempty snapshots keep their original checks. Explicit [] can use
        fully finalized, ordered tool events; missing/None is never an alias.
        """
        output = getattr(response, "output", None)
        if type(output) is not list:
            reject("TERMINAL_STRUCTURE")
        if output or not (self.added or self.done or self.deltas or self.arguments_done):
            return list(enumerate(output))
        ids = set(self.done)
        if (not ids or ids != set(self.added) or ids != set(self.arguments_done)
                or set(self.deltas) - ids or set(self.item_phases) != ids
                or set(self.slots.values()) != ids):
            reject("UNMATCHED_EVENT")
        if (not self.stream_exhausted or not self.sequence_valid
                or self.first_event_type != "response.created"
                or self.event_counts.get("response.created") != 1
                or self.event_counts.get("response.completed") != 1
                or self.event_counts.get("OTHER", 0)
                or self.event_response_ids - {self.response_id}):
            reject("EVENT_COMPLETION_PROOF")
        indices = sorted(value[0] for value in self.done.values())
        if indices != list(range(len(ids))):
            reject("COMPLETE_INDEX")
        result = []
        for ident, done in sorted(self.done.items(), key=lambda entry: entry[1][0]):
            phases = self.item_phases[ident]
            if (len(phases) < 3 or phases[0] != "response.output_item.added"
                    or phases[-2:] != ["response.function_call_arguments.done", "response.output_item.done"]
                    or any(p != "response.function_call_arguments.delta" for p in phases[1:-2])
                    or done[4] != "completed"):
                reject("EVENT_ORDER_OR_STATUS")
            # Copy only already-final fields. The shared admission loop below
            # still checks all identities, JSON equality and native Schema.
            result.append((done[0], SimpleNamespace(type="function_call", id=ident,
                call_id=done[1], name=done[2], arguments=done[3], status=done[4])))
        self.reconciliation = {"version": "market-stream-reconciliation-v1",
                               "source": "COMPLETED_EVENTS", "tool_count": len(result), "status": "CANDIDATE"}
        return result

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
        for output_index, item in self.verified_output(response):
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
        if self.reconciliation:
            self.reconciliation["status"] = "VERIFIED"
        # Native Market diagnostics use the existing private bounded side-lock
        # sink. Audit failure propagates before any tools can be dispatched.
        for call in calls:
            diagnose(call, "MARKET_SAFE_ADMISSION", "TERMINAL_VERIFIED")
        self.diagnostic("EVENTS_RECONCILED" if self.reconciliation else "TERMINAL_VERIFIED")
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
        self.evidence.stream_exhausted = True
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

