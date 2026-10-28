"""Isolated presentation tests; all providers remain socket-blocked by conftest."""
import copy
import csv
import io
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.test_okx_multitimeframe import CFG, DAY, MultiClient, NOW, SYMBOL
from tradingagents.agents import tools
from tradingagents.agents.analysts.market_analyst import TOOLS
from tradingagents.agents.analysts.market_display import (
    TAIL_ROWS, _hash, _moving_averages, bound_csv_tables, market_tool, project_history,
)
from tradingagents.agents.analysts.turn import _as_text
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import okx, okx_derivatives

IDENTITY_HEADER = ("- source: OKX\n- exchange: OKX\n- symbol: BTC-USDT-SWAP\n- interval: 1h\n"
                   "- snapshot_id: " + "a" * 64 + "\n- fetched_at_ms: 1\n- cutoff_ms: 1\n- latest_close_ms: 1\n")

@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(okx_derivatives, "utc_ms", lambda: NOW)
    return okx.build_bundle(SYMBOL, DAY, {**CFG, "okx_derivatives_enabled": True}, MultiClient())


def table(count=100):
    return "Date,Close,Volume\n" + "".join(f"2026-01-{i + 1:03d}, {i},1\n" for i in range(count))


@pytest.mark.parametrize("count", [0, 1, 29, 30])
def test_short_tables_and_prose_identical(count):
    raw = "STALE_DATA; UNAVAILABLE CPI; no risk\n" + table(count)
    assert bound_csv_tables(raw) == raw


def test_summary_preserves_range_extremes_missing_and_reference():
    raw = table().replace(", 3,1", ", -999,1").replace(", 4,1", ", NaN,1")
    result = bound_csv_tables("risk before\n" + raw + "risk after\n")
    assert "risk before" in result and "risk after" in result
    summary = json.loads(result.split("[Bounded deterministic table]\n")[1].splitlines()[0])
    assert summary["full_rows"] == 100
    assert summary["full_table_sha256"] == _hash(raw)
    assert summary["all_range_fields"]["Close"]["min"] == " -999"
    assert summary["all_range_fields"]["Close"]["missing_or_nonfinite"] == [{"at": "2026-01-005", "value": " NaN"}]
    assert summary["all_range_fields"]["Volume"]["sum_over_valid_rows"] == "100"
    tail = result[result.index("Date,"):].split("risk after")[0]
    assert len(list(csv.DictReader(io.StringIO(tail)))) == TAIL_ROWS


@pytest.mark.parametrize("text", ["", "UNKNOWN", "Date,Date\n2026,1\n", "not CSV\n", "Date,Close\ninvalid\n"])
def test_unrecognised_never_truncated(text):
    assert bound_csv_tables(text) == text


@pytest.mark.parametrize("original,wrapped", zip(
    [tools.get_stock_data, tools.get_indicators, tools.get_verified_market_snapshot], TOOLS))
def test_native_schema_and_injected_state_unchanged(original, wrapped):
    assert original.name == wrapped.name
    assert original.args_schema is wrapped.args_schema
    assert original.tool_call_schema.model_json_schema() == wrapped.tool_call_schema.model_json_schema()


def test_full_input_calculations_and_archive_unchanged(pinned):
    before = copy.deepcopy(pinned)
    calculations = {k: okx.indicator_frame(v)[list(okx.INDICATORS)].copy() for k, v in pinned["timeframes"].items()}
    with run_config(CFG):
        result = TOOLS[0].func(SYMBOL, "2020-01-01", DAY, DAY, pinned)
    assert len(result) < len(okx.render_prices(pinned, "2020-01-01", DAY))
    assert pinned == before
    assert okx.fingerprint(pinned) == before["snapshot_id"]
    for period, child in pinned["timeframes"].items():
        assert calculations[period].equals(okx.indicator_frame(child)[list(okx.INDICATORS)])
        assert child["snapshot_id"] in result
    assert "coverage is partial" in result


@pytest.mark.parametrize("window", [20, 60, 120])
def test_ma_full_input_not_last30(pinned, window):
    supplemental = json.loads(_moving_averages(pinned).splitlines()[-1])
    for period, child in pinned["timeframes"].items():
        assert supplemental[period]["supplemental_sma_not_strategy_signal"][f"MA{window}"] == pytest.approx(
            sum(x["Close"] for x in child["records"][-window:]) / window)


@pytest.mark.parametrize("name", okx.INDICATORS)
def test_indicator_latest_extrema_and_quality_kept(pinned, name):
    with run_config(CFG):
        old = tools.get_indicators.func(SYMBOL, name, DAY, 30, DAY, pinned)
        new = TOOLS[1].func(SYMBOL, name, DAY, 30, DAY, pinned)
    assert new == bound_csv_tables(old)
    for period, child in pinned["timeframes"].items():
        value = str(okx.indicator_frame(child)[name].iloc[-1])
        assert value in new
    assert "periods count" in new


def test_verified_structure_risk_and_derivatives_byte_identical(pinned):
    with run_config(CFG):
        old = tools.get_verified_market_snapshot.func(SYMBOL, DAY, 30, DAY, pinned)
        new = TOOLS[2].func(SYMBOL, DAY, 30, DAY, pinned)
    assert old == new
    assert "rsi" in new and "macd" in new
    assert json.dumps(pinned["timeframe_analysis"], ensure_ascii=False) in new
    from tradingagents.dataflows.vendors.okx_derivatives import render_derivatives
    assert render_derivatives(pinned["derivatives"], detailed=True) in new


