"""D.5 causal, simulation-only feedback import and opt-in reflection CLI."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from langchain_core.messages import AIMessage

from tradingagents import okx_agent_comparison as research
from tradingagents import okx_traded_comparison as experiment
from tradingagents import okx_backtest as bt, okx_cost_audit as costs
from tradingagents.llm_clients.recovery import digest
from tradingagents.memory.okx_simulation import (OKXSimulationMemory, SimulationFeedback, as_ms,
                                               settle_simulation_feedback, utc_ms)
from tradingagents.memory.reflection import Reflector


def build_feedback(case, decision, trade, events, *, source_hashes, strategy_version,
                   strategy_fingerprint, model_version, source_available_at_ms, recorded_at_ms,
                   limitations=(), window_end_ms=None):
    """Associate immutable sources; outcome is the BASELINE rule simulation only."""
    sid, cutoff = decision["snapshot_id"], decision["cutoff_ms"]
    if case["candidate_id"] != sid or as_ms(case["cutoff_utc"]) != cutoff:
        raise ValueError("Research/candidate cutoff mismatch")
    experiment.validate_case(case, sid)
    if trade:
        candidate = decision.get("candidate")
        if not candidate or trade["direction"] != candidate["direction"] or trade["stop"] != candidate["stop"]["price"] or trade["target"] != candidate["target"]["price"]:
            raise ValueError("Trade geometry/ownership mismatch")
        if trade["snapshot_id"] != sid:
            raise ValueError("Foreign simulated trade")
        # Reuse D.1 point-in-time outcome selector rather than a parallel gate.
        known = trade["resolved_at_ms"]
        if not bt.feedback({"strategy_version": strategy_version, "trades": [trade]}, known):
            raise ValueError("Unresolved simulated trade")
    else:
        terminal = [e for e in events if e["type"] in {"EXPIRED", "INVALIDATED", "ENTRY_REJECTED", "UNFILLED_WINDOW_END", "WAIT", "DECISION_SKIPPED"}]
        if not terminal:
            raise ValueError("No trade outcome cannot be inferred without a terminal lifecycle event")
        known = max(e["ts"] for e in terminal)
    known = max([known, cutoff, *[e["ts"] for e in events]])
    if window_end_ms is not None and known > window_end_ms:
        raise ValueError("Outcome outside audited simulation window")
    original_status = case["status"]
    status = {"COMPLETED": "COMPLETED", "MODEL_FAILED": "MODEL_FAILED", "DATA_ERROR": "DATA_ERROR",
              "DATA_INVALID": "DATA_ERROR", "UNSTUDIED": "UNSTUDIED"}.get(original_status, "INCOMPLETE_RESEARCH")
    outcome = ("PROFIT" if trade["net_pnl"] > 0 else "LOSS" if trade["net_pnl"] < 0 else "FLAT") if trade else ("WAIT" if case.get("decision") == "WAIT" else "UNFILLED")
    category = "ANALYSIS_REVIEW_REQUIRED_NOT_PROVEN_CAUSE" if trade else "NO_FILL_NOT_ZERO_PROFIT"
    if status != "COMPLETED":
        outcome = "DATA_ERROR" if status == "DATA_ERROR" else "UNSTUDIED" if status == "UNSTUDIED" else "MODEL_FAILURE"
        category = case.get("failure", {}).get("kind", status)
    elif case["decision"] == "ABSTAIN":
        category = "ABSTENTION_NOT_SUCCESSFUL_AVOIDANCE"
    elif case["decision"] == "REJECT":
        category = "COUNTERFACTUAL_BASELINE_NOT_AGENT_PORTFOLIO"
    # These are retrospective model calls. Never backdate them to market cutoff.
    call_times = [int(datetime.fromisoformat(c["started_at_utc"]).timestamp()*1000 + c.get("elapsed_seconds", 0)*1000)
                  for c in case.get("calls", []) if c.get("started_at_utc")]
    research_recorded = max([cutoff, source_available_at_ms if not call_times else 0, *call_times])
    available = max(research_recorded, known, source_available_at_ms, recorded_at_ms)
    feedback_id = digest({"candidate": sid, "sources": source_hashes})
    return SimulationFeedback(feedback_id=feedback_id, candidate_id=sid,
        research_id=digest(case), snapshot_id=sid, research_status=status,
        decision=case.get("decision") if status == "COMPLETED" else None,
        judgment=case.get("choice", {}).get("rationale", "No complete model decision; not an abstention or rejection"),
        outcome_status=outcome, baseline_trade=trade, state_machine=events, error_category=category,
        model_version=model_version, strategy_version=strategy_version, strategy_fingerprint=strategy_fingerprint,
        source_hashes=source_hashes, evidence_limitations=list(limitations)+[
            "Baseline rule outcome is not an Agent portfolio return; filtering requires continuous replay",
            "Archive publication timing unknown; source availability conservatively begins at D.5 import",
            "Retrospective LLM knowledge contamination cannot be eliminated"],
        research_cutoff_ms=cutoff, research_recorded_at_ms=research_recorded, outcome_known_at_ms=known,
        source_available_at_ms=source_available_at_ms, recorded_at_ms=recorded_at_ms, available_at_ms=available)


def import_experiment(source, run, memory, *, clock=utc_ms):
    """Read sealed decisions first, then join the unchanged audited rule ledger."""
    protocol, _ = experiment.verify_frozen(source, check_code=False)
    cases = experiment.verified_cases(source, protocol)
    if any(as_ms(case["cutoff_utc"]) >= research.continuous.DEVELOPMENT_END for case in cases):
        raise ValueError("Protected future holdout is not a D.5 development feedback source")
    inputs, baseline = experiment.read(run/"inputs.json"), experiment.read(run/"result.json")
    if bt.digest(inputs) != protocol["source_digest"] or bt.digest(baseline) != protocol["baseline_result_digest"]:
        raise ValueError("Wrong baseline research/outcome source")
    proof = costs.audit(inputs, baseline)
    decisions, lineage = experiment.locate_candidates(inputs, baseline, expected=len(cases))
    if bt.digest(lineage) != protocol["lineage_hash"] or proof["strategy_fingerprint"] != protocol["strategy_fingerprint"]:
        raise ValueError("Frozen lifecycle/strategy lineage changed")
    if [d["snapshot_id"] for d in decisions] != protocol["candidate_ids"]:
        raise ValueError("Cohort/ownership changed")
    if protocol.get("execution_enabled") is not False or baseline.get("execution_enabled") is not False:
        raise ValueError("Only disabled simulation inputs accepted")
    before = {"source": bt.digest(protocol), "ledger": bt.digest(baseline), "inputs": bt.digest(inputs),
              "strategy_code": bt.digest(protocol["code"])}
    by_id = {d["snapshot_id"]: d for d in decisions}
    trades = {t["snapshot_id"]: t for t in baseline["trades"]}
    now = clock()
    model = json.dumps({k: v for k, v in protocol["model_config"].items()
                        if k in {"llm_provider", "quick_think_llm", "deep_think_llm"}}, sort_keys=True)
    imported = []
    for case in cases:
        sid = case["candidate_id"]
        record = build_feedback(case, by_id[sid], trades.get(sid),
            [e for e in baseline["events"] if e.get("snapshot_id") == sid],
            source_hashes={"protocol": before["source"], "inputs": before["inputs"],
                           "ledger": before["ledger"], "case": bt.digest(case),
                           "strategy_code": before["strategy_code"]},
            strategy_version=baseline["strategy_version"], strategy_fingerprint=protocol["strategy_fingerprint"],
            model_version=model, source_available_at_ms=now, recorded_at_ms=now,
            limitations=protocol["historical_limitations"], window_end_ms=inputs["end"])
        imported.append(memory.store_feedback(record))
    return imported


class DemonstrationModel:
    """Fixed mocked lesson for an explicit offline example; never model evidence."""
    def invoke(self, messages):
        record = json.loads(messages[-1][1])
        value = record["baseline_trade"]["net_pnl"] if record["baseline_trade"] else None
        return AIMessage(content=(f"MOCK demonstration: the original {record['decision']} judgment is linked to "
            f"a baseline simulated outcome of {value} USDT. A single case does not establish the reason for "
            "the outcome or a strategy advantage. Preserve the execution/data assumptions and review the "
            "recorded opposing evidence before drawing any lesson; do not change frozen rules."))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mock-reflection", action="store_true")
    parser.add_argument("--real-reflection", action="store_true")
    parser.add_argument("--as-of", help="UTC-aware ISO timestamp; default current UTC")
    parser.add_argument("--max-reflections", type=int, default=1)
    args = parser.parse_args()
    if args.mock_reflection and args.real_reflection:
        parser.error("Choose mock or real reflection")
    output = args.output.resolve()
    for source in (args.experiment.resolve(), args.run.resolve()):
        if output == source or source in output.parents or output in source.parents:
            raise ValueError("Output must be separate from historical sources")
    memory = OKXSimulationMemory(output)
    records = import_experiment(args.experiment, args.run, memory)
    cutoff = as_ms(args.as_of) if args.as_of else utc_ms()
    if cutoff > utc_ms():
        raise ValueError("Future feedback as-of forbidden")
    done = None
    if args.mock_reflection or args.real_reflection:
        if args.real_reflection:
            config = research.model_config()
            config["llm_max_retries"] = 0
            backend = research.create_tier_client(config, "quick").get_llm()
            model_version, mode = config["quick_think_llm"], "REAL_LLM"
        else:
            backend, model_version, mode = DemonstrationModel(), "FIXED_MOCK_NO_LLM", "MOCK"
        done = settle_simulation_feedback(memory, Reflector(backend), as_of=cutoff,
            model_version=model_version, generation_mode=mode, max_entries=args.max_reflections)
    print(json.dumps({"records": len(records), "research_statuses": dict(Counter(r.research_status for r in records)),
        "outcome_statuses": dict(Counter(r.outcome_status for r in records)), "output": str(output),
        "settled": done.settled if done else [], "failed": done.failed if done else [],
        "execution_enabled": False}, ensure_ascii=True))


if __name__ == "__main__":
    main()
