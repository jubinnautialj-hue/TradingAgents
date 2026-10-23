"""Offline schema/actions, directional prices, costs and separate revalidation."""

import copy
import math

import pytest
from langchain_core.runnables import RunnableLambda
from typer.testing import CliRunner

from tests.test_graph_end_to_end import ARGS, ScriptedModel, _Client
from tests.test_okx_multitimeframe import CFG, MultiClient
from tests.test_okx_research import DAY, INFO, NOW, SYMBOL
from tradingagents.agents.perpetual_schemas import (
    PerpetualChoice,
    PerpetualResearchResult,
    ResearchAction,
)
from tradingagents.dataflows import perpetual_plan as plan
from tradingagents.dataflows.okx_research import final_gate
from tradingagents.dataflows.vendors import okx, okx_derivatives as d
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio import PortfolioContext, Position

CONFIG = {
    **CFG,
    "okx_derivatives_enabled": True,
    "okx_trade_plan_enabled": True,
    "okx_plan_target_atr": 6,
    "okx_plan_taker_fee_bps": 1,
    "okx_plan_min_net_rr": 1.1,
    "okx_plan_horizon_hours": 4,
    "okx_derivatives_history_points": 10,
}


class PlanClient(MultiClient):
    def __init__(self, direction=1, now=NOW, price=1000):
        super().__init__()
        self.direction, self.now, self.price = direction, now, price

    def candles(self, symbol, interval, cutoff, count):
        rows = super().candles(symbol, interval, cutoff, count)
        for i, row in enumerate(rows[1:]):
            close = 1000 - self.direction * i + 3 * math.sin(-i * 2 * math.pi / 10)
            row[1:5] = [str(close - 0.2), str(close + 2), str(close - 2), str(close)]
        return rows

    def get(self, path, params=None):
        rows = super().get(path, params)
        if path == "/api/v5/public/time":
            return [{"ts": str(self.now)}]
        if path == d.BOOK:
            return [
                {
                    "ts": str(self.now - 1000),
                    "asks": [[str(self.price + 0.1), "100000", "0", "1"]],
                    "bids": [[str(self.price - 0.1), "100000", "0", "1"]],
                }
            ]
        if path in (d.OI, d.FUNDING, d.MARK, d.INDEX, d.TICKER):
            rows[0]["ts"] = str(self.now - 1000)
            if path == d.FUNDING:
                rows[0].update(
                    fundingTime=str(NOW + 3600000), nextFundingTime=str(NOW + 9 * 3600000)
                )
            if path == d.MARK:
                rows[0]["markPx"] = str(self.price)
            if path == d.INDEX:
                rows[0]["idxPx"] = str(self.price)
            if path == d.TICKER:
                rows[0]["last"] = str(self.price)
        return rows


def choice(action="OPEN_LONG_CANDIDATE"):
    c = plan.default_choice("Waiting is an independent alternative").model_dump(mode="json")
    c.update(
        proposed_action=action,
        candidate_id={
            "OPEN_LONG_CANDIDATE": "LONG_BREAKOUT_RETEST",
            "OPEN_SHORT_CANDIDATE": "SHORT_BREAKOUT_RETEST",
        }.get(action),
        reasoning="Use verified conditional structure",
    )
    c["long_case"] = {
        "thesis": "Independent long case",
        "supporting_evidence_ids": ["1d:trend"],
        "opposing_evidence_ids": ["1h:momentum"],
    }
    c["short_case"] = {
        "thesis": "Independent short case",
        "supporting_evidence_ids": ["4h:structure"],
        "opposing_evidence_ids": ["1d:trend"],
    }
    return c


