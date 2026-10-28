from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.analysts.market_display import market_tool, project_history
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import get_indicators, get_stock_data, get_verified_market_snapshot

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    market_tool(get_stock_data),
    market_tool(get_indicators),
    get_verified_market_snapshot,
)


def create_market_analyst(llm):

    def market_analyst_node(state):
        if state.get("candidate_filter_mode"):
            from tradingagents.agents.candidate_filter import market_filter_report
            return market_filter_report(llm, state)
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)
        if state.get("market_snapshot", {}).get("multi_timeframe"):
            instrument_context += (
                " This is REQUIRED three-timeframe research, not an anchor-only report. "
                "Read get_verified_market_snapshot for deterministic structures and independent indicators. "
                "Organize the report as 1d primary regime, 4h structure, 1h short-term confirmation, "
                "then explicit agreement/conflict/insufficient-evidence synthesis and derivative limitations. "
                "Quote each timeframe's latest closed UTC time and common cutoff. Never majority-vote "
                "BUY/SELL or reinterpret a short timeframe as the whole market. Pivots only become "
                "known at confirmed_at_ms; rejected breaks/retests are rule candidates, not proof or orders."
            )
        if state.get("market_snapshot"):
            instrument_context += (
                " All tools share a pinned OKX candle bundle. Indicator periods count candles, "
                "not days (a 200 SMA on 1h is 200 hours). Use the research date for each tool's "
                "end/as-of date; historical lookbacks cannot manufacture unavailable coverage. "
                "Volume is base currency; VolumeContracts is contract count and VolumeQuote is USDT. "
                "State partial history coverage and data-quality exclusions explicitly."
                " If optional derivative evidence is present, use its per-component status, UTC timestamp "
                "and history coverage. Distinguish forecast funding from realized settlements and USD OI "
                "from USDT notional. OI cannot identify aggressive long/short opening. Displayed book "
                "imbalance is not executed buy/sell flow; depth slippage is an estimate, not a fill guarantee. "
                "Do not infer account liquidation prices from mark prices without account evidence. "
                "Missing or expired optional evidence is unknown, not a directional signal."
            )

        system_message = (
            """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.
- close_200_sma: 200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.
- close_10_ema: 10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.
- macds: MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.
- atr: ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.

- Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). Also briefly explain why they are suitable for the given market context. When you tool call, please use the exact name of the indicators provided above as they are defined parameters, otherwise your call will fail. Please make sure to call get_stock_data first to retrieve the CSV that is needed to generate indicators. Then use get_indicators with the specific indicator names.

Before writing the final report, call get_verified_market_snapshot for this ticker and the current date, and treat it as the source of truth for any exact OHLCV, price-level, or indicator-value claim. If another tool's output conflicts with the verified snapshot, flag the discrepancy rather than inventing a reconciled number. Do not claim historical validation, support/resistance bounces, or exact percentage moves unless they are directly supported by tool output with concrete dates and prices.

Write a very detailed and nuanced report of the trends you observe. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."""
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        messages = (project_history(state["messages"])
                    if state.get("market_snapshot", {}).get("exchange") == "OKX" else state["messages"])
        result, report = take_turn(prompt, llm, TOOLS, messages)

        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node
