# Explicit admission policy in the guarded research entry

Scope: local deterministic implementation only, based on 373db6731d811aa12bb5f6325a5d1645a38a5ba5, on the existing codex/f1-revalidation-admission-oi-diagnostics branch. No branch/commit/push or deployment. Existing F1/F2.1 qualification and F3 entry rules are unchanged.

## Configuration transport

The new field is `forward_admission_policy`. Only `strict-v1` (default) and `forward-registration-revalidation-v1` are accepted.

1. The persistent budget `Policy` validates the explicit choice and binds it into the existing immutable policy journal/fingerprint. No token/call/HTTP/time/resource limits change.
2. `guarded_research.watchlist` passes it to the validated native `Watchlist`.
3. The native sealed task includes the field in `config` and `config_hash`; `command_for` emits `--forward-admission-policy VALUE` to the original CLI.
4. The worker checks the task configuration hash before dispatch and rejects a CLI/F1 intent policy that conflicts with the frozen task. The receipt includes the selected policy and config hash.
5. The CLI accepts a finite enum, defaults to strict, rejects conflicting preconfigured policy, and requires an actual trade-plan/F1 research invocation for the extension. It explicitly includes the value in the configuration passed to the existing graph and `Registry.begin`; no environment override, global default mutation, or test-only policy injection is used.
6. Existing F1 SAFE_CONFIG already seals this field in the prospective intent/registration. The existing Registry generates the versioned proof for a qualified OPEN only. The registration summary records the selected policy.
7. Existing F2.1 recomputes that SAME archived proof and checks the CURRENT observation window. There is no separate F2 opt-in switch to invent and no automatic F2 launch.

Illustrative configuration fragment for a FUTURE separately approved run:

```json
{"enabled": false, "accepted_post_response_exposure": false,
 "forward_admission_policy": "forward-registration-revalidation-v1",
 "optional_source_policy": "official-macro-403-v1",
 "execution_enabled": false, "allow_new_risk": false, "simulation_admitted": false}
```

The fragment is not a complete run policy, approval or initialization command. Live research must continue through `tradingagents.guarded_research`; direct CLI availability does not mean old unguarded commands receive a global budget. No new ledger is initialized here.

## Compatibility and failure boundaries

Absent fields in new policy/watchlist inputs use strict behavior. Existing sealed archives are not reinterpreted or migrated. WAIT, invalid original evidence, invalid/missing/expired/identity-mismatched proof and unrelated stale evidence retain their previous rejection behavior. Independent revalidation is not a repair of invalid originals or a TTL extension. F3 still requires its own fresh pre-entry evidence and independent paper admission.

New schema/default example files contain the strict choice and keep enabled/accepted exposure disabled. This changes configuration fingerprints for newly constructed policies/tasks. Existing code/implementation mismatch checks remain intact: do not reuse, migrate or reset any old production ledger.

The implementation-hash algorithm and its four inputs are unchanged, but safety_budget.py, guarded_research.py and auto_research.py content changes mean the implementation fingerprint CHANGES. auto_research CODE_HASH also changes. Working-tree fingerprints are engineering evidence only; after a reviewed commit, measure the actual Linux checkout independently, including platform EOL checks. Do not reuse the previous approved 73a3... baseline for the new code or claim the new Linux version has been accepted.

## Test boundaries

All new tests use frozen OHLCV/derivatives and the existing deterministic PlanModel/graph. Requests are blocked; HTTPX can only reach an in-memory MockTransport. No real model, business HTTP or production budget/authorization is used.

Two complementary integration paths are explicit:
- Original CLI + real graph/final gate/report writer + native prospective-shaped F1 + F2.1, entirely under pytest FIXTURE paths. The model selects OPEN/WAIT deterministically and a frozen clock crosses 300 seconds before the final gate. Only the CLI parameter supplies the admission policy. This verifies actual OPEN proof generation, sealing/loading and AWAITING_F2 without manufacturing a real forward signal.
- Global guarded worker + original auto worker + actual CLI and scripted models/MockTransport. Native FIXTURE semantics intentionally discard prospective intent and produce TEST_REPLAY, not AWAITING_F2. Policy transport is audited in the task/receipt. This test must not weaken that namespace protection to claim production admission.

Additional tests cover strict/default vs explicit extension, enum/type errors, conflicting configuration, changed sealed task hash, frozen-slot config conflict, WAIT, current F2 expiry and F3 proof rejection. Existing 21 admission/OI tests retain invalid originals, missing/expired/bad proofs, identities and immutable old archives. Budget, authentication and failure guards are exercised by unchanged related suites.

