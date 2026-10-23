# D.2: signal diagnostics and expanded historical scenarios

Research only. execution_enabled=False. No new agent, private OKX calls, strategy tuning, stock backtest replacement, Memory injection or live execution.

## Files and reuse

- `tradingagents/okx_diagnostics.py`: independent diagnostics, public page cache, funding pagination evidence, 14-day batch orchestration, trade audit, reports and CLI.
- `tradingagents/okx_backtest.py`: optional diagnostics hooks, snapshot attribution in events and opt-in observed-funding validation. Default D.1 declared-interval mode remains supported. No level, direction, confirmation, fill, fee or slippage rule was loosened.
- `tests/test_okx_diagnostics.py`: diagnostics/schedule/cache/audit/invalid-data tests.

Reuses C.1 evaluate_signal/prepare_historical_signal, Stage C candidates, multi-timeframe indicators/confluence, existing candle quality gates and the D.1 ledger. HH/HL and LH/LL remain descriptive observations, not additional filters. No alternate-filter returns are reported.

## Frozen baseline

BTC-USDT-SWAP, 1h execution, 4h decision spacing, 1d/4h/1h strict directional resonance, closed breakout then later retest. Confirmation window 6 bars; maximum holding 12 bars. Target 3 x 4h ATR; stop buffer 0.25 x 1h ATR. Warmup minimum 250, up to 400 bars per cutoff/period. Initial scenario equity 10000 USDT, fixed exposure 1000 USDT, explicit Taker 5 bps per leg, adverse slippage 2 bps per fill plus adverse tick rounding. No model selection, optimization or leverage.

## Original zero-trade cause

Original UTC 2026-10-12 through 2026-10-22, 60 decisions:

| Period | UP | MIXED | DOWN |
|---|---:|---:|---:|
| 1d | 60 | 0 | 0 |
| 4h | 0 | 39 | 21 |
| 1h | 6 | 36 | 18 |

Since daily UP never agrees with 4h UP, three-period resonance cannot occur. The original zero-trade run is primarily a trend-filter result, not a failed broker connection, missing account, or cost deduction issue. MIXED follows the original close/SMA50/SMA200 ordering and five-bar SMA50 slope rules; it is not secretly treated as directional.

## Funnel interpretation

Per decision: original data quality, each period's indicators/close/slope/structure/pivots, independent LONG/SHORT checks, failed conditions, confluence and candidate generation. Event replay adds pending-breakout, breakout confirmation, retest confirmation, expiry/invalidation, rejection/active-skip, and simulated entry. Breakout/confirmation is future closed-bar replay, NOT inferred from the 1h directional label at cutoff.

Side tables give independent pass counts, sequential pass counts, unconditional denominators and conditional pass rates. Candidate existence and actual scheduler acceptance are separate: a candidate may be skipped because another hypothesis/position is active. NO_CANDIDATE, EXPIRED, INVALIDATED, ENTRY_REJECTED, SKIPPED_ACTIVE, DATA_INVALID and FILLED are distinct. Expired hypotheses distinguish no breakout from no retest. Whole invalid batches retain each requested decision in the denominator and have metrics=null, never fake zero return.

## Historical coverage and funding

Public history GET pages are keyed by official base URL, endpoint and exact parameters. Successful immutable pages are atomically cached with payload hashes; interrupted downloads replay cached pages and continue at the same backward cursor. Corruption fails closed; empty responses are not frozen. Dynamic time/instrument metadata is fetched separately. Use a NEW cache directory for a deliberate historical revision audit; existing archives are not overwritten.

Observed-event mode requires raw funding pages, hash/strict cursor/duplicate checks, actual realizedRate parsing, and bracketing events. It supports changing 1/2/4/8-hour intervals and never invents eight-hour settlements. Subhour settlements or a gap above the supported 8-hour safety bound block the hourly simulation. A vendor omitting a shorter-interval record may be undetectable if the remaining gap still falls within that bound; the API page chain is evidence, not a guarantee that the vendor archive is infallible. Actual observed intervals are reported.

The fetched 90-day example contains 2563 hourly, 943 four-hour and 493 UTC daily confirmed candles; oldest opens are respectively 2026-07-07 06:00, 2026-05-17 20:00 and 2025-06-16 00:00 UTC. Terminal hourly verification closes 2026-10-22 01:00; only the predeclared window-end OPEN affects the ledger. Warmup-complete candle window starts 2026-07-23 22:00. Funding contains 282 realized records from 2026-07-20 16:00 through 2026-10-22 08:00; all 281 observed gaps in this actual sample happen to be 8 hours. This is an observation, not a global historical schedule assumption.

