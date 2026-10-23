"""Exercise real LangGraph wiring and both public entry paths without network."""

import copy

import pytest
from typer.testing import CliRunner

from tests.test_graph_end_to_end import ScriptedModel, _Client
from tests.test_okx_research import CONFIG, DAY, NOW, SYMBOL, FakeClient
from tradingagents.dataflows.vendors import okx
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph


@pytest.fixture
def graph_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    bundle = okx.build_bundle(SYMBOL, DAY, CONFIG, FakeClient())
    model = ScriptedModel(structured=True)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda *a, **k: _Client(model))
    monkeypatch.setattr("tradingagents.dataflows.okx_research.build_bundle", lambda *a, **k: copy.deepcopy(bundle))
    config = {**copy.deepcopy(DEFAULT_CONFIG), **CONFIG, "results_dir": str(tmp_path / "results"),
              "data_cache_dir": str(tmp_path / "cache"), "memory_log_path": str(tmp_path / "memory.md")}
    return model, bundle, config


def test_python_api_valid_and_stock_settlement_disabled(graph_setup):
    model, _, config = graph_setup
    graph = trading_graph.TradingAgentsGraph(["market"], config=config)
    state, signal = graph.propagate(SYMBOL, DAY, "crypto")
    assert state["data_quality"] == "VALID" and state["allow_new_risk"]
    assert not state["execution_enabled"] and signal == "Overweight"
    assert "Yahoo settlement is disabled" in state["memory_note"]
    assert len(model.calls) > 0
    with pytest.raises(ValueError, match="Yahoo fallback is blocked"):
        graph.settle_pending(SYMBOL)


def test_preflight_stops_all_llms(graph_setup, monkeypatch):
    model, bundle, config = graph_setup
    invalid = {**bundle, "status": "PARTIAL", "allow_new_risk": False, "issues": ["missing bars"]}
    monkeypatch.setattr("tradingagents.dataflows.okx_research.build_bundle", lambda *a, **k: invalid)
    graph = trading_graph.TradingAgentsGraph(["market"], config=config)
    state, signal = graph.propagate(SYMBOL, DAY, "crypto")
    assert not model.calls and signal == "REVIEW"
    assert not state["allow_new_risk"] and state["data_quality"] == "PARTIAL"


def test_final_graph_gate_overrides_model_buy(graph_setup, monkeypatch):
    _, _, config = graph_setup
    graph = trading_graph.TradingAgentsGraph(["market"], config=config)
    # Preflight succeeds; simulate a run exceeding the bundle lifetime just
    # before PM returns an explicit Buy. The final graph node must veto it.
    def buy(_llm):
        def node(state):
            monkeypatch.setattr(okx, "utc_ms", lambda: NOW + 901000)
            return {"final_rating": "Buy", "final_trade_decision": "**Rating**: Buy"}
        return node
    monkeypatch.setattr("tradingagents.graph.setup.create_portfolio_manager", buy)
    graph = trading_graph.TradingAgentsGraph(["market"], config=config)
    state, signal = graph.propagate(SYMBOL, DAY, "crypto")
    assert signal == "REVIEW" and state["data_quality"] == "STALE_DATA"
    assert not state["allow_new_risk"] and "RESEARCH_INVALID" in state["final_trade_decision"]


def test_full_cli_uses_same_graph_gate(graph_setup, monkeypatch):
    _, bundle, config = graph_setup
    from cli import main
    monkeypatch.setattr(main, "DEFAULT_CONFIG", config)
    invalid = {**bundle, "status": "DATA_UNAVAILABLE", "allow_new_risk": False}
    monkeypatch.setattr("tradingagents.dataflows.okx_research.build_bundle", lambda *a, **k: invalid)
    result = CliRunner().invoke(main.app, ["okx-research", "--date", DAY])
    assert result.exit_code == 2, result.output
    assert "allow_new_risk=False" in result.output


def test_explicit_mode_and_no_unsupported_analysts(graph_setup):
    _, _, config = graph_setup
    with pytest.raises(ValueError, match="requires selected_analysts"):
        trading_graph.TradingAgentsGraph(["market", "news"], config=config)