## Future acceptance

Before a real new research window: review/commit separately, deploy a new read-only Linux checkout, re-measure the new guard hash, validate schemas/defaults and exact CLI transport under Linux, retain the Reviewed baseline failure, validate one-use claims/resource/time limits in isolation, and seek explicit paid-run authorization. No new real OPEN candidate or live F1/F2 admission has been proven by these fixtures. Costs/remaining quota remain UNKNOWN; token thresholds remain soft.

Final acceptance results and file inventory follow.

## Change inventory and fingerprints

Ten files in this change:
- cli/main.py — explicit finite CLI option, default/conflict checks, report-setting transport and runtime policy-drift rejection.
- tradingagents/safety_budget.py — one finite, default-strict Policy field; no budget thresholds or guard algorithm change.
- tradingagents/guarded_research.py — pass that field to native Watchlist.
- tradingagents/auto_research.py — field, CLI argument, frozen-config hash/conflict checks and audit summaries.
- docs/d4b_safety_budget.json and docs/d4b_safety_budget.schema.json — matching strict default/example/schema.
- docs/auto_research_r11.json and docs/auto_research_r11.schema.json — matching strict default/example/schema.
- tests/test_admission_policy_entry.py — new offline entry/transport/qualification tests.
- docs/admission_policy_entry.md — this review.

No changes to registration_revalidation.py, forward_validation.py, observation_eligibility.py, forward_paper.py, okx_derivatives.py, perpetual_plan.py or plan_lifecycle.py. Their normalized bytes match the fixed baseline. Existing tests were not edited.

Windows working-tree guard hash: `c3761819262cb2d8c47a505348cd0af9524dde9a664dc1a2572adfec89669d87`.
LF-normalized candidate guard hash (prediction only, NOT Linux accepted): `2a8fa2760267b68c66ca6797c7d6a0b2afa00b8c242406c6c2f64dc4437f5cf6`.
Old Git-blob/Linux baseline: `73a3c2e3132d798e69fbf570e821ab3f20ee9807d231f3aae00150be893b39e6`.
Raw implementation_hash algorithm verified AST-identical; no normalization was added to it.

Default Policy config hash: `420ee0c426c42df947e131b7471e47247492a3bfd25d03c0629695dbad363a5a`.
Identical defaults with ONLY the new policy selected: `bed84dcc4c18dd6053c4d97710f324082f9ff6649205ada7d28526940e2bec85`.
These illustrative config hashes cover default budgets, not a newly approved research window. Actual future complete config must be independently sealed. Implementation/schema changes require a new reviewed commit and independent new ledger after approval; no old budget is compatible/migrated.


## Final local acceptance

- New entry tests: **35 passed**, including actual CLI + scripted native graph, report writing, OPEN proof generation, sealed F1 loading, F2.1 qualification, budget-worker/CLI transport and runtime policy drift.
- Entire executed related suite: **618 passed, 1 existing failure, 0 skipped**, 281.71 seconds. This is related regression, NOT full-project zero-failure acceptance.
- Existing failure preserved unchanged: tests/test_opening_observation_f31.py:232, test_init_is_opt_in_and_no_production_activation_of_uncommitted_code, expected Reviewed ValueError but DID NOT RAISE. No exclusion, skip, marker or assertion modification.
- Related suites: new entry, prior 21 registration/OI tests, guarded_research, safety_budget, auto_research, automation, authentication renewal, CPI isolation, official macro isolation, forward_validation, observation_eligibility, perpetual_plan, OKX derivatives/multi-timeframe/research, forward_tracking, forward_paper and the unchanged Reviewed case.
- Initial expanded run was 615 passed / 3 failed. Besides Reviewed, failures were the missing default example field and Windows MAX_PATH in an unchanged F2 test. The example was corrected and the final run used a shorter independent basetemp; no test rule was changed or skipped. Earlier scripted transport setup/usage/duplicate-response-ID mistakes were corrected in test substitutes, never by relaxing guards.
- git diff --check passed. Branch unchanged; no staged files, commit or push. The six pre-existing untracked files (AGENTS.md plus five others) remain outside this change.
- Evidence: C:/Users/jerry_su/.tradingagents/logs/admission_policy_entry_20261031/final_regression.log, final_regression.xml, summary.json and fingerprints.json. The fixtures use C:/Users/jerry_su/.tradingagents/logs/ae31; no formal ledger or old production archive is involved.
- Recommendation: **READY_FOR_COMMIT**, conditional local engineering acceptance with the known baseline failure retained. No paid run, remote deployment, new real candidate, live revalidation or paper execution is approved/proven by this result.