@pytest.fixture
def research(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(d, "utc_ms", lambda: NOW)
    b = okx.build_bundle(SYMBOL, DAY, CONFIG, PlanClient())
    assert b["status"] == "VALID", b
    s = {
        "company_of_interest": SYMBOL,
        "trade_date": DAY,
        "market_snapshot": b,
        "position_context": "UNKNOWN",
        "research_id": "test-run",
        "portfolio_choice": choice(),
        "llm_status": "AVAILABLE",
        "plan_mode": True,
    }
    return s, b


@pytest.mark.parametrize("side,action", [(1, "OPEN_LONG_CANDIDATE"), (-1, "OPEN_SHORT_CANDIDATE")])
def test_valid_independent_candidates(research, side, action):
    s, b = research
    if side == -1:
        b = okx.build_bundle(SYMBOL, DAY, CONFIG, PlanClient(-1))
    s["portfolio_choice"] = choice(action)
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert r.plan_valid and r.plan_status == "VALID_CANDIDATE", r.missing_data
    assert r.proposed_action.value == action
    assert r.long_case.thesis and r.short_case.thesis and r.wait_case
    assert not r.allow_new_risk and not r.execution_enabled and r.requires_revalidation
    assert r.position_context == "UNKNOWN"  # No invented holdings.
    assert r.reference_contracts >= float(INFO["minSz"])
    assert r.risk_reward.gross > r.risk_reward.net >= 1.1
    assert PerpetualResearchResult.model_validate_json(r.model_dump_json()) == r


def test_conflict_wait_and_no_forced_plan(research):
    s, b = research

    class ConflictClient(PlanClient):
        def candles(self, symbol, interval, cutoff, count):
            direction = self.direction
            if interval == "1h":
                self.direction = -1
            rows = super().candles(symbol, interval, cutoff, count)
            self.direction = direction
            return rows

    b = okx.build_bundle(SYMBOL, DAY, CONFIG, ConflictClient())
    assert b["confluence"]["state"] == "TREND_CONFLICT"
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert r.proposed_action == ResearchAction.WAIT and not r.plan_valid


@pytest.mark.parametrize(
    "position,action",
    [("LONG", "REDUCE_LONG"), ("SHORT", "REDUCE_SHORT"), ("LONG", "EXIT_CANDIDATE")],
)
def test_reductions_require_matching_position(research, position, action):
    s, b = research
    s.update(position_context=position, portfolio_choice=choice(action))
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert r.proposed_action.value == action and r.plan_status == "POSITION_REVIEW"
    assert not r.plan_valid and not r.allow_new_risk
    s["position_context"] = "UNKNOWN"
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert r.proposed_action == ResearchAction.WAIT and r.plan_status == "PLAN_INCOMPLETE"


def test_position_context_uses_existing_portfolio():
    assert plan.position_context(None, SYMBOL) == "UNKNOWN"
    assert plan.position_context(PortfolioContext(), SYMBOL) == "FLAT"
    assert (
        plan.position_context(
            PortfolioContext(positions=[Position(ticker=SYMBOL, quantity=-1)]), SYMBOL
        )
        == "SHORT"
    )
    assert (
        plan.position_context(
            PortfolioContext(positions=[Position(ticker=SYMBOL, quantity=1)]), SYMBOL
        )
        == "LONG"
    )
    assert (
        plan.position_context(
            PortfolioContext(
                positions=[
                    Position(ticker=SYMBOL, quantity=1),
                    Position(ticker=SYMBOL, quantity=-1),
                ]
            ),
            SYMBOL,
        )
        == "UNKNOWN"
    )


def test_wrong_stop_and_tick_hard_block(research, monkeypatch):
    s, b = research
    original = plan.candidates(b, CONFIG)
    bad = copy.deepcopy(original)
    bad["LONG_BREAKOUT_RETEST"]["stop"]["price"] = bad["LONG_BREAKOUT_RETEST"]["entry"]["price"] + 1
    monkeypatch.setattr(plan, "candidates", lambda *a: bad)
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert not r.plan_valid and r.proposed_action == ResearchAction.WAIT
    with pytest.raises(ValueError):
        plan.validate_prices(bad["LONG_BREAKOUT_RETEST"], INFO)
    bad = copy.deepcopy(original)
    bad["LONG_BREAKOUT_RETEST"]["entry"]["price"] += 0.01
    with pytest.raises(ValueError):
        plan.validate_prices(bad["LONG_BREAKOUT_RETEST"], INFO)


def test_costs_and_funding_are_deterministic(research):
    s, b = research
    p = plan.candidates(b, CONFIG)["LONG_BREAKOUT_RETEST"]
    c, rr, contracts, base = plan.calculate_costs(
        p, INFO, b["derivatives"]["components"], CONFIG, NOW
    )
    entry, stop, target = [p[k]["price"] for k in ["entry", "stop", "target"]]
    assert c.fees_usdt_at_target == pytest.approx(base * (entry + target) / 10000)
    assert c.forecast_funding_cashflow_usdt == pytest.approx(-base * entry * 0.0001)
    assert rr.gross == pytest.approx((target - entry) / (entry - stop))
    assert rr.net == pytest.approx(rr.net_profit_usdt / rr.net_loss_usdt)
    assert rr.net < rr.gross and c.funding_scope == "FORECAST_ESTIMATE_NOT_SETTLED_COST"
    assert c.forecast_settlement_times_ms == [NOW + 3600000]
    assert math.isclose(base, contracts * 0.01)
    with pytest.raises(ValueError, match="Funding schedule"):
        plan.calculate_costs(
            p, INFO, b["derivatives"]["components"], {**CONFIG, "okx_plan_horizon_hours": 10}, NOW
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("okx_plan_taker_fee_bps", None),
        ("okx_liquidity_notional_usdt", 0.00001),
        ("okx_plan_taker_fee_bps", 10000),
    ],
)
def test_missing_costs_size_and_bad_rr(research, key, value):
    s, b = research
    r, _ = plan.finalize_result(s, b, {**CONFIG, key: value})
    assert not r.plan_valid and r.plan_status == "PLAN_INCOMPLETE" and r.missing_data


