# D.3 continuous historical research (conditional simulation)

No account/private API, trading, scanning, optimizer or new agent. execution_enabled=False and delivered allow_new_risk=False. Original stock Backtest and Memory are unchanged.

## Implementation and freeze

`okx_d3_strategy_freeze.json` records the pre-development configuration and source checksums. Numerical strategy files (perpetual_plan, plan_lifecycle and okx_multitimeframe) must still match these checksums. Strategy and configuration fingerprints are saved separately from the accounting-model fingerprint and runtime versions.

Rules remain three-timeframe resonance, original deterministic breakout/retest and price geometry, six confirmed hourly bars, twelve-hour maximum holding, target 3 x 4h ATR, stop buffer 0.25 x 1h ATR, explicit Taker 5bps per leg, adverse slippage 2bps plus tick rounding, fixed exposure 1000 USDT and initial equity 10000 USDT. No candidate or confirmation filter was loosened.

- `okx_continuous.fetch_history`: cached REST candles/mark candles, official monthly funding ZIPs plus recent REST tail.
- `verify_archive_evidence`: ZIP/hash/schema/UTC+8 month partition, true settlement timestamps, archive/REST actual-rate overlap and coverage validation.
- `decision_history`: after full-source OHLCV/gap/volume validation, gives the original decision generator its 400 past bars and processing segment; it never changes per-cutoff filtering or indicators. Full and cropped real-history decisions compare equal.
- `continuous_simulation` / `run_continuous`: reuse the D.1 ledger exactly once over the entire chronological stream. Processing boundaries are not terminal exits, capital resets or pending-state resets. Merge rejects gaps and overlapping chunks.
- `validation_protocol`: protected future interval and explicit historical-development labeling.

Minimal shared changes: public endpoint allowlist adds the official archive listing; cached public client adds archive/mark paths; ledger accepts optional larger window and historical funding price map; independent audit accepts the same explicit price evidence. Defaults for D.1/D.2 remain unchanged.

## Official long-history evidence

Official documentation: https://www.okx.com/docs-v5/en/#public-data-rest-api-get-historical-market-data

Module 3 is funding history. Monthly listing dates use UTC+8; raw funding event timestamps remain absolute UTC milliseconds. ZIPs are used only from official HTTPS static.okx.com funding archive paths, retained with SHA256/base64 evidence and bounded download/extraction sizes. No credentials or API keys are used. No ZIP files are extracted to arbitrary paths.

Actual inspected window: 2025-10-01 00:00 through 2026-10-22 00:00 UTC, 386 days:

| Evidence | Count | Actual coverage |
|---|---:|---|
| 1h trading candles | 9667 | first open 2025-09-14 06:00; terminal verification close 2026-10-22 01:00 |
| 4h trading candles | 2719 | first open 2025-07-25 20:00; last close 2026-10-22 00:00 |
| UTC daily candles | 789 | first open 2024-08-24; last close 2026-10-22 |
| Historical hourly mark candles | 9265 | 2025-10-01 00:00 through terminal open 2026-10-22 00:00 |
| Official monthly funding files | 12 / 1095 records | Oct 2025 through Sep 2026 (UTC+8 archive month) |
| Recent REST funding tail | 282 records | raw paginated official responses retained |
| Archive/REST overlap | 216 | all actual-rate comparisons pass tolerance |
| Merged funding | 1161 | first 2025-09-30 16:00, last 2026-10-22 08:00 UTC |

No missing execution or mark candle in the tested interval, and all 2316 required research cutoffs pass the original quality gate. All 1160 observed consecutive funding gaps in this particular dataset are 8h; this is observed evidence, not the default schedule for all history. As before, a vendor itself omitting a shorter-interval record may not be detectable if the remaining gap is within the supported 8h bound. No current OI, book or forecast funding is substituted.

Official archives are typically released T+2 and may have backfill gaps. Exact original publication times are UNKNOWN. The archive is ex-post evidence, not assumed downloadable at each event. Signals assume the underlying closed market bar was observable at close with zero dissemination latency; this does not certify actual fill availability. Funding is a settled outcome, never a feature for selecting earlier entries.

## Funding and valuation limits

