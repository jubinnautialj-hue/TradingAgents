"""Offline ToolNode and parsed-model boundary checks, never a live research."""
import copy
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from tests.test_okx_multitimeframe import CFG, DAY, NOW, SYMBOL, MultiClient
from tradingagents.agents import tools
from tradingagents.agents.state import AgentState
from tradingagents.agents.analysts.market_analyst import TOOLS, create_market_analyst
from tradingagents.agents.analysts.market_contract import (
    admit_market_tool, diagnose, observe_model_result, IndicatorParameters,
)
from tradingagents.agents.analysts.market_display import market_tool, project_history
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import okx, okx_derivatives

NAMES = ["close_10_ema", "close_50_sma", "close_200_sma", "rsi", "macd", "macds", "macdh"]


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(okx_derivatives, "utc_ms", lambda: NOW)
    result = okx.build_bundle(SYMBOL, DAY, CFG, MultiClient())
    assert result["status"] == "VALID"
    return result


def invoke(pinned, args, *, baseline=False):
    selected = [market_tool(tools.get_indicators)] if baseline else list(TOOLS)
    graph = StateGraph(AgentState)
    graph.add_node("tools", ToolNode(selected, **({} if baseline else {"wrap_tool_call": admit_market_tool})))
    graph.add_edge(START, "tools"); graph.add_edge("tools", END)
    calls = [{"name": "get_indicators", "args": a, "id": f"call_{i}"} for i, a in enumerate(args)]
    state = {"company_of_interest": SYMBOL, "trade_date": DAY, "market_snapshot": pinned,
             "messages": [AIMessage(content="", tool_calls=calls)]}
    with run_config(CFG):
        result = graph.compile().invoke(state)
    return [m for m in result["messages"] if m.type == "tool"]


def events(caplog):
    return [json.loads(r.message.split("MARKET_TOOL_DIAGNOSTIC ", 1)[1])
            for r in caplog.records if r.message.startswith("MARKET_TOOL_DIAGNOSTIC ")]


@pytest.mark.parametrize("args,code", [
    ({}, "MISSING_REQUIRED"),
    ({"indicator": "rsi"}, "MISSING_REQUIRED"),
    ({"curr_date": DAY}, "MISSING_REQUIRED"),
    ({"indicator": "", "curr_date": DAY}, "EMPTY_INDICATOR"),
    ({"indicator": "  ", "curr_date": DAY}, "EMPTY_INDICATOR"),
    ({"indicator": "rsi,", "curr_date": DAY}, "EMPTY_INDICATOR"),
    ({"indicator": "rsi_typo", "curr_date": DAY}, "UNKNOWN_INDICATOR"),
    ({"indicator": ["rsi"], "curr_date": DAY}, "ARGUMENT_TYPE"),
    ({"indicator": "rsi", "curr_date": "nonsense"}, "INVALID_DATE"),
    ({"indicator": "rsi", "curr_date": "2026-02-30"}, "INVALID_DATE"),
    *[({"indicator": "rsi", "curr_date": DAY, "look_back_days": x}, "LOOKBACK_TYPE")
      for x in (True, "30", 1.5, None)],
    *[({"indicator": "rsi", "curr_date": DAY, "look_back_days": x}, "LOOKBACK_RANGE")
      for x in (0, -1, 3651)],
    ({"indicator": "rsi", "curr_date": DAY, "lookback_days": 30}, "UNKNOWN_PARAMETER"),
    ({"indicator": "rsi", "curr_date": DAY, "symbol": "ETH-USDT-SWAP"}, "INJECTED_ARGUMENT_CONFLICT"),
    ({"indicator": "rsi", "curr_date": DAY, "market_snapshot": {}}, "INJECTED_ARGUMENT_CONFLICT"),
])
def test_invalid_recoverable_not_success(pinned, args, code):
    before = copy.deepcopy(pinned)
    message = invoke(pinned, [args])[0]
    assert message.status == "error" and code in message.content
    assert message.tool_call_id == "call_0" and message.name == "get_indicators"
    assert "Bounded deterministic table" not in message.content
    assert pinned == before  # No failed tool output becomes snapshot evidence.


def test_historical_signature_in_baseline_only(pinned):
    old = invoke(pinned, [{}] * 14, baseline=True)
    assert len(old) == 14
    assert all(len(m.content.encode()) == 153 and hashlib.sha256(m.content.encode()).hexdigest()
               == "1253ace65ba08833ece9931efd8893b95be5b94f656dbe99f5c8049e36e46318" for m in old)
    assert all(m.status == "error" for m in invoke(pinned, [{}] * 14))