def test_missing_levels_and_legacy_sell(research, monkeypatch):
    s, b = research
    monkeypatch.setattr(plan, "candidates", lambda *a: {})
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert not r.plan_valid and r.plan_status == "PLAN_INCOMPLETE"
    s["portfolio_choice"] = {"proposed_action": "SELL"}
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert r.proposed_action == ResearchAction.WAIT and not r.plan_valid
    with pytest.raises(ValueError):
        PerpetualChoice.model_validate({**choice(), "plan_valid": True})


@pytest.mark.parametrize("kind", ["rr", "size", "fee", "stop"])
def test_export_schema_rejects_inconsistent_math(research, kind):
    s, b = research
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert r.plan_valid
    payload = r.model_dump(mode="json")
    if kind == "rr":
        payload["risk_reward"]["net"] += 1
    elif kind == "size":
        payload["reference_contracts"] += 0.001
    elif kind == "fee":
        payload["estimated_costs"]["fees_usdt_at_stop"] += 1
    else:
        payload["stop_loss"]["price"] = payload["entry_reference"]["price"] + 1
    with pytest.raises(ValueError):
        PerpetualResearchResult.model_validate(payload)


def test_direct_python_validator_cannot_trust_raw_allow_flag(research):
    s, b = research
    b = copy.deepcopy(b)
    b["timeframes"]["1h"]["records"][-1]["Close"] = -1
    b["allow_new_risk"] = True
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert r.plan_status == "RESEARCH_INVALID" and not r.plan_valid


def test_expired_live_evidence_needs_separate_revalidation(research, monkeypatch):
    s, b = research
    original = copy.deepcopy(b)
    later = NOW + 301000
    monkeypatch.setattr(okx, "utc_ms", lambda: later)
    monkeypatch.setattr(d, "utc_ms", lambda: later)
    r, _ = plan.finalize_result(s, b, CONFIG)
    assert not r.plan_valid and r.requires_revalidation
    monkeypatch.setattr(okx, "OKXPublicClient", lambda: PlanClient(now=later))
    r, validation = plan.finalize_result(s, b, CONFIG, refresh=True)
    assert r.plan_valid, r.missing_data
    assert (
        validation["checked_at_ms"] == later
        and validation["research_snapshot_id"] == b["snapshot_id"]
    )
    assert b == original
    assert (
        r.evidence["original_derivative_quality"]["components"]["orderbook"]["status"]
        == "STALE_DATA"
    )
    # Repeated report/final gates reuse the saved refresh; never call the network again.
    s["entry_revalidation"] = validation
    monkeypatch.setattr(okx, "OKXPublicClient", lambda: pytest.fail("Unexpected second refresh"))
    again, _ = plan.finalize_result(s, b, CONFIG, refresh=True)
    assert again.plan_valid


def test_material_change_invalidates_without_replacing_original(research, monkeypatch):
    s, b = research
    monkeypatch.setattr(okx, "OKXPublicClient", lambda: PlanClient(price=1100))
    r, v = plan.finalize_result(s, b, CONFIG, refresh=True)
    assert (
        r.plan_status == "RESEARCH_CHANGED"
        and not r.plan_valid
        and r.proposed_action == ResearchAction.WAIT
    )
    assert b["derivatives"]["components"]["trade_current"]["data"]["price"] == 1000
    v["instrument"] = "ETH-USDT-SWAP"
    assert plan.compare_revalidation(b, v, CONFIG)


def test_slow_public_refresh_cannot_extend_research_lifetime(research, monkeypatch):
    s, b = research
    original = plan.revalidate_live

    def slow(*args, **kwargs):
        later = NOW + 901000
        monkeypatch.setattr(okx, "utc_ms", lambda: later)
        monkeypatch.setattr(d, "utc_ms", lambda: later)
        return original(b, CONFIG, PlanClient(now=later))

    monkeypatch.setattr(plan, "revalidate_live", slow)
    r, _ = plan.finalize_result(s, b, CONFIG, refresh=True)
    assert r.plan_status == "RESEARCH_INVALID" and not r.plan_valid and not r.allow_new_risk


