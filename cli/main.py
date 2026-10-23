import sys
from enum import StrEnum
from typing import Annotated

import typer
from rich.markup import escape

from cli.display import console
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import run_analysis
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
    html: bool | None = typer.Option(
        None, "--html/--no-html",
        help="Save the report as one HTML page too, complete_report.html, without asking (default: yes)",
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals; omit for all the asset type allows"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim][{done}/{total}] {ticker} {date}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}")
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


# The plans `login` and `logout` take: the providers signed in to, not keyed.
@app.command("okx-research")
def okx_research(
    instrument: str = typer.Option("BTC-USDT-SWAP", "--instrument"),
    interval: str = typer.Option("1h", "--interval", help="1h, 4h or 1d (UTC)"),
    date: str = typer.Option(None, "--date", help="UTC research day; default UTC today"),
    data_only: bool = typer.Option(False, "--data-only", help="Public data/quality report without LLM calls"),
    derivatives: bool = typer.Option(False, "--derivatives", help="Include optional public OI/funding/mark/index/depth evidence"),
    orderflow: bool = typer.Option(False, "--orderflow", help="Optional bounded closed-minute taker Delta/CVD and matching spot evidence"),
    flow_minutes: int = typer.Option(1, "--flow-minutes", min=1, max=60),
    flow_max_pages: int = typer.Option(60, "--flow-max-pages", min=1, max=100),
    multi_timeframe: bool = typer.Option(False, "--multi-timeframe", help="Joint 1d regime / 4h structure / 1h confirmation; interval remains the labelled anchor"),
    trade_plan: bool = typer.Option(False, "--trade-plan", help="Schema-validated perpetual research; enables three timeframes and derivatives; never executes"),
    taker_fee_bps: float = typer.Option(None, "--taker-fee-bps", min=0, help="Explicit fee assumption in basis points, not fetched account fee"),
    maker_fee_bps: float = typer.Option(None, "--maker-fee-bps", help="Optional maker assumption; costs conservatively use taker"),
    portfolio_file: str = typer.Option(None, "--portfolio", help="Optional caller portfolio JSON; no account API query"),
    liquidity_notional: float = typer.Option(10000, "--liquidity-notional", min=1, help="USDT reference size for displayed-depth estimates"),
):
    """Research OKX linear USDT swaps. Public data only; execution always disabled."""
    from copy import deepcopy
    from datetime import UTC, datetime
    from pathlib import Path
    from uuid import uuid4

    from tradingagents.dataflows.config import run_config
    from tradingagents.dataflows.okx_research import final_gate, preflight
    from tradingagents.dataflows.vendors.okx import BARS, MODE, render_snapshot, validate_symbol
    from tradingagents.graph.trading_graph import TradingAgentsGraph, _validate_trade_date
    from tradingagents.reporting import write_report_tree

    try:
        validate_symbol(instrument)
        if interval not in BARS:
            raise ValueError("interval must be 1h, 4h or 1d")
        day = _validate_trade_date(date or datetime.now(UTC).strftime("%Y-%m-%d"))
        config = deepcopy(DEFAULT_CONFIG)
        if not data_only:
            import os

            from cli.prefs import load_last_run, sanitize
            prefs = sanitize(load_last_run(), AssetType.CRYPTO)
            # Reuse existing model choices without rewriting preferences; explicit
            # environment settings retain precedence over remembered choices.
            variables = {"llm_provider": "TRADINGAGENTS_LLM_PROVIDER",
                         "quick_think_llm": "TRADINGAGENTS_QUICK_THINK_LLM",
                         "deep_think_llm": "TRADINGAGENTS_DEEP_THINK_LLM",
                         "backend_url": "TRADINGAGENTS_LLM_BACKEND_URL",
                         "output_language": "TRADINGAGENTS_OUTPUT_LANGUAGE"}
            for key, variable in variables.items():
                if key in prefs and not os.environ.get(variable):
                    config[key] = prefs[key]
        config.update(market_mode=MODE, okx_interval=interval, checkpoint_enabled=False)
        config.update(okx_derivatives_enabled=derivatives, okx_liquidity_notional_usdt=liquidity_notional)
        config.update(okx_orderflow_enabled=orderflow, okx_orderflow_minutes=flow_minutes,
                      okx_orderflow_max_pages=flow_max_pages)
        multi_timeframe = multi_timeframe or trade_plan
        config["okx_multi_timeframe"] = multi_timeframe
        config["okx_trade_plan_enabled"] = trade_plan
        if trade_plan:
            config.update(okx_derivatives_enabled=True, okx_plan_taker_fee_bps=taker_fee_bps,
                          okx_plan_maker_fee_bps=maker_fee_bps)
        portfolio = load_portfolio(portfolio_file) if portfolio_file else None
        # Accurate configured source labels; all market tools use the bundle.
        config["data_vendors"].update(core_stock_apis="okx", technical_indicators="okx")
        if data_only:
            state = {"company_of_interest": instrument, "trade_date": day}
            if trade_plan:
                from tradingagents.dataflows.perpetual_plan import position_context

                state["position_context"] = position_context(portfolio, instrument)
            with run_config(config):
                state.update(preflight(state))
                state.update(final_gate(state))
            if state["allow_new_risk"]:
                state.update(market_report=render_snapshot(state["market_snapshot"]),
                             final_rating="REVIEW", final_trade_decision="Data validation only; no model trading recommendation. Execution disabled.")
            settings = {k: config[k] for k in ("market_mode", "okx_interval", "okx_bundle_ttl_seconds",
                                               "okx_min_bars", "okx_history_bars", "okx_max_lag_bars",
                                               "okx_derivatives_enabled", "okx_derivatives_history_points",
                                               "okx_derivatives_ttl_seconds", "okx_liquidity_notional_usdt", "okx_max_spread_bps",
                                               "okx_multi_timeframe", "okx_pivot_window", "okx_structure_window", "okx_retest_atr_fraction") if k in config}
            settings.update({k: v for k, v in config.items() if k.startswith("okx_plan_") or k == "okx_trade_plan_enabled"})
            settings.update({k: v for k, v in config.items() if k.startswith("okx_orderflow_")})
        else:
            graph = TradingAgentsGraph(selected_analysts=["market"], config=config)
            state, _ = graph.propagate(instrument, day, asset_type="crypto", portfolio=portfolio)
            settings = graph.run_settings()
        mode_label = "multi" if multi_timeframe else interval
        folder = Path(config["results_dir"]) / "reports" / f"okx_{instrument}_{mode_label}_{uuid4().hex[:12]}"
        report = write_report_tree(state, instrument, folder, settings=settings)
        if state.get("market_snapshot", {}).get("derivatives"):
            from tradingagents.dataflows.vendors.okx_derivatives import evidence_view

            typer.echo(f"Derivative evidence: {evidence_view(state['market_snapshot']['derivatives'])['status']} (optional; see report coverage)")
        typer.echo(f"Data quality: {state['data_quality']}; allow_new_risk={state['allow_new_risk']}; execution_enabled=False")
        if not data_only:
            typer.echo(f"Model: {state.get('llm_status', 'NOT_STARTED')}; workflow: {state.get('workflow_status', 'NOT_STARTED')}")
        typer.echo(f"Report: {report}")
        if state.get("research_result"):
            result = state["research_result"]
            typer.echo(f"Research action: {result['proposed_action']}; plan_status={result['plan_status']}; plan_valid={result['plan_valid']}; requires_revalidation={result['requires_revalidation']}")
            typer.echo(f"Research JSON: {folder / 'research_result.json'}")
        if state.get("llm_status") == "LLM_UNAVAILABLE":
            typer.echo(f"LLM_UNAVAILABLE: {state['llm_failure']['kind']} at {state['llm_failure']['stage']}")
            raise typer.Exit(code=3)
        if (trade_plan and state["data_quality"] != "VALID") or (not trade_plan and not state["allow_new_risk"]):
            raise typer.Exit(code=2)
    except ValueError as exc:
        typer.echo(f"OKX research configuration error: {exc}", err=True)
        raise typer.Exit(code=1) from None