Common conditional simulation window: UTC 2026-07-24 00:00 through 2026-10-22 00:00. Current contract settings remain UNVERIFIED historical assumptions in each independent <=14-day scenario. Historical Taker fees are also not verified. Thus there is no claim of a fully historically verified executable window. No current OI/orderbook or forecast funding is substituted.

## Actual sample results

540 valid decision points. Sequential LONG funnel: 540 -> 258 daily-UP -> 87 matching 4h structure/direction -> 41 matching 1h -> 41 resonance -> 41 candidates. No SHORT candidates because daily DOWN never occurs. Of 41 candidates, 21 are skipped while another hypothesis/position is active; 20 enter breakout waiting, 6 confirm breakout, 2 confirm retest and fill. Of 20 active hypotheses, 16 expire and 2 invalidate. There are 499 no-candidate decisions but only 497 actual WAIT events: two no-candidate points occur while the scheduler is active. Two additional active-skip events explain the difference.

Breakout success: 6/20=30%; retest success: 2/6=33.33%; fill after confirmation: 2/2=100%. This last figure is a model assumption result, NOT evidence of real liquidity.

Seven independent batches reset cash and pending hypotheses; no compounded full-window portfolio return is reported. Candidate counts by batch start: Jul24=0, Aug07=0, Aug21=0, Sep04=0, Sep18=31, Oct02=10, Oct16=0. Only Sep18-Oct02 has trades: two LONGs, both maximum-holding exits, net -13.610011307 USDT, win rate 0/2, Profit Factor 0, scenario return -0.1361001%, observed hourly-equity max drawdown 0.1902465%. Other batches have zero trades and undefined win rates.

| UTC entry | UTC exit | Side | Net PnL USDT |
|---|---|---|---:|
| 2026-09-19 01:00 | 2026-09-19 13:00 | LONG | -5.720542912 |
| 2026-09-21 23:00 | 2026-09-22 11:00 | LONG | -7.889468395 |

Fee sum 1.984859015 USDT, funding cashflow -0.296642292 USDT across three settlements, slippage 0.795690 USDT embedded in prices (NOT deducted twice). Funding notional still uses settlement-hour trade-open price proxy, not historical mark. Each trade is independently audited for confirmation timing, next-open fill, tick/lot conversion, exit priority, fees, funding and cash reconciliation before a batch is saved as valid.

During expansion from the 82-day cache to 90 days, 41 historical page requests hit cache and only 3 new historical pages were fetched. Metadata/time requests are separate. Complete source.json supports zero-network replay. Identical source/code produces identical run identities and no additional trade accounting. Batch results are written before aggregate completion. Safe reruns recompute verification and reuse identical completed records without appending accounting; there is no per-candle computational checkpoint.

## PowerShell

```powershell
cd D:\_01trading_agents
.\.venv\Scripts\python.exe -m tradingagents.okx_diagnostics --start 2026-07-24T00:00:00Z --end 2026-10-22T00:00:00Z --assume-current-contract-spec
```

For fully offline replay, add `--input <run directory>\source.json`. The original D.1 command remains available. The new CLI freezes fees/slippage and does not expose a parameter search. Upper request bound is a 90-day TOOL safety limit, not a claim about the exchange's maximum archive depth.

Outputs: report.md, diagnostics.json, decisions.csv and source.json; each batch separately contains result.json, inputs.json, trades.csv, events.csv and equity.csv. Raw page cache is separate. CLI returns exit code 2 for invalid/partial validation instead of silently declaring a completed zero-return backtest.

## Next stage boundary

Only two losing LONG samples, no historical SHORT fills, and only a subset of market regimes: statistically inadequate for an edge or robust out-of-sample validation claim. The pipeline can support a separately frozen unseen-date test once adequate historical specifications, funding price evidence and data coverage exist. Current explored dates are diagnostic/in-sample evidence, not a pristine holdout.

Future Reflection should consume isolated simulation-only records keyed by strategy/source/snapshot/decision/result-known time, keep data faults, no-signal cases, confirmation failures and execution assumptions separate, and use D.1 feedback(as_of_ms) known-outcome filtering. Do not automatically inject these two losses into stock memory or alter strategy rules. No integration or optimization is enabled in D.2.

## Validation

Final selected regression suite: 348 passed, including Stage A/B/C/C.1/D.1, stock graph/backtest and Memory. The D.2-specific suite has 23 tests; D.1 ledger has 26. A real two-trade batch was independently recomputed offline and saved under the same content-addressed directory, verifying no duplicate accounting. Original reports remain untouched.