@pytest.mark.parametrize("name", NAMES)
def test_valid_single_bytes_unchanged(pinned, name):
    args = {"indicator": name, "curr_date": DAY}
    assert invoke(pinned, [args])[0].content == invoke(pinned, [args], baseline=True)[0].content


def test_parallel_comma_and_individual_bytes(pinned):
    args = [{"indicator": n, "curr_date": DAY} for n in NAMES]
    fixed = invoke(pinned, args)
    old = invoke(pinned, args, baseline=True)
    assert [m.content for m in fixed] == [m.content for m in old]
    assert [m.content for m in fixed] == [invoke(pinned, [a])[0].content for a in args]
    comma = {"indicator": ",".join(NAMES), "curr_date": DAY}
    assert invoke(pinned, [comma])[0].content == invoke(pinned, [comma], baseline=True)[0].content
    assert all(m.status == "success" for m in fixed)


@pytest.mark.parametrize("value", [1, 30, 3650])
def test_valid_bounds_and_original_arguments(value):
    args = {"indicator": " RSI , MACD ", "curr_date": DAY, "look_back_days": value}
    assert IndicatorParameters.model_validate(args).model_dump() == args


def test_parsed_and_entry_diagnostics_through_actual_market_node(pinned, caplog):
    caplog.set_level(logging.INFO, logger="tradingagents.agents.analysts.market_contract")
    call = {"name": "get_indicators", "args": {}, "id": "call_trace"}
    class Model:
        def bind_tools(self, tools):
            return RunnableLambda(lambda _: AIMessage(content="", tool_calls=[call]))
    state = {"company_of_interest": SYMBOL, "trade_date": DAY, "market_snapshot": pinned,
             "messages": []}
    with run_config(CFG):
        result = create_market_analyst(Model())(state)
    request = SimpleNamespace(tool_call=result["messages"][0].tool_calls[0], tool=TOOLS[1])
    message = admit_market_tool(request, lambda _: pytest.fail("Invalid tool executed"))
    rows = events(caplog)
    parsed = next(r for r in rows if r["stage"] == "MODEL_PARSED")
    entry = next(r for r in rows if r["stage"] == "TOOLNODE_ENTRY")
    assert parsed["call_id"] == entry["call_id"] == "call_trace"
    assert parsed["structure_sha256"] == entry["structure_sha256"]
    assert message.status == "error" and rows[-1]["code"] == "MISSING_REQUIRED"


def test_boundary_loss_and_sensitive_values_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="tradingagents.agents.analysts.market_contract")
    call = {"name": "get_indicators", "id": "call_loss", "args": {"indicator": "rsi", "curr_date": DAY}}
    observe_model_result(AIMessage(content="PRIVATE_RESPONSE", tool_calls=[call]))
    request = SimpleNamespace(tool_call={**call, "args": {}}, tool=TOOLS[1])
    admit_market_tool(request, lambda _: pytest.fail("Invalid tool executed"))
    rows = events(caplog)
    assert rows[0]["structure_sha256"] != rows[1]["structure_sha256"]
    assert rows[0]["call_id"] == rows[1]["call_id"]
    secret = {**call, "args": {"indicator": "PRIVATE_VALUE", "curr_date": "PRIVATE_DATE", "PRIVATE_KEY": "TOKEN_SECRET"}}
    message = admit_market_tool(SimpleNamespace(tool_call=secret, tool=TOOLS[1]), lambda _: None)
    text = " ".join(r.message for r in caplog.records) + message.content
    assert not any(s in text for s in ["PRIVATE_VALUE", "PRIVATE_DATE", "PRIVATE_KEY", "TOKEN_SECRET", "PRIVATE_RESPONSE"])
    assert "UNKNOWN_PARAMETER" in message.content


def test_mixed_candidates_no_error_fold_or_parameter_mutation(pinned):
    args = [{}, {"indicator": "rsi", "curr_date": DAY}]
    before = copy.deepcopy(args)
    msgs = invoke(pinned, args)
    assert [m.status for m in msgs] == ["error", "success"]
    assert args == before
    ai = AIMessage(content="", tool_calls=[{"name": "get_indicators", "args": {}, "id": "call_0"}])
    assert project_history([ai, msgs[0]])[-1] == msgs[0]


