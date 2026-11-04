"""Market-only Responses terminal evidence admission; no transport or retries."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from types import SimpleNamespace

from tradingagents.llm_clients.reliability import LLMUnavailable
from tradingagents.agents.analysts.market_contract import argument_error, diagnose

ACTIVE = ContextVar("market_safe_response", default=None)


def reject(code):
    # Static codes only: no provider data or arbitrary exception text escapes.
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

    def fail(self, code):
        self.error = self.error or code

    def observe(self, event):
        # Do not interrupt the SDK before its usage tap can account for an
        # accepted response. Admission occurs only AFTER invoke/reconciliation.
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
        for event in self.response:
            self.evidence.observe(event)
            yield event


class ObservedResponses:
    def __init__(self, responses, evidence):
        self.responses, self.evidence = responses, evidence

    def create(self, **kwargs):
        if kwargs.get("stream") is not True:
            reject("STREAM_REQUIRED")
        return ObservedResponse(self.responses.create(**kwargs), self.evidence)


class ObservedRoot:
    def __init__(self, root, evidence):
        self.responses = ObservedResponses(root.responses, evidence)

