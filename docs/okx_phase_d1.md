# D.1: BTC-USDT-SWAP offline historical simulation

This is a deterministic rule-only simulation, not the original stock decision-quality backtest and not a trading service. No LLM, account, order, margin, leverage or liquidation capability is added. `execution_enabled=False` throughout.

## Run (PowerShell)

```powershell
cd D:\_01trading_agents
.\.venv\Scripts\python.exe -m tradingagents.okx_backtest --start 2026-10-12T00:00:00Z --end 2026-10-22T00:00:00Z --initial-equity 10000 --notional-usdt 1000 --taker-fee-bps 5 --slippage-bps 2 --assume-current-contract-spec
```

Fees/slippage are caller assumptions, NOT account fees. `--assume-current-contract-spec` explicitly acknowledges unverified historical lot/tick/minimum/contract settings. Maximum test window is 14 days. The terminal hour must have closed before download. Default output is a separate `~/.tradingagents/logs/okx_simulations/<content hash>/` directory.

Replay without any network by adding `--input <saved run directory>\inputs.json` with the same UTC window and assumptions. Repeated identical inputs, source hashes and runtime versions map to the same directory; identical completed output is returned without appending trades. An interrupted run can be rerun safely; it does not checkpoint each candle. Completed result.json is written atomically after exports; original stock memories and research reports are untouched.

## Reuse and strategy boundary

- `plan_lifecycle.evaluate_signal`, `timing`, `prepare_historical_signal`: original closed-bar confirmation and historical template quality checks.
- `perpetual_plan.candidates`, `validate_prices`, `round_tick`: existing reference prices, stop/target geometry, tick rounding.
- `okx.validate_candles`, `check_bundle`, `fingerprint`, `validate_instrument`: original data quality gates. All three periods require 250 warmup bars; last 400 are used for each decision.
- `okx_multitimeframe.analyze_timeframe`, `confluence`: existing indicators, confirmed pivots and strict three-period trend resonance.
- `okx_derivatives.history_pages`, `parse_funding_history`, `funding_cashflow`: bounded public retrieval and actual realized rate semantics.
- `dataflows.files.locked`: same cross-process locking primitive as Memory. No stock Memory/Reflection records are reused as simulation P&L.

One decision every 4h from start. LONG requires three-period UP resonance, SHORT requires DOWN. Conflicts, insufficient pivots or invalid geometry yield WAIT. This validates the deterministic structural template subset, NOT the full Stage C multi-agent selection, live order-book cost gate, or a claim that LLM selection adds value. No future realized funding is used to select candidates. A future Stage D.2 must version and evaluate additional selection filters separately.

All historical levels and indicators are regenerated from candles closed by that decision cutoff. Today's Stage C reports are never imported. Source hashes and Python/pandas/stockstats/Pydantic versions accompany outputs. Stable snapshot IDs bind only the historical input, contract assumption and rules, not wall-clock reconstruction times.

## Matching and accounting

Single pending hypothesis or position; later decisions while active are logged as skipped. Default confirmation window 6 full 1h bars; breakout and later closed-bar retest must complete before expiry. ENTRY_ELIGIBLE at a close can fill at the next bar open. Entry outside original stop/target geometry is rejected. Confirmation-bar OHLC cannot generate an earlier fill.

Fixed reference notional 1000 USDT, initial equity 10000 USDT. Quantity floors to contract lots using assumed current multiplier and minimum. No leverage amplification: reference notional plus entry fee must fit simulated cash; only one position. This is an exposure constraint, not validation of exchange margin or liquidation safety.

Entry and exit incur explicit adverse slippage plus adverse tick rounding. Both legs are Taker. Slippage is embedded in fill prices and reported separately; it is NOT deducted twice. Stops precede targets if both touch in an hour; gapped stops use the worse open, targets get no favorable gap improvement. Intrabar exit timing is unknown and recorded as known by close; the position is not carried into the next settlement boundary. Maximum holding is 12 hours, exited at that boundary open. Remaining positions exit at the predeclared window-end open. Post-entry plan expiry alone does not close a position; original stop, target, max holding and window boundary govern exits.

Funding applies to positions carried into a settlement-hour open, before scheduled boundary exits. Positions newly entered at that open do not receive/pay that boundary. Settlement notional uses that hour's TRADE OPEN, an explicit price proxy, not historical mark price. Only `realizedRate` is used; forecast fields are never charged. The default historical schedule assumption is every 8h UTC. Every expected event and bracketing events must exist, with no extras/duplicates in the window; a schedule change or missing record blocks the run. This check cannot prove that an exchange archive omitted no off-grid event; schedule is an explicit assumption, not an inferred guarantee. There are no trades in the real sample to validate actual holding-period funding against exchange charges.

## Results and feedback

- `inputs.json`: complete public data, config, source/version identity.
- `result.json`: deterministic metrics, decisions, events, trades, equity, assumptions and limits.
- `trades.csv`, `events.csv`, `equity.csv`: shareable records. Empty trades still export headers.
- `feedback(result, as_of_ms)`: only simulated trades whose outcome was known by the cutoff; never automatically injected into stock Memory or agents.

Hour-close equity includes unrealized P&L. Drawdown includes initial equity and terminal settlement, but not intrabar equity extremes. Profit Factor/win rate/average win/loss are null when undefined, including zero trades. Valid NO_TRADES is distinct from DATA_OR_CONFIGURATION_INVALID (exit code 2); data failure is never silently recorded as zero return.

## Public historical sample

UTC window 2026-10-12 00:00 through 2026-10-22 00:00, unchanged strategy and costs.

- 1h: 643 confirmed bars, first open 2026-09-25 06:00, last close 2026-10-22 01:00 (terminal open verification).
- 4h: 463 confirmed bars, first open 2026-08-05 20:00, last close 2026-10-22 00:00.
- UTC 1d: 413 confirmed bars, first open 2025-09-04, last close 2026-10-22.
- Actual funding: 200 records, 2026-08-17 16:00 through 2026-10-23 00:00. Only settlement events reached during the simulated holding period can affect equity.
- 60 decision points, 60 WAIT, zero candidates, zero simulated trades. Initial/final equity 10000 USDT, net P&L 0, fees/funding/slippage 0, observed drawdown 0. Win rate and Profit Factor undefined.

This is data/replay pipeline validation with NO effective trading sample. It is not evidence of profitability, loss avoidance, margin safety or realistic execution. Conditions were not loosened to manufacture trades. Long/short fills and accounting are validated using fixed synthetic test plans.

## Remaining work (not implemented)

Historical contract specification versions, true mark-based funding notional, margin tiers/liquidation, historical liquidity, alternate settlement schedules, statistical sample adequacy, out-of-sample evaluation, model selection comparisons and automated Reflection. Suggested D.2: freeze rules, collect more covered windows, add historical specification/mark evidence, then compare a small versioned historical LLM sample to the frozen deterministic baseline. No automatic strategy tuning or execution authorization.