Plan = StrEnum("Plan", {name: name for name in SIGNED_IN_PROVIDERS})


@app.command()
def login(plan: Annotated[Plan, typer.Argument(help="The plan to sign in with")]):
    """Sign in with your ChatGPT Plus or Pro plan, for the chatgpt provider."""
    from tradingagents.llm_clients import chatgpt_auth

    try:
        # One unwrapped line, so the sign-in address can be copied whole.
        email = chatgpt_auth.sign_in(
            say=lambda line: console.print(line, soft_wrap=True, markup=False, highlight=False))
    except chatgpt_auth.ChatGPTAuthError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Signed in to ChatGPT as {escape(email)}.[/green]")


@app.command()
def logout(plan: Annotated[Plan, typer.Argument(help="The plan to sign out of")]):
    """Sign out of your ChatGPT plan: revoke this machine's access and forget it."""
    from tradingagents.llm_clients import chatgpt_auth

    outcome = chatgpt_auth.sign_out()
    if outcome == "revoked":
        console.print("Signed out of ChatGPT.")
    elif outcome == "forgotten":
        console.print("[yellow]Signed out here, but OpenAI could not be reached to revoke the sign-in; "
                      "it stays valid at OpenAI until it expires.[/yellow]")
    else:
        console.print("Not signed in to ChatGPT.")


if __name__ == "__main__":
    app()
