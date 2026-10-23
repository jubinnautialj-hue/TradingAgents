"""Real client entry points with scripted transport failures; never network/auth."""

import copy
import ssl

import httpx2
import openai
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from typer.testing import CliRunner

from tests.test_okx_graph import graph_setup as make_graph_setup
from tests.test_okx_research import DAY, SYMBOL
from tradingagents.agents.structured import invoke_structured
from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI
from tradingagents.llm_clients.reliability import LLMUnavailable, classify_failure


@pytest.fixture
def graph_setup(monkeypatch, tmp_path):
    return make_graph_setup.__wrapped__(monkeypatch, tmp_path)


def model(**kwargs):
    return ChatGPTPlanChatOpenAI(model="gpt-6.1-sol", api_key=lambda: "test-placeholder",
                                use_responses_api=True, **kwargs)


def chunk(text="", status=None):
    return ChatGenerationChunk(message=AIMessageChunk(content=text, response_metadata={"status": status} if status else {}))


def test_timeout_is_finite_and_sdk_replay_disabled():
    llm = model()
    assert llm.streaming and llm.store is False
    assert llm.request_timeout == 120 and llm.root_client.timeout == 120
    assert llm.root_client.max_retries == 0
    assert model(timeout=45).root_client.timeout == 45


@pytest.mark.parametrize("error", [httpx2.ConnectTimeout, httpx2.ConnectError, httpx2.PoolTimeout])
def test_only_preconnect_failures_retry(monkeypatch, error):
    attempts = []
    def stream(*a, **k):
        attempts.append(1)
        if len(attempts) < 3:
            raise error("sensitive body is never logged")
        yield chunk("OK", "completed")
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    monkeypatch.setattr("tradingagents.llm_clients.openai_client.time.sleep", lambda _: None)
    assert model().invoke([HumanMessage("test")]).content == "OK"
    assert len(attempts) == 3


def test_preconnect_retry_budget_exhausted(monkeypatch):
    attempts = []
    def stream(*a, **k):
        attempts.append(1)
        raise httpx2.ConnectTimeout("private URL")
        yield
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    monkeypatch.setattr("tradingagents.llm_clients.openai_client.time.sleep", lambda _: None)
    with pytest.raises(LLMUnavailable, match="CONNECT_FAILURE"):
        model().invoke("test")
    assert len(attempts) == 3


def test_explicit_zero_retries_is_respected(monkeypatch):
    attempts = []
    def stream(*a, **k):
        attempts.append(1)
        raise httpx2.ConnectTimeout("unreachable")
        yield
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    with pytest.raises(LLMUnavailable):
        model(max_retries=0).invoke("test")
    assert len(attempts) == 1


@pytest.mark.parametrize("error,kind", [(httpx2.ReadTimeout, "STREAM_TIMEOUT"),
                                       (httpx2.RemoteProtocolError, "STREAM_INTERRUPTED"),
                                       (httpx2.ReadError, "STREAM_INTERRUPTED")])
def test_partial_stream_never_replayed_or_returned(monkeypatch, error, kind):
    attempts = []
    def stream(*a, **k):
        attempts.append(1)
        yield chunk("partial BUY/tool arguments")
        raise error("private response")
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    with pytest.raises(LLMUnavailable) as raised:
        model().bind_tools([]).invoke("test")
    assert raised.value.kind == kind and raised.value.partial
    assert "private response" not in str(raised.value)
    assert len(attempts) == 1


@pytest.mark.parametrize("events", [[], [chunk("partial")], [chunk("partial", "incomplete")]])
def test_clean_eof_requires_completed_event(monkeypatch, events):
    monkeypatch.setattr(ChatOpenAI, "_stream", lambda *a, **k: iter(events))
    with pytest.raises(LLMUnavailable, match="STREAM_INCOMPLETE"):
        model().invoke("test")


def test_request_timeout_without_response_is_ambiguous_no_replay(monkeypatch):
    attempts = []
    def stream(*a, **k):
        attempts.append(1)
        raise httpx2.ReadTimeout("unknown acceptance")
        yield
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    with pytest.raises(LLMUnavailable, match="REQUEST_TIMEOUT"):
        model().invoke("test")
    assert len(attempts) == 1


def test_authentication_not_retried(monkeypatch):
    attempts = []
    def stream(*a, **k):
        attempts.append(1)
        raise openai.AuthenticationError("secret text", response=httpx2.Response(401, request=httpx2.Request("POST", "https://example.invalid")), body=None)
        yield
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    with pytest.raises(LLMUnavailable, match="AUTHENTICATION_OR_PERMISSION"):
        model().invoke("test")
    assert len(attempts) == 1


def test_wrapped_protocol_error_and_tls_classification():
    try:
        try:
            raise httpx2.RemoteProtocolError("sensitive")
        except Exception as cause:
            raise openai.APIConnectionError(request=httpx2.Request("POST", "https://example.invalid")) from cause
    except Exception as exc:
        result = classify_failure(exc, partial=True)
    assert result.kind == "STREAM_INTERRUPTED"
    assert result.exception_types == ["APIConnectionError", "RemoteProtocolError"]
    assert classify_failure(ssl.SSLCertVerificationError()).kind == "TLS_ERROR"
    assert classify_failure(ValueError("programming bug")) is None


def test_structured_network_error_not_retried_as_text():
    class Structured:
        def invoke(self, _):
            raise LLMUnavailable("STREAM_INTERRUPTED", partial=True)
    with pytest.raises(LLMUnavailable):
        invoke_structured(Structured(), "test", "manager")


def test_graph_failure_preserves_data_status_and_blocks_risk(graph_setup, monkeypatch):
    from tradingagents.graph import trading_graph
    _, _, config = graph_setup
    def analyst(_):
        def fail(state):
            raise LLMUnavailable("STREAM_INTERRUPTED", partial=True)
        return fail
    monkeypatch.setattr("tradingagents.graph.setup.create_market_analyst", analyst)
    graph = trading_graph.TradingAgentsGraph(["market"], config=config)
    state, signal = graph.propagate(SYMBOL, DAY, "crypto")
    assert state["data_quality"] == "VALID"
    assert state["llm_status"] == "LLM_UNAVAILABLE" and state["workflow_status"] == "FAILED"
    assert state["llm_failure"]["stage"] == "Market Analyst"
    assert not state["allow_new_risk"] and not state["execution_enabled"] and signal == "REVIEW"
    report = graph.save_reports(state, SYMBOL)
    assert "LLM_UNAVAILABLE" in report.read_text(encoding="utf-8")
    assert not state["allow_new_risk"]  # Report revalidation cannot undo model failure.


def test_cli_model_failure_exit_code_and_report(graph_setup, monkeypatch):
    from cli import main
    _, _, config = graph_setup
    monkeypatch.setattr(main, "DEFAULT_CONFIG", copy.deepcopy(config))
    def analyst(_):
        return lambda state: (_ for _ in ()).throw(LLMUnavailable("REQUEST_TIMEOUT"))
    monkeypatch.setattr("tradingagents.graph.setup.create_market_analyst", analyst)
    result = CliRunner().invoke(main.app, ["okx-research", "--date", DAY])
    assert result.exit_code == 3, result.output
    assert "Data quality: VALID" in result.output and "Model: LLM_UNAVAILABLE" in result.output
    assert "Report:" in result.output and "allow_new_risk=False" in result.output