def test_refresh_respects_configured_spread_and_index_changes(research):
    _, b = research
    v = plan.revalidate_live(b, {**CONFIG, "okx_max_spread_bps": 1}, PlanClient())
    assert v["status"] == "VALID" and v["components"]["orderbook"]["data"]["abnormal_spread"]
    assert plan.compare_revalidation(b, v, CONFIG)

    class IndexChangeClient(PlanClient):
        def get(self, path, params=None):
            rows = super().get(path, params)
            if path == d.INDEX:
                rows[0]["idxPx"] = "1100"
            return rows

    v = plan.revalidate_live(b, CONFIG, IndexChangeClient())
    assert v["status"] == "VALID" and plan.compare_revalidation(b, v, CONFIG)


def test_research_expiry_and_model_cannot_override_quality(research, monkeypatch):
    s, b = research
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW + 901000)
    monkeypatch.setattr(d, "utc_ms", lambda: NOW + 901000)
    # Even if callers forge plan_valid/allow_new_risk outside the model schema.
    s.update(research_result={"plan_valid": True}, allow_new_risk=True)
    result = final_gate(s, CONFIG)
    assert not result["research_result"]["plan_valid"] and not result["allow_new_risk"]
    assert result["research_result"]["plan_status"] == "RESEARCH_INVALID"
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW + 101000)
    monkeypatch.setattr(d, "utc_ms", lambda: NOW + 101000)
    fresh, _ = plan.finalize_result(s, b, {**CONFIG, "okx_plan_ttl_seconds": 100})
    assert fresh.plan_status == "EXPIRED"


class PlanModel(ScriptedModel):
    action: str = "WAIT"

    def with_structured_output(self, schema, **kwargs):
        if schema is PerpetualChoice:
            return RunnableLambda(
                lambda _: self._count() or PerpetualChoice.model_validate(choice(self.action))
            )
        return super().with_structured_output(schema, **kwargs)


def test_complete_existing_agents_schema_cli_and_report(research, monkeypatch, tmp_path):
    from cli import main
    from tradingagents.graph import trading_graph

    monkeypatch.setattr(okx, "OKXPublicClient", lambda: PlanClient())
    model = PlanModel(structured=True)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda *a, **k: _Client(model))
    for key in ["curr_date", "end_date", "start_date"]:
        monkeypatch.setitem(ARGS, key, DAY)
    cfg = {
        **copy.deepcopy(DEFAULT_CONFIG),
        **CONFIG,
        "results_dir": str(tmp_path),
        "memory_log_path": str(tmp_path / "memory.md"),
    }
    graph = trading_graph.TradingAgentsGraph(["market"], config=cfg)
    s, signal = graph.propagate(SYMBOL, DAY, "crypto", portfolio=PortfolioContext())
    assert signal == "REVIEW" and s["workflow_status"] == "COMPLETED"
    assert s["research_result"]["proposed_action"] == "WAIT" and s["position_context"] == "FLAT"
    report = graph.save_reports(s, SYMBOL, tmp_path / "report")
    parsed = PerpetualResearchResult.model_validate_json(
        (report.parent / "research_result.json").read_text(encoding="utf-8")
    )
    assert parsed.proposed_action == ResearchAction.WAIT and not parsed.execution_enabled
    assert (report.parent / "research_result.schema.json").exists()
    assert "FINAL TRANSACTION PROPOSAL: **SELL**" not in s["trader_investment_plan"]
    monkeypatch.setattr(main, "DEFAULT_CONFIG", cfg)
    result = CliRunner().invoke(
        main.app,
        ["okx-research", "--trade-plan", "--data-only", "--date", DAY, "--taker-fee-bps", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "Research action: WAIT" in result.output


@pytest.mark.parametrize("side,action", [(1, "OPEN_LONG_CANDIDATE"), (-1, "OPEN_SHORT_CANDIDATE")])
def test_full_mock_agents_deliver_checked_candidate(research, monkeypatch, tmp_path, side, action):
    from tradingagents.graph import trading_graph

    monkeypatch.setattr(okx, "OKXPublicClient", lambda: PlanClient(side))
    model = PlanModel(structured=True, action=action)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda *a, **k: _Client(model))
    for key in ["curr_date", "end_date", "start_date"]:
        monkeypatch.setitem(ARGS, key, DAY)
    cfg = {
        **copy.deepcopy(DEFAULT_CONFIG),
        **CONFIG,
        "results_dir": str(tmp_path),
        "memory_log_path": str(tmp_path / "memory.md"),
    }
    s, signal = trading_graph.TradingAgentsGraph(["market"], config=cfg).propagate(
        SYMBOL, DAY, "crypto"
    )
    r = PerpetualResearchResult.model_validate(s["research_result"])
    assert r.proposed_action.value == action and r.plan_valid, r.blocking_reasons
    assert r.evidence["entry_revalidation"]["status"] == "VALID"
    assert s["workflow_status"] == "COMPLETED" and signal == "REVIEW"
    assert not s["allow_new_risk"] and not r.execution_enabled
