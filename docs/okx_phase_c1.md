# Stage C.1: research signal timing and historical interface

No execution, fills, ledger, account access or backtest metrics are implemented.

## Independent clocks

- `signal_timing.hypothesis_expires_ms`: observation hypothesis lifetime, counted from the first complete UTC hourly bar opening at/after research cutoff.
- `okx_plan_confirmation_bars`: 2..48 complete hourly bars (default 6). A partial hour containing cutoff cannot confirm the signal.
- `okx_derivatives_ttl_seconds`: existing live derivative TTL remains unchanged (default 300 seconds).
- `okx_entry_revalidation_ttl_seconds`: fresh evidence validation budget, default 60 seconds, bounded by derivative expiry and hypothesis expiry.
- Existing `expires_at_utc`, `research_expires_at_utc`, and `okx_plan_ttl_seconds` remain delivery/evidence validity deadlines. They do not extend automatically, and are not the hypothesis deadline. A stale report is never made a valid current trading plan by the longer observation clock.

## Deterministic replay

`evaluate_signal(candidate, timing, normalized_bars, as_of_ms)` is pure and replayable. State is recomputed from bars, not accepted from an LLM or persisted state override.

LONG: future confirmed close strictly above entry -> PENDING_RETEST. A later hourly bar with low <= entry and close > entry -> ENTRY_ELIGIBLE. SHORT reverses the inequalities. Retest may occur one or several bars later. The breakout bar cannot also confirm a retest. A close at/through the original stop invalidates the hypothesis. An explicit structural change invalidates immediately. Missing required quality or incomplete replay coverage produces WAIT. At the hypothesis deadline state is EXPIRED, including a confirmation exactly at that deadline.

All bars must be confirmed, aligned UTC hours, contiguous, finite, and closed no later than as_of. `next_tradable_ms` is the confirming bar CLOSE / next bar OPEN, never that bar's earlier open. It is a temporal lower bound, not a promised fill. A live caller must wait until fresh validation completes. No further bars are fetched or watched automatically.

`entry_evidence_ready` additionally requires existing drift/identity validation, validation AFTER signal confirmation, unexpired short validation TTL, and a newly verified full current market bundle. True remains a research evidence check, never execution authorization. Callers must retain all other plan/cost/position gates. A future stage must recompute prices/costs and check hypothesis structure against the new bundle.

## Historical preparation

`prepare_historical_signal(bundle, config, candidate_id, assumptions)` reuses existing candidates and existing bundle quality checking. Requires a validated historical 1h/4h/1d snapshot with shared cutoff, no future closes, no current derivative data, and explicit nonnegative fee/slippage assumptions. Optional historical dependencies must be VALID and present if required by the strategy.

It returns a simulation template, not a live `PerpetualResearchResult`: `plan_valid=False`, `allow_new_risk=False`, `execution_enabled=False`. Missing historical order book alone is acceptable for the explicitly labeled conservative candle simulation. A book-dependent strategy is blocked without historical book evidence. Current OI, order book and forecast rates cannot be substituted. The candidate is recomputed from the historical bundle, not copied from today's plan.

Assumptions: next-bar-open fill with adverse slippage; both legs explicit taker fees; ambiguous same-bar exits stop-first; realized funding only or block. These rules are declarations for stage D, NOT an implemented matching engine. Fees are scenario assumptions, not account fee history.

Historical contract parameter versions, maintenance margin tiers, liquidation rules, realized funding coverage through each holding interval, and actual simulated fills still need stage D validation. No historical performance is claimed.
