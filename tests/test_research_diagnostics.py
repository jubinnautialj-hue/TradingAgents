"""TEST_FIXTURE only: no network, credentials, historical ledger or model."""
import json
from types import SimpleNamespace as NS

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from tests.test_guarded_research import audited, FixtureModel
from tests.test_llm_stream_reliability import model, chunk
from tests.test_okx_research import INFO, NOW
from tests.test_okx_derivatives import DerivativeClient
from tradingagents.agents.analysts.market_contract import observe_model_result
from tradingagents.dataflows.vendors import okx_derivatives as oi
from tradingagents.llm_clients import diagnostics as d
from tradingagents.llm_clients.reliability import LLMUnavailable, classify_failure
from tradingagents.safety_budget import SafetyStop


@pytest.fixture
def sink(tmp_path, monkeypatch):
    path = tmp_path / "TEST_FIXTURE.jsonl"
    monkeypatch.setenv("TA_SINGLE_AUDIT_FILE", str(path))
    return path


def rows(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


@pytest.mark.parametrize("mode", ["complete", "partial", "missing_usage"])
def test_real_guard_http_sdk_usage_separate(audited, sink, mode):
    ledger, _, session, meter = audited
    with session.guards(meter):
        if mode == "complete":
            assert FixtureModel().invoke("SECRET_PROMPT").content == "PRIVATE_RESPONSE"
        else:
            with pytest.raises((SafetyStop, LLMUnavailable)):
                FixtureModel(partial=mode == "partial", total=None if mode == "missing_usage" else 100).invoke("SECRET_PROMPT")
            with pytest.raises(SafetyStop):
                FixtureModel().invoke("MUST_NOT_DISPATCH")
    rr = rows(sink)
    assert rr[0]["stage"] == "HTTP_RESPONSE" and rr[0]["transport_http_status"] == 200
    assert len({x["call_id"] for x in rr}) == 1
    assert ledger.status()["http"] == ledger.status()["llm_calls"] == 1
    if mode == "complete":
        assert [x["stage"] for x in rr] == ["HTTP_RESPONSE", "SDK_RESULT", "USAGE_CONFIRMED"]
        assert rr[-1]["usage_confirmed"] and not ledger.status()["paused"]
    else:
        assert ledger.status()["paused"] and ledger.status()["total_usage"] == "UNKNOWN"
    text = sink.read_text()
    for secret in ("SECRET_PROMPT", "PRIVATE_RESPONSE", "PRIVATE_TOKEN", "PRIVATE_REQUEST_ID", "Authorization"):
        assert secret not in text
    assert d.CONTEXT.get() is None


@pytest.mark.parametrize("mode", ["apierror", "eof", "parse", "complete"])
def test_stream_events_without_replay(monkeypatch, sink, mode):
    attempts = []
    def stream(*a, **k):
        attempts.append(1)
        yield chunk("SECRET_BODY")
        if mode == "apierror":
            raise openai.APIError("SECRET_ERROR", request=httpx.Request("POST", "https://example.invalid"),
                                  body={"code": "server_error", "message": "SECRET_BODY"})
        if mode == "parse":
            raise ValueError("SECRET_PARSE_BODY")
        if mode == "complete":
            yield chunk("", "completed")
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    token = d.CONTEXT.set({"call_id": "call_fixture", "wire_id": "wire_fixture", "transport_http_status": 200})
    try:
        if mode == "complete":
            assert len(list(model()._stream([]))) == 2
        else:
            with pytest.raises(ValueError if mode == "parse" else LLMUnavailable):
                list(model()._stream([]))
    finally:
        d.CONTEXT.reset(token)
    rr = rows(sink)
    assert len(attempts) == 1 and rr[0]["stage"] == "STREAM_FIRST_CHUNK"
    assert rr[-1]["stage"] == ("STREAM_END" if mode == "complete" else "STREAM_ERROR")
    assert rr[-1]["stream_completed"] == (mode == "complete")
    assert rr[-1]["chunk_received"]
    assert "SECRET_" not in sink.read_text()


def raw_event(arguments):
    return NS(type="response.output_item.done", item=NS(type="function_call", name="get_indicators",
                                                       call_id="call_tool_fixture", arguments=arguments))


@pytest.mark.parametrize("raw,parsed,state,lost", [
    ("{}", {}, "EMPTY_OBJECT", False),
    ('{"indicator":"SECRET_VALUE","curr_date":"SECRET_DATE"}', {}, "OBJECT", True),
    ('{"indicator":"rsi","curr_date":"2026-11-03"}', {"indicator": "rsi", "curr_date": "2026-11-03"}, "OBJECT", False),
])
def test_raw_vs_parsed_boundary(sink, raw, parsed, state, lost):
    d.observe_response_event(raw_event(raw))
    observe_model_result(AIMessage(content="SECRET_RESPONSE", tool_calls=[
        {"name": "get_indicators", "id": "call_tool_fixture", "args": parsed}]))
    rr = rows(sink)
    assert rr[0]["stage"] == "RAW_TOOL_ARGUMENTS" and rr[1]["stage"] == "MODEL_PARSED"
    assert rr[0]["tool_call_id"] == rr[1]["call_id"]
    assert rr[0]["arguments"]["state"] == state
    assert (rr[0]["arguments"]["structure_sha256"] != rr[1]["structure_sha256"]) == lost
    assert "SECRET_" not in sink.read_text()


@pytest.mark.parametrize("value,state", [(None, "MISSING"), ("", "EMPTY_STRING"),
    ("{SECRET_INVALID", "INVALID_JSON"), ("x" * 65537, "LIMIT_UNKNOWN"), ([], "NON_OBJECT")],
    ids=["missing", "empty", "invalid", "oversized", "non_object"])
def test_ambiguous_raw_arguments_never_claim_empty(value, state):
    assert d.arguments(value) == {"state": state}


def test_converter_hook_preserves_result_and_records_parse_failure(audited, sink, monkeypatch):
    import langchain_openai.chat_models.base as sdk
    sentinel = object()
    monkeypatch.setattr(sdk, "_convert_responses_chunk_to_generation_chunk", lambda *a, **k: sentinel)
    _, _, session, meter = audited
    with session.guards(meter):
        assert sdk._convert_responses_chunk_to_generation_chunk(raw_event("{}")) is sentinel
    assert rows(sink)[0]["arguments"]["state"] == "EMPTY_OBJECT"
    def broken(*a, **k):
        raise ValueError("SECRET_PARSER_TEXT")
    monkeypatch.setattr(sdk, "_convert_responses_chunk_to_generation_chunk", broken)
    with session.guards(meter):
        with pytest.raises(ValueError):
            sdk._convert_responses_chunk_to_generation_chunk(raw_event("{}"))
    assert rows(sink)[-1]["stage"] == "SDK_PARSE_ERROR"
    assert "SECRET_" not in sink.read_text()


def test_transport_context_does_not_reclassify_error():
    exc = openai.APIError("PRIVATE", request=httpx.Request("POST", "https://example.invalid"), body={})
    old = classify_failure(exc)
    token = d.CONTEXT.set({"call_id": "call_a", "transport_http_status": 200})
    try:
        new = classify_failure(exc, partial=True)
    finally:
        d.CONTEXT.reset(token)
    assert old.kind == new.kind == "MODEL_SERVICE_ERROR"
    assert new.partial and new.diagnostics["transport_http_status"] == 200
    assert "http_status" not in new.diagnostics


def test_transport_error_has_wire_identity_without_success_status(audited, sink):
    class TransportFailure(FixtureModel):
        def _generate(self, *args, **kwargs):
            def failure(request):
                raise httpx.ConnectError("SECRET_NETWORK", request=request)
            with httpx.Client(transport=httpx.MockTransport(failure)) as client:
                client.post("https://api.openai.com/v1/responses")
    ledger, _, session, meter = audited
    with session.guards(meter):
        with pytest.raises(httpx.ConnectError):
            TransportFailure().invoke("SECRET_PROMPT")
    rr = rows(sink)
    assert rr[0]["stage"] == "HTTP_ERROR" and rr[0]["wire_id"]
    assert rr[0]["transport_http_status"] is None
    assert ledger.status()["paused"] and ledger.status()["http"] == 1
    assert "SECRET_" not in sink.read_text()


def test_oi_unit_summary_preserves_rejection_and_success_bytes(sink):
    good = {"oi": "100", "oiCcy": "1", "oiUsd": "100", "ts": str(NOW - 1000)}
    assert oi.parse_oi(good, INFO, NOW) == oi.parse_oi(good, INFO, NOW, diagnostic={})
    diag = {}
    with pytest.raises(ValueError, match="OI contract/base units mismatch"):
        oi.parse_oi({**good, "oiCcy": "123.456789"}, INFO, NOW, diagnostic=diag)
    summary = diag["unit_check"]
    d.emit("OI_UNIT_CHECK", unit_summary=summary)
    assert summary["stage"] == "CONTRACT_BASE_COMPARISON" and summary["base_unit"] == "BTC"
    assert summary["present"] == {k: True for k in good}
    assert "123.456789" not in sink.read_text()


def test_oi_capture_emits_but_stays_unavailable(sink, monkeypatch):
    class Client(DerivativeClient):
        def get(self, path, params=None):
            result = super().get(path, params)
            if path == oi.OI_HISTORY:
                result[0][2] = "999999.87654321"
            return result
    monkeypatch.setattr(oi, "utc_ms", lambda: NOW)
    result = oi.collect_derivatives(Client(), INFO, NOW, "4h", False, {})
    part = result["components"]["oi_history"]
    assert part["status"] == "DATA_UNAVAILABLE"
    assert part["diagnostic"]["error_code"] == "CONTRACT_UNITS"
    assert rows(sink)[-1]["stage"] == "OI_UNIT_CHECK"
    assert "999999.87654321" not in sink.read_text()


def test_audit_failure_propagates(monkeypatch, sink):
    def fail(*a, **k):
        raise RuntimeError("MARKET_DIAGNOSTIC_CAPACITY")
    monkeypatch.setattr(d, "append_audit", fail)
    with pytest.raises(RuntimeError, match="CAPACITY"):
        d.emit("STREAM_END", completed=True)


def test_sdk_parse_error_still_pauses_and_never_replays(audited, sink):
    class BrokenParser(FixtureModel):
        def _generate(self, *args, **kwargs):
            super()._generate(*args, **kwargs)
            raise ValueError("SECRET_SDK_PARSE")
    ledger, _, session, meter = audited
    with session.guards(meter):
        with pytest.raises(ValueError, match="SECRET_SDK_PARSE"):
            BrokenParser().invoke("SECRET_PROMPT")
        with pytest.raises(SafetyStop):
            FixtureModel().invoke("NO_SECOND_SEND")
    assert ledger.status()["paused"] and ledger.status()["llm_calls"] == ledger.status()["http"] == 1
    assert "SECRET_" not in sink.read_text()


def test_audit_failure_inside_guard_still_marks_usage_unknown(audited, sink, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("MARKET_DIAGNOSTIC_CAPACITY")
    monkeypatch.setattr(d, "append_audit", fail)
    ledger, _, session, meter = audited
    with session.guards(meter):
        with pytest.raises(RuntimeError, match="CAPACITY"):
            FixtureModel().invoke("SECRET_PROMPT")
    state = ledger.status()
    assert state["paused"] and state["total_usage"] == "UNKNOWN"
    assert state["http"] == state["llm_calls"] == 1
    assert d.CONTEXT.get() is None


def test_real_converter_observation_is_nonmutating(audited, sink):
    import langchain_openai.chat_models.base as sdk
    from copy import deepcopy
    event = raw_event('{"indicator":"rsi","curr_date":"2026-11-03"}')
    original = deepcopy(event)
    # An ordinary done function-call event is ignored by this native converter
    # (argument deltas have already supplied its content); observation adds none.
    before = sdk._convert_responses_chunk_to_generation_chunk(event, 0, 0, 0)
    _, _, session, meter = audited
    with session.guards(meter):
        after = sdk._convert_responses_chunk_to_generation_chunk(event, 0, 0, 0)
    assert after == before and event == original
    assert rows(sink)[0]["arguments"]["state"] == "OBJECT"


def test_provider_error_allowlist_and_request_hash(sink):
    response = httpx.Response(429, headers={"retry-after": "12", "authorization": "SECRET_CREDENTIAL"},
                              request=httpx.Request("POST", "https://example.invalid"))
    error = openai.RateLimitError("SECRET_RESPONSE", response=response,
        body={"code": "rate_limit_exceeded", "type": "SECRET_UNKNOWN_TYPE", "message": "SECRET_BODY"})
    d.emit("STREAM_ERROR", failure=error)
    value = rows(sink)[0]["error"]
    assert value["http_status"] == 429 and value["retry_after_seconds"] == 12
    assert value["provider_code"] == "rate_limit_exceeded" and "provider_type" not in value
    assert "SECRET_" not in sink.read_text()


def test_new_events_share_market_observer_lock(sink):
    import importlib.util
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor
    from tradingagents.agents.analysts.market_contract import diagnose
    spec = importlib.util.spec_from_file_location("fixture_observer", Path(__file__).resolve().parents[1] / "scripts/role_observer.py")
    observer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observer)
    assert observer.append_audit is d.append_audit
    def write(i):
        if i % 3 == 0: d.emit("SDK_RESULT")
        elif i % 3 == 1: diagnose({"id": "fixture_" + str(i), "name": "get_indicators", "args": {}}, "MODEL_PARSED")
        else: observer.write({"kind": "MODEL_START", "callback_id": "fixture_" + str(i)})
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(90)))
    assert len(rows(sink)) == 90


def test_real_sink_capacity_and_side_lock(sink):
    from tradingagents.dataflows.audit_file import append_audit
    append_audit(sink, b"{}\n", 4 * 1024**2)
    assert sink.with_name(sink.name + ".lock").exists()
    # Fill a private TEST_FIXTURE file; never truncate/rewrite a historical audit.
    with sink.open("ab") as handle:
        handle.write(b" " * (4 * 1024**2 - sink.stat().st_size))
    with pytest.raises(RuntimeError, match="CAPACITY"):
        d.emit("SDK_RESULT")
    assert sink.stat().st_size == 4 * 1024**2