Held positions use the historical hourly MARK open at the actual funding event. Missing mark evidence cannot fall back to current mark or trade open. This improves the old TRADE-open proxy but does not establish the exact mark used by OKX at settlement. Nineteen held-position settlement events have total cashflow +0.298015780225 USDT (old trade-open proxy +0.297994013780). Neither is an account-bill reconstruction.

Taker 5bps remains a scenario assumption, not a historical VIP fee tier; exchange fee rounding is not reconstructed. Current contract multiplier, tick, lot and minimum are explicitly unverified historical assumptions. Full-source observed contract/base volume consistency is checked, but that alone does not prove historical price/quantity constraints. The command requires acknowledgement. No leverage, margin, maintenance tiers or liquidation safety is modeled.

Equity retains the legacy ledger's hourly TRADE-close unrealized valuation, with historical mark used for funding. Maximum drawdown therefore is not exact exchange mark/margin equity or an intrabar worst drawdown.

## Actual continuous results

14 trades (2 LONG, 12 SHORT); 4 wins, 10 losses. Initial/final equity 10000 / 9978.035169965 USDT. Net -21.964830035 USDT (-0.2196483%). Profit Factor 0.8736557; win rate 28.5714%; average win/loss +37.9710393 / -17.3848987 USDT; hourly-equity maximum drawdown 1.3777781%.

Fees 13.943115815 USDT, net funding receipt +0.298015780225 USDT, adverse slippage/tick cost 5.59425 USDT embedded in fills and NOT deducted twice. All trades pass independent entry confirmation, next-open, lot/tick, exit, fee, historical funding and cash reconciliation audit.

Signal funnel: 2316 valid decisions; 49 LONG and 230 SHORT candidates. 137 candidates skipped due to an active hypothesis/position. 142 start breakout waiting, 37 confirm breakout, 14 confirm retest and fill; 117 expire and 11 invalidate. No resonance at 2036 decisions and one invalid price geometry explain the no-candidate cases. MIXED daily/hourly conditions and the later confirmation window explain scarce trades; rules were not altered to manufacture more fills.

The previous inspected 2026-07-24..2026-10-22 and all newly inspected historical dates are DEVELOPMENT, not pristine out-of-sample evidence. Fourteen conditional trades do not establish an edge. The computed net result is negative.

## Continuous/recovery checks

Real input replayed in 1-hour, 7-hour and 14-day partitions gives identical metrics, events, trades and equity. Full-history versus cropped-prewarm real decisions/trades/equity also match. Synthetic tests place both a position and pending signal across the actual 14-day boundary, including funding, to verify no forced exit/reset/double fees.

Raw HTTP pages and ZIPs are cached atomically. A prepared source is saved before long computation. Resume is deterministic replay from cached frozen input, not a per-candle mutable state checkpoint. Completed result files are content addressed, atomically committed and never append-accounted. No historical report is overwritten.

## PowerShell

```powershell
cd D:\_01trading_agents
.\.venv\Scripts\python.exe -u -m tradingagents.okx_continuous --start 2025-10-01T00:00:00Z --end 2026-10-22T00:00:00Z --assume-current-contract-spec --cache "$env:USERPROFILE\.tradingagents\logs\okx_diagnostics\public_cache"
```

Offline replay: replace `--cache ...` with `--input <run folder>\inputs.json` (or the prepared source). `--processing-days` changes processing only, not economic identity. Existing D.1 and D.2 commands remain available. This tool bounds requests at 731 days; only the 386-day dataset above was actually validated here, not all exchange history.

Outputs: inputs.json/result.json, trades.csv/events.csv/equity.csv, coverage.json, validation_protocol.json and report.md. The final inspected run also contains verification_zh.md and partition_audit.json.

## Future chronological protocol

Protected future holdout: [2026-10-24, 2027-01-24) UTC; NOT RUN. D.3 network and replay interfaces refuse development windows after 2026-10-22. Freeze source/config/costs before opening any holdout, evaluate once after data release, preserve causal warmup and continuous state rather than forcing split exits, separate crossing-boundary attribution, and require a new unseen interval after any strategy change. No task/reminder/automatic evaluation was scheduled.

Before claiming reliable executable historical validation, obtain historical contract parameter versions, exact funding mark/fee evidence and publication-latency evidence. Later Reflection may explain isolated simulation-only known outcomes; it must not optimize rules or write simulated returns into real trading/stock Memory. D.3 stops here.