def test_actual_market_subgraph_recovers_without_retrying_for_user(pinned, caplog):
    from tradingagents.graph.setup import _analyst_graph
    from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
    caplog.set_level(logging.INFO, logger="tradingagents.agents.analysts.market_contract")
    seen = []
    replies = iter([
        AIMessage(content="", tool_calls=[{"name": "get_indicators", "args": {}, "id": "call_bad"}]),
        AIMessage(content="", tool_calls=[{"name": "get_indicators", "args": {"indicator": "rsi", "curr_date": DAY}, "id": "call_good"}]),
        AIMessage(content="FIXTURE WAIT; parameter error retained, no trade plan"),
    ])
    class Model:
        def bind_tools(self, tools):
            def reply(prompt):
                seen.extend(m.status for m in prompt.messages if m.type == "tool")
                return next(replies)
            return RunnableLambda(reply)
    state = {"company_of_interest": SYMBOL, "trade_date": DAY, "market_snapshot": pinned, "messages": []}
    graph = _analyst_graph(ANALYST_NODE_SPECS["market"], create_market_analyst(Model()), 10)
    with run_config(CFG):
        result = graph.invoke(state)
    assert "FIXTURE WAIT" in result["market_report"]
    assert "error" in seen and "success" in seen
    rows = events(caplog)
    assert sum(r["stage"] == "TOOLNODE_ENTRY" and r["call_id"] == "call_bad" for r in rows) == 1


def test_runtime_errors_are_not_silently_reclassified():
    call = {"name": "get_indicators", "args": {"indicator": "rsi", "curr_date": DAY}, "id": "call_runtime"}
    def fail(_):
        raise RuntimeError("FIXTURE_CORE_FAILURE")
    with pytest.raises(RuntimeError, match="FIXTURE_CORE_FAILURE"):
        admit_market_tool(SimpleNamespace(tool_call=call, tool=TOOLS[1]), fail)


def test_diagnostic_shape_has_no_value_or_unknown_key_dependency(caplog):
    caplog.set_level(logging.INFO, logger="tradingagents.agents.analysts.market_contract")
    a = diagnose({"id": "call_shape", "name": "get_indicators", "args": {"indicator": "TOKEN_ONE"}}, "MODEL_PARSED")
    b = diagnose({"id": "call_shape", "name": "get_indicators", "args": {"indicator": "TOKEN_TWO"}}, "TOOLNODE_ENTRY")
    assert a["structure_sha256"] == b["structure_sha256"]
    assert "TOKEN_ONE" not in caplog.text and "TOKEN_TWO" not in caplog.text


def test_unparsed_arguments_and_malformed_id_redacted(caplog):
    caplog.set_level(logging.INFO, logger="tradingagents.agents.analysts.market_contract")
    message = AIMessage(content="PRIVATE_RESPONSE", invalid_tool_calls=[{
        "name": "get_indicators", "id": "bad\nPRIVATE_ID", "args": "{PRIVATE_RAW}", "error": "PRIVATE_ERROR"}])
    observe_model_result(message)
    row = events(caplog)[0]
    assert row["code"] == "UNPARSED_ARGUMENTS" and row["call_id"].startswith("sha256:")
    assert "PRIVATE" not in caplog.text


def test_existing_metadata_sink_records_both_boundaries(tmp_path, monkeypatch):
    path = tmp_path / "metadata.jsonl"
    monkeypatch.setenv("TA_SINGLE_AUDIT_FILE", str(path))
    call = {"name": "get_indicators", "id": "call_file", "args": {}}
    observe_model_result(AIMessage(content="SECRET_RESPONSE", tool_calls=[call]))
    admit_market_tool(SimpleNamespace(tool_call=call, tool=TOOLS[1]), lambda _: None)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["stage"] for r in rows] == ["MODEL_PARSED", "TOOLNODE_ENTRY", "ARGUMENT_VALIDATION"]
    assert len({r["call_id"] for r in rows}) == 1
    assert "SECRET_RESPONSE" not in path.read_text()


def test_metadata_sink_capacity_stops_without_unbounded_output(tmp_path, monkeypatch):
    from tradingagents.agents.analysts import market_contract as contract
    path = tmp_path / "metadata.jsonl"
    path.write_text("retained")
    monkeypatch.setenv("TA_SINGLE_AUDIT_FILE", str(path))
    monkeypatch.setattr(contract, "AUDIT_LIMIT", 1)
    with pytest.raises(RuntimeError, match="MARKET_DIAGNOSTIC_CAPACITY"):
        diagnose({"name": "get_indicators", "id": "call_full", "args": {}}, "MODEL_PARSED")
    assert path.read_text() == "retained"
