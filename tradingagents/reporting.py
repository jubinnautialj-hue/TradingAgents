"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus a consolidated ``complete_report.md`` under ``save_path``. The
CLI and ``TradingAgentsGraph.save_reports`` both call this, so a headless / API
run produces the same on-disk report tree a CLI run does.
"""

from datetime import datetime
from pathlib import Path

from tradingagents.agents.rating import run_rating
from tradingagents.dataflows.okx_research import final_gate
from tradingagents.dataflows.vendors.okx import MODE, describe


def checked_report_state(final_state: dict, settings: dict | None) -> dict:
    if final_state.get("market_snapshot") or (settings or {}).get("market_mode") == MODE:
        config = {"market_mode": MODE, "okx_interval": final_state.get("market_snapshot", {}).get("interval", "1h"),
                  **(settings or {})}
        return {**final_state, **final_gate(final_state, config)}
    return final_state


def analyst_names(keys) -> list[str]:
    """The analysts as users select them: the sentiment analyst's key is "social"."""
    return ["sentiment" if key == "social" else key for key in keys or []]


def _header(ticker: str, final_state: dict, settings: dict | None) -> str:
    """The report's title and what produced it: analysis date, version, models, analysts, vendors."""
    lines = [f"# Trading Analysis Report: {ticker}", ""]
    if final_state.get("llm_status"):
        lines.append(f"- Model status: {final_state['llm_status']}; workflow: {final_state.get('workflow_status', 'NOT_STARTED')}")
    if final_state.get("trade_date"):
        lines.append(f"- Analysis date: {final_state['trade_date']}")
    if final_state.get("research_result"):
        result = final_state["research_result"]
        lines.append(f"- Research action: {result['proposed_action']}; plan_status: {result['plan_status']}; plan_valid: {result['plan_valid']}")
    elif final_state.get("final_trade_decision"):
        lines.append(f"- Rating: {run_rating(final_state)}")
    lines.append(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if settings:
        s = settings.get
        deep_provider = s("deep_think_provider") or s("llm_provider", "?")
        quick_provider = s("quick_think_provider") or s("llm_provider", "?")
        if deep_provider == quick_provider:
            models = f"{deep_provider}, deep {s('deep_think_llm', '?')}, quick {s('quick_think_llm', '?')}"
        else:   # each tier on its own provider (#1440)
            models = (f"deep {deep_provider} {s('deep_think_llm', '?')}, "
                      f"quick {quick_provider} {s('quick_think_llm', '?')}")
        lines.append(f"- TradingAgents {s('version', '?')}: {models}")
        lines.append(f"- Analysts: {', '.join(analyst_names(s('analysts')))}; "
                     f"research debate rounds {s('max_debate_rounds', '?')}, "
                     f"risk debate rounds {s('max_risk_discuss_rounds', '?')}")
        vendors = {**(s("data_vendors") or {}), **(s("tool_vendors") or {})}
        if vendors:
            lines.append("- Data vendors: " + ", ".join(f"{k} {v}" for k, v in vendors.items()))
    if final_state.get("memory_note"):
        lines.append(f"- Memory log: {final_state['memory_note']}")
    if final_state.get("market_snapshot"):
        lines.append("\n" + describe(final_state["market_snapshot"]))
    if final_state.get("event_evidence"):
        from tradingagents.dataflows.vendors.okx_events import render_events

        lines.append(render_events(final_state["event_evidence"]))
    if final_state.get("price_depth_evidence"):
        from tradingagents.dataflows.vendors.okx_price_depth import render_price_depth

        lines.append(render_price_depth(final_state["price_depth_evidence"]))
    if final_state.get("market_checks_evidence"):
        from tradingagents.dataflows.vendors.okx_market_checks import render_checks

        lines.append(render_checks(final_state["market_checks_evidence"]))
    if final_state.get("orderflow_evidence"):
        from tradingagents.dataflows.vendors.okx_orderflow import render_orderflow

        lines.append(render_orderflow(final_state["orderflow_evidence"]))
    return "\n".join(lines) + "\n\n"


# The report's sections in order: (heading, folder, [(agent, file, state path)]).
_SECTIONS = (
    ("I. Analyst Team Reports", "1_analysts", (
        ("Market Analyst", "market.md", ("market_report",)),
        ("Sentiment Analyst", "sentiment.md", ("sentiment_report",)),
        ("News Analyst", "news.md", ("news_report",)),
        ("Fundamentals Analyst", "fundamentals.md", ("fundamentals_report",)),
    )),
    ("II. Research Team Decision", "2_research", (
        ("Bull Researcher", "bull.md", ("investment_debate_state", "bull_history")),
        ("Bear Researcher", "bear.md", ("investment_debate_state", "bear_history")),
        ("Research Manager", "manager.md", ("investment_plan",)),
    )),
    ("III. Trading Team Plan", "3_trading", (
        ("Trader", "trader.md", ("trader_investment_plan",)),
    )),
    ("IV. Risk Management Team Decision", "4_risk", (
        ("Aggressive Analyst", "aggressive.md", ("risk_debate_state", "aggressive_history")),
        ("Conservative Analyst", "conservative.md", ("risk_debate_state", "conservative_history")),
        ("Neutral Analyst", "neutral.md", ("risk_debate_state", "neutral_history")),
    )),
    ("V. Portfolio Manager Decision", "5_portfolio", (
        ("Portfolio Manager", "decision.md", ("final_trade_decision",)),
    )),
)


def _report_parts(final_state: dict) -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """The sections with something to say: (heading, folder, [(agent, file, text)])."""
    parts = []
    for heading, folder, agents in _SECTIONS:
        written = []
        for agent, filename, path in agents:
            value = final_state
            for key in path:
                value = (value or {}).get(key)
            if value:
                written.append((agent, filename, value))
        if written:
            parts.append((heading, folder, written))
    return parts


def write_report_tree(final_state: dict, ticker: str, save_path, settings: dict | None = None,
                      html: bool = True) -> Path:
    """Save a completed run's reports to ``save_path``; return the complete-report path.

    ``settings`` (``TradingAgentsGraph.run_settings()``) adds what produced the run
    to the report's header. ``html`` also writes ``complete_report.html``, one
    self-contained page of the same report (#1419).
    """
    save_path = Path(save_path)
    final_state = checked_report_state(final_state, settings)
    save_path.mkdir(parents=True, exist_ok=True)
    if final_state.get("research_result"):
        import json

        from tradingagents.agents.perpetual_schemas import PerpetualResearchResult

        result = PerpetualResearchResult.model_validate(final_state["research_result"])
        (save_path / "research_result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
        (save_path / "research_result.schema.json").write_text(json.dumps(
            PerpetualResearchResult.model_json_schema(), indent=2), encoding="utf-8")
    if final_state.get("market_snapshot", {}).get("multi_timeframe"):
        import json

        # Research evidence archive, not a stage-C executable trade plan.
        (save_path / "multitimeframe_snapshot.json").write_text(json.dumps(
            final_state["market_snapshot"], ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if final_state.get("market_snapshot", {}).get("derivatives"):
        import json

        from tradingagents.dataflows.vendors.okx_derivatives import evidence_view

        snapshot = final_state["market_snapshot"]
        evidence = evidence_view(snapshot["derivatives"])
        (save_path / "derivatives_snapshot.json").write_text(json.dumps(
            {"snapshot_id": snapshot["snapshot_id"], "core_data_quality": snapshot["status"],
             "allow_new_risk": final_state["allow_new_risk"], "execution_enabled": False,
             "derivatives": evidence}, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if final_state.get("orderflow_evidence"):
        import json
        from tradingagents.dataflows.vendors.okx_orderflow import evidence_view

        raw = final_state["orderflow_evidence"]
        (save_path / "orderflow_snapshot.json").write_text(json.dumps(
            {"original": raw, "effective_view": evidence_view(raw), "execution_enabled": False},
            indent=2, allow_nan=False), encoding="utf-8")
    if final_state.get("market_checks_evidence"):
        import json
        from tradingagents.dataflows.vendors.okx_market_checks import evidence_view

        raw = final_state["market_checks_evidence"]
        (save_path / "market_checks_snapshot.json").write_text(json.dumps(
            {"original": raw, "effective_view": evidence_view(raw), "execution_enabled": False},
            indent=2, allow_nan=False), encoding="utf-8")
    if final_state.get("price_depth_evidence"):
        import json
        from tradingagents.dataflows.vendors.okx_price_depth import evidence_view

        raw = final_state["price_depth_evidence"]
        (save_path / "price_depth_snapshot.json").write_text(json.dumps(
            {"original": raw, "effective_view": evidence_view(raw), "execution_enabled": False},
            indent=2, allow_nan=False), encoding="utf-8")
    if final_state.get("event_evidence"):
        import json
        import hashlib
        from tradingagents.agents.analysts import event_context
        from tradingagents.dataflows.vendors import okx_events
        from tradingagents.dataflows.vendors.okx_events import evidence_view

        raw = final_state["event_evidence"]
        allowed = ("version", "llm_provider", "deep_think_provider", "quick_think_provider",
                   "deep_think_llm", "quick_think_llm", "analysts", "max_debate_rounds",
                   "max_risk_discuss_rounds", "output_language", "market_mode", "okx_interval",
                   "okx_events_enabled", "okx_events_lookback_days")
        metadata = {"prompt_version": event_context.PROMPT_VERSION,
                    "settings": {k: settings[k] for k in allowed if settings and k in settings},
                    "code_sha256": {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                    for name, module in (("events", okx_events), ("event_prompt", event_context))}}
        (save_path / "events_snapshot.json").write_text(json.dumps(
            {"original": raw, "effective_view": evidence_view(raw), "research_metadata": metadata, "execution_enabled": False},
            indent=2, allow_nan=False), encoding="utf-8")
    parts = _report_parts(final_state)
    sections = []
    for heading, folder, written in parts:
        (save_path / folder).mkdir(exist_ok=True)
        for _agent, filename, text in written:
            (save_path / folder / filename).write_text(text, encoding="utf-8")
        content = "\n\n".join(f"### {agent}\n{text}" for agent, _filename, text in written)
        sections.append(f"## {heading}\n\n{content}")

    (save_path / "complete_report.md").write_text(
        _header(ticker, final_state, settings) + "\n\n".join(sections), encoding="utf-8"
    )
    if html:
        from tradingagents.report_html import render_report

        (save_path / "complete_report.html").write_text(
            render_report(ticker, final_state, settings, parts), encoding="utf-8"
        )
    return save_path / "complete_report.md"
