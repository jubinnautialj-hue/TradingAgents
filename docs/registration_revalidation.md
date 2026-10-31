# Registration-time revalidation admission extension

Version: `forward-registration-revalidation-v1`. Default OFF.

Enable only before a NEW native F1 intent via configuration:

```json
{"forward_admission_policy": "forward-registration-revalidation-v1"}
```

Absent / `strict-v1` retains legacy behavior. There is no new CLI auto-enable, migration, network request, model call or budget change. Unknown policy values fail closed. Existing registrations return their sealed original record; retrying with a new policy cannot upgrade them.

## Contract

The immutable `forward-f1-v1` envelope and existing protocol files are unchanged. New registrations optionally archive an explicit extension in `quality_evidence.registration_revalidation`. Its version, policy hash and proof hash distinguish it from legacy records. F2.1 verifies the identical proof by recomputing the pure admission function. Its existing audit registration hash transitively binds the extension. Old observation chains and ledger formats are not rewritten.

Schema: `registration_revalidation.schema.json`. Fields bind research ID, original snapshot, instrument, deterministic LONG/SHORT candidate ID, complete result hash, frozen candidate conditions hash, revalidation hash, actual registration/check/expiry times, extension version/hash, and three literal false safety flags. No credentials, prompts or responses are stored in the proof.

Positive qualification requires all of:

- New prospective COMPLETED / AVAILABLE OPEN with native plan_valid, original core validity/identity and original strategy/cost/clock checks.
- Original derivatives belong to this snapshot/instrument/cutoff, all components originally VALID and complete; the six critical components were acquired while valid, before cutoff, with at most the existing 300-second TTL. PARTIAL/unavailable/invalid originals are deliberately not repaired.
- Independent revalidation is VALID, matches original snapshot and instrument and validation hash, and passes the existing specification/price/OI/funding/liquidity comparison. Frozen canonical entry, stop, target and signal clock are unchanged.
- Original cutoff <= check <= research completion <= registration < proof expiry. This extension caps registration proof age at 60 seconds and never extends original research/result expiry.
- Only reproducible natural ageing inside the exact derivative registration view or derivative report view is allowed. Other STALE/RESEARCH_INVALID evidence, including optional evidence, still rejects. Original state and diagnostics remain byte-preserved.

Missing/failed/expired proof in opt-in mode fails closed. WAIT and originally invalid evidence cannot be upgraded. F1 sealed loading validates the extension too; F2 cannot accept tampered or unsupported proof.

## Observation versus entry time

The proof must be CURRENT when F1 registers it. After registration it is a historical attestation that the research qualified then; it is not a renewed live-data lease. F2.1 still verifies original identities, full frozen coverage, prospective clocks, plan validity and the CURRENT observation/hypothesis window. It does not claim this historical proof is still live at a future hourly close. Otherwise a 60-second registration proof would make a one-hour observation impossible.

F3 implementation and protocols are unchanged. F3 still needs its verified condition, independent paper admission, own fresh pre-entry revalidation, future opening and original timing rules. This registration proof cannot satisfy `revalidation_ready` or become a fill. execution_enabled, allow_new_risk and simulation_admitted remain False.

This explicit observation-time interpretation is part of the extension and should be reviewed before commit; no existing F1/F2 record is reinterpreted merely because new code is installed.

## OI-only diagnostic

Version `oi-history-diagnostic-v1` is attached only when original OI-history capture fails. Existing request, pagination, parsing, quality rules, `DATA_UNAVAILABLE` status and sanitized issue string remain. Successful output is unchanged. Other sources do not receive this diagnostic.

Bounded fields: stage REQUEST/PAGINATION/PARSE_ROWS/POST_VALIDATE, page index (max original page count), integer cursor_ms, accumulated/page record counts, up to eight unique row widths, min/max page timestamp when available, static error_code, whitelisted exception type, module-local function/line or EXTERNAL. No full HTTP body, arbitrary exception message, headers, auth, model text or environment is retained.

Example: `{ "version":"oi-history-diagnostic-v1", "stage":"PAGINATION", "page":0, "cursor_ms":1793424841758, "records":0, "page_records":2, "error_code":"PAGINATION_NONPROGRESS", "exception_type":"ValueError", "location":{"function":"history_pages","line":0} }`. The line is illustrative, not historical evidence. Third-round historical ValueError remains UNKNOWN; nothing is retroactively invented.

## Validation and limits

Frozen fixture matrix covers fresh proof, natural ageing + valid proof, absent/failed/expired proof, invalid original, WAIT, snapshot/instrument/time/hash/price mismatch, schema shape, proof tampering, immutable legacy retry, unrelated stale source rejection, F3 proof isolation and OI static-code/redaction cases.

Tests are offline pytest fixtures and scripted substitutes. No real study, business HTTP, private interface, production budget, old archive or Vultr change is involved. Original Reviewed baseline test is run unchanged and its failure is retained in the acceptance results. This is related regression, not a full-project zero-failure claim.

Actual changed/new files: tradingagents/forward_validation.py; tradingagents/observation_eligibility.py; tradingagents/registration_revalidation.py; tradingagents/dataflows/vendors/okx_derivatives.py; tests/test_registration_revalidation.py; docs/registration_revalidation.schema.json; docs/registration_revalidation.md.

Before production: independent review/commit, new isolated Linux validation and explicit prospective-run approval. No scheduler, fourth study, old-ledger reset or automatic rollout is authorized.

### Local acceptance results

- Branch: `codex/f1-revalidation-admission-oi-diagnostics`, parent `8c9cc6ff3798df81763b718df9f1391b22f512ae`.
- New focused tests: **21 passed** (included in the related suite below).
- F1/F2.1, plans, F2/F3 and unchanged Reviewed test: **188 passed, 1 existing failure**, 64.85s.
- OKX derivatives, multi-timeframe and research data regressions: **92 passed**.
- Combined executed suites: **280 passed, 1 existing failure**. Not a full-project regression claim.
- Existing failure retained: `tests/test_opening_observation_f31.py:232`, `test_init_is_opt_in_and_no_production_activation_of_uncommitted_code`, expected Reviewed ValueError but DID NOT RAISE. No change/skip/hash bypass.
- `git diff --check` passed. No commit, push, Vultr deployment, model or business HTTP.
- Evidence logs: `C:/Users/jerry_su/.tradingagents/logs/f1_ttl_oi_review_20261031/final_regression.log`, `final_regression.xml`, `data_regression.log`.
- Engineering recommendation: **READY_FOR_COMMIT, conditional local acceptance**. Review the documented registration-time attestation semantics before accepting; real/Linux candidate admission and execution remain unverified and disabled.