def test_stale_bundle_cannot_be_restored_by_display(pinned, monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW + 901000)
    with run_config(CFG):
        old = tools.get_stock_data.func(SYMBOL, DAY, DAY, DAY, pinned)
        new = TOOLS[0].func(SYMBOL, DAY, DAY, DAY, pinned)
    assert new == old and "STALE_DATA" in new
    assert "Supplemental" not in new


def test_invalid_period_date_and_tampering_still_rejected(pinned):
    for field, value in [("snapshot_id", "tampered"), ("cutoff_ms", 0)]:
        bad = {**pinned, field: value}
        with run_config(CFG):
            assert TOOLS[0].func(SYMBOL, DAY, DAY, DAY, bad) == tools.get_stock_data.func(SYMBOL, DAY, DAY, DAY, bad)
    with run_config(CFG):
        assert TOOLS[0].func(SYMBOL, DAY, "2020-01-01", DAY, pinned) == tools.get_stock_data.func(SYMBOL, DAY, "2020-01-01", DAY, pinned)


def test_date_parse_error_cannot_gain_supplemental_valid_evidence(pinned):
    with run_config(CFG):
        old = tools.get_stock_data.func(SYMBOL, "not-a-date", DAY, DAY, pinned)
        new = TOOLS[0].func(SYMBOL, "not-a-date", DAY, DAY, pinned)
    assert new == old and "Supplemental" not in new


def test_duplicate_projection_preserves_native_pairs_and_originals():
    text = IDENTITY_HEADER + "STALE_DATA / missing evidence / " * 200
    messages = [HumanMessage(content="research")]
    for i in range(3):
        messages += [AIMessage(content="", tool_calls=[{"name": "get_stock_data", "args": {}, "id": str(i)}]),
                     ToolMessage(content=text, name="get_stock_data", tool_call_id=str(i))]
    original = copy.deepcopy(messages)
    projected = project_history(messages)
    assert messages == original
    assert projected[2].content == text
    assert len(projected[-1].content) < 500
    assert "No new observation" in projected[-1].content
    assert [m.tool_call_id for m in projected if isinstance(m, ToolMessage)] == ["0", "1", "2"]
    assert len(_as_text(projected)) == len(messages)
    assert project_history(projected) == projected
    assert project_history(messages) == projected  # no cross-run memory


def test_nonidentical_and_unknown_tools_never_folded():
    messages = [ToolMessage(content="x" * 3000, name="other", tool_call_id="1"),
                ToolMessage(content="x" * 3000, name="other", tool_call_id="2"),
                ToolMessage(content="y" * 3000, name="get_stock_data", tool_call_id="3")]
    assert project_history(messages) == messages


@pytest.mark.parametrize("status", ["STALE_DATA", "UNAVAILABLE", "RESEARCH_INVALID"])
def test_changed_quality_not_folded_into_prior_valid_evidence(status):
    messages = [ToolMessage(content="VALID " + "x" * 3000, name="get_verified_market_snapshot", tool_call_id="1"),
                ToolMessage(content=status + "x" * 3000, name="get_verified_market_snapshot", tool_call_id="2")]
    assert project_history(messages) == messages


def test_dedup_preserves_evidence_order_and_all_distinct_results():
    first = ToolMessage(content=IDENTITY_HEADER + "a" * 3000, name="get_indicators", tool_call_id="1")
    second = ToolMessage(content=IDENTITY_HEADER + "b" * 3000, name="get_indicators", tool_call_id="2")
    repeated = ToolMessage(content=first.content, name="get_indicators", tool_call_id="3")
    calls = AIMessage(content="", tool_calls=[
        {"name": "get_indicators", "args": {}, "id": str(i)} for i in range(1, 4)])
    result = project_history([calls, first, second, repeated])
    assert result[:3] == [calls, first, second]
    assert "tool_call_id=1" in result[-1].content


def test_stock_and_other_analysts_still_use_original_tool(monkeypatch):
    original = tools.get_stock_data.model_copy(update={"func": lambda symbol, market_snapshot=None: table()})
    assert market_tool(original).func("AAPL") == table()
    assert tools.get_stock_data.func is not TOOLS[0].func
    from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
    assert ANALYST_NODE_SPECS["market"].tools is TOOLS
    assert set(ANALYST_NODE_SPECS) == {"market", "news", "social", "fundamentals"}


def test_native_toolnode_injected_state_and_ids(pinned):
    from langgraph.prebuilt import ToolNode
    from langgraph.graph import StateGraph, START, END
    from tradingagents.agents.state import AgentState
    state = {"company_of_interest": SYMBOL, "trade_date": DAY, "market_snapshot": pinned,
             "messages": [AIMessage(content="", tool_calls=[{
                 "name": "get_stock_data", "args": {"start_date": "2020-01-01", "end_date": DAY}, "id": "native-1"}])]}
    graph = StateGraph(AgentState)
    graph.add_node("tools", ToolNode(list(TOOLS)))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    with run_config(CFG):
        result = graph.compile().invoke(state)
    message = result["messages"][-1]
    assert message.tool_call_id == "native-1" and message.name == "get_stock_data"
    assert "Bounded deterministic table" in message.content
    assert "Supplemental full-input" in message.content


def test_market_projection_not_shared_turn_or_analysts():
    import inspect
    from tradingagents.agents.analysts import news_analyst, sentiment_analyst
    from tradingagents.agents.analysts.turn import take_turn
    assert "project_history" not in inspect.getsource(take_turn)
    assert "market_display" not in inspect.getsource(news_analyst)
    assert "market_display" not in inspect.getsource(sentiment_analyst)
