"""D.4.1 retrospective filled-opportunity experiment. Public history, no execution.

Preparation verifies lineage. Execution reads only frozen causal inputs, never
the original ledger. Analysis requires a sealed manifest of all case results.
"""
from __future__ import annotations
import argparse
import ast
import copy
import csv
import hashlib
import json
import os
import platform
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import urlsplit
from tradingagents import okx_agent_comparison as research
from tradingagents import okx_backtest as bt, okx_cost_audit as costs, okx_diagnostics as diagnostics
from tradingagents.agents.candidate_filter import validate_choice, PROMPT_VERSION
from tradingagents.dataflows.files import locked

VERSION = "okx-filled-opportunity-comparison-v1"
WORKERS = 2
UNCERTAIN_POLICY = "RULE_PASS_THROUGH_NOT_SUCCESSFUL_REJECTION"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def immutable(path, value):
    """Atomic completion, immutable final artifacts; campaign lock owns writers."""
    path = Path(path)
    content = json.dumps(value, indent=2, allow_nan=False)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError("Immutable artifact mismatch: "+path.name)
        return
    tmp = path.with_suffix(path.suffix+".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def locate_candidates(inputs, original, expected=14):
    decisions = original["decisions"]
    ids = [d["snapshot_id"] for d in decisions]
    if len(set(ids)) != len(ids):
        raise ValueError("Ambiguous original decision IDs")
    by_id = {d["snapshot_id"]: d for d in decisions if d.get("candidate")}
    trades = original["trades"]
    if len(trades) != expected or len({t["snapshot_id"] for t in trades}) != expected:
        raise ValueError("Unexpected or duplicate original trade membership")
    rows = {int(r[0]): r for r in inputs["data"]["candles"]["1h"]}
    config = bt.SimulationConfig.model_validate(inputs["config"])
    selected, lineage = [], []
    for trade in sorted(trades, key=lambda t: t["research_cutoff_ms"]):
        sid = trade["snapshot_id"]
        d = by_id.get(sid)
        if d is None or d["cutoff_ms"] != trade["research_cutoff_ms"] or d["candidate"]["direction"] != trade["direction"]:
            raise ValueError("Original trade/candidate ownership mismatch")
        if trade["stop"] != d["candidate"]["stop"]["price"] or trade["target"] != d["candidate"]["target"]["price"]:
            raise ValueError("Original candidate levels changed")
        confirmation = bt.normalized([{"ts": ts, "Open": r[1], "High": r[2], "Low": r[3], "Close": r[4]}
            for ts, r in sorted(rows.items()) if d["cutoff_ms"] <= ts < trade["entry_ms"]])
        signal = bt.evaluate_signal(d["candidate"], bt.timing(d["cutoff_ms"], {"okx_plan_confirmation_bars": config.confirmation_bars}),
                                    confirmation, trade["entry_ms"])
        if signal.state != "ENTRY_ELIGIBLE" or signal.next_tradable_ms != trade["entry_ms"]:
            raise ValueError("Original confirmation/entry linkage invalid")
        events = [e for e in original["events"] if e.get("snapshot_id") == sid]
        for kind, ts in (("PENDING_BREAKOUT", d["cutoff_ms"]), ("PENDING_RETEST", signal.breakout_confirmed_ms),
                         ("ENTRY_ELIGIBLE", signal.retest_confirmed_ms), ("ENTRY", trade["entry_ms"])):
            matching = [e for e in events if e["type"] == kind and e["ts"] == ts]
            if len(matching) != 1:
                raise ValueError("Missing/ambiguous lifecycle ownership: "+kind)
        entry = next(e for e in events if e["type"] == "ENTRY")
        if entry["contracts"] != trade["contracts"] or entry["price"] != trade["entry_price"]:
            raise ValueError("Original entry event/quantity mismatch")
        selected.append(d)
        # Future confirmation is ONLY in this separate audit, never model input.
        lineage.append({"candidate_id": sid, "cutoff_ms": d["cutoff_ms"], "direction": trade["direction"],
            "breakout_confirmed_ms": signal.breakout_confirmed_ms, "retest_confirmed_ms": signal.retest_confirmed_ms,
            "original_entry_ms": trade["entry_ms"], "status": "UNIQUE_CANDIDATE_AND_CLOSED_BAR_LINEAGE_VERIFIED",
            "model_input_exclusion": "All confirmation/entry details above are retrospective audit only"})
    return selected, lineage


def source_fingerprints():
    root = Path(__file__).parent
    files = research.code_fingerprint()
    extra = [Path(__file__), root/"okx_cost_audit.py", root/"okx_diagnostics.py",
             *root.glob("dataflows/vendors/okx*.py"), root/"dataflows/perpetual_plan.py", root/"dataflows/plan_lifecycle.py"]
    for p in sorted(set(extra)):
        files[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files


def safe_config(config):
    keys = ("llm_provider", "quick_think_provider", "deep_think_provider", "quick_think_llm", "deep_think_llm",
            "llm_max_retries", "max_tokens", "temperature", "openai_reasoning_effort", "output_language")
    result = {k: config.get(k) for k in keys}
    result["endpoint_host"] = urlsplit(config.get("backend_url") or "").hostname
    result["model_proxy_configured"] = bool(config.get("llm_proxy"))
    return result


def prepare(run, output):
    inputs, original = read(run/"inputs.json"), read(run/"result.json")
    proof = costs.audit(inputs, original)
    decisions, lineage = locate_candidates(inputs, original)
    if sum(bool(d.get("candidate")) for d in original["decisions"]) != 279:
        raise ValueError("Expected original 279-candidate development set")
    config = research.model_config()
    payloads = [research.case_input(inputs["data"], d, bt.SimulationConfig.model_validate(inputs["config"])) for d in decisions]
    protocol = {"version": VERSION, "selection": "ALL_14_ORIGINAL_FILLED_CANDIDATES_SORTED_BY_CUTOFF_NO_PNL_SELECTION",
        "scope": "RETROSPECTIVE_EXECUTION_CONDITIONED_DEVELOPMENT_SAMPLE_NOT_ALL_CANDIDATES_OR_OUT_OF_SAMPLE",
        "source_digest": bt.digest(inputs), "baseline_result_digest": bt.digest(original),
        "strategy_fingerprint": proof["strategy_fingerprint"], "simulation_config": inputs["config"],
        "candidate_ids": [p["candidate_id"] for p in payloads], "payload_hashes": [bt.digest(p) for p in payloads],
        "lineage_hash": bt.digest(lineage), "code": source_fingerprints(), "prompt_version": PROMPT_VERSION,
        "model_config": safe_config(config), "max_cases": 14, "max_requests": 126, "workers": WORKERS,
        "retry_policy": "ZERO; NO_RESEARCH_OF_STARTED_OR_TERMINAL_CASES", "model_output_cap": "Subscription may ignore max_tokens; bounded requests, no resampling",
        "analysis_sequence": "SEAL_ALL_CASE_RESULTS_BEFORE_JOINING_OUTCOMES_IN_SEPARATE_PROCESS",
        "B_policy": {"ACCEPT": "UNCHANGED_RULE_CANDIDATE", "REJECT": "SUPPRESS_ONLY_THIS_CANDIDATE_AT_ORIGINAL_CUTOFF",
                     "ABSTAIN": UNCERTAIN_POLICY, "FAILURE": UNCERTAIN_POLICY,
                     "265_unstudied_candidates": UNCERTAIN_POLICY},
        "portfolio_replay": "ALL_ORIGINAL_DECISION_POINTS_CONTINUOUS_LEDGER_WITH_CONFLICTS_NEW_OPPORTUNITIES_AND_FEES; HYBRID_PARTIAL_AGENT_COVERAGE",
        "validation_protocol": original["validation_protocol"], "historical_limitations": proof["limitations"],
        "LLM_hindsight": "Input truncation cannot eliminate model training hindsight; no strict point-in-time predictive claim",
        "runtime": {"python": platform.python_version(), **{p: research.version(p) for p in
            ("pandas", "stockstats", "pydantic", "langgraph", "langchain-openai", "openai")}},
        "execution_enabled": False, "allow_new_risk": False}
    root = output/bt.digest(protocol); root.mkdir(parents=True, exist_ok=True)
    with locked(root/"campaign"):
        immutable(root/"protocol.json", protocol)
        immutable(root/"lineage.json", lineage)
        immutable(root/"candidate_catalog.json", [{"candidate_id": d["snapshot_id"], "cutoff_ms": d["cutoff_ms"],
            "direction": d["candidate"]["direction"], "in_conditioned_cohort": d["snapshot_id"] in protocol["candidate_ids"]}
            for d in original["decisions"] if d.get("candidate")])
        for p in payloads: immutable(root/(p["candidate_id"]+".input.json"), p)
        source = Path(__file__).parent
        immutable(root/"source_snapshot.json", {name: (source/name).read_bytes().decode("utf-8") for name in protocol["code"]})
    return root


def verify_frozen(root, *, check_code=True):
    protocol = read(root/"protocol.json")
    if protocol["version"] != VERSION or protocol["execution_enabled"] is not False:
        raise ValueError("Wrong experimental protocol")
    if check_code and source_fingerprints() != protocol["code"]:
        raise ValueError("Frozen code changed; no model call permitted")
    ids = protocol["candidate_ids"]
    if len(set(ids)) != len(ids) or len(ids) != protocol["max_cases"] or len(ids) > 14:
        raise ValueError("Invalid frozen cohort")
    if len(protocol["payload_hashes"]) != len(ids):
        raise ValueError("Invalid frozen input count")
    payloads = [read(root/(sid+".input.json")) for sid in ids]
    if any(not isinstance(p, dict) for p in payloads) or [p.get("candidate_id") for p in payloads] != ids or [bt.digest(p) for p in payloads] != protocol["payload_hashes"]:
        raise ValueError("Frozen model input changed")
    return protocol, payloads


def validate_case(case, sid):
    if case["candidate_id"] != sid or case["execution_enabled"] is not False or case.get("allow_new_risk", False):
        raise ValueError("Unsafe or mismatched research result")
    if len(case.get("calls", [])) > 9:
        raise ValueError("Request budget exceeded")
    if case["status"] == "COMPLETED":
        choice = validate_choice(case["choice"], sid)
        if choice.decision != case["decision"] or len(case["calls"]) != 9 or set(case["nodes"]) != research.REQUIRED_NODES:
            raise ValueError("Incomplete/schema-mismatched completed case")
    elif case["decision"] is not None:
        raise ValueError("Failed case cannot be counted as a decision")


def seal(root, protocol):
    records = []
    for sid in protocol["candidate_ids"]:
        case = read(root/(sid+".result.json")); validate_case(case, sid)
        records.append({"candidate_id": sid, "result_hash": bt.digest(case), "status": case["status"], "decision": case["decision"]})
    value = {"protocol_hash": bt.digest(protocol), "records": records,
             "scope": "Independent Agent decisions only; no outcome join performed", "execution_enabled": False}
    immutable(root/"sealed_decisions.json", value)
    return value


def execute(root, backend_factory=None):
    """Reads NO original history/results file. At most 2 concurrent native cases."""
    with locked(root/"campaign"):
        protocol, payloads = verify_frozen(root)
        if (root/"sealed_decisions.json").exists():
            existing = seal(root, protocol)
            return existing
        config = research.model_config()
        if safe_config(config) != protocol["model_config"]:
            raise ValueError("Frozen model/config changed")
        workers = threading.local(); stop = threading.Event()
        def process(payload):
            sid = payload["candidate_id"]; target = root/(sid+".result.json")
            calls_path = root/(sid+".calls.json"); started = root/(sid+".started.json")
            if target.exists():
                case = read(target); validate_case(case, sid); return case
            common = {"candidate_id": sid, "cutoff_utc": payload["cutoff_utc"], "decision": None,
                      "calls": [], "execution_enabled": False, "allow_new_risk": False}
            if started.exists():
                case = {**common, "status": "INTERRUPTED_NOT_RETRIED", "calls": read(calls_path) if calls_path.exists() else [],
                        "unobserved_inflight_request_possible": True}
            elif stop.is_set():
                case = {**common, "status": "NOT_STARTED_SERVICE_LIMIT"}
            else:
                immutable(started, {"started_at_utc": datetime.now(UTC).isoformat(), "protocol_hash": bt.digest(protocol)})
                def persist(records):
                    tmp = calls_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(records, indent=2, allow_nan=False), encoding="utf-8")
                    os.replace(tmp, calls_path)
                try:
                    if not hasattr(workers, "models"):
                        workers.models = backend_factory(config) if backend_factory else (
                            research.create_tier_client(config, "quick").get_llm(), research.create_tier_client(config, "deep").get_llm())
                    case = research.run_case(payload, *workers.models, config, persist=persist)
                except Exception as exc:
                    failure = research.classify_failure(exc)
                    case = {**common, "status": "MODEL_INITIALIZATION_OR_WORKFLOW_FAILED", "exception_type": type(exc).__name__}
                    if failure: case["failure"] = failure.summary()
                if case.get("failure", {}).get("kind") in {"RATE_LIMIT", "AUTHENTICATION_OR_PERMISSION", "AUTH_REFRESH_UNAVAILABLE"}:
                    stop.set()
            validate_case(case, sid); immutable(target, case)
            return case
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(process, p) for p in payloads]
            for completed in as_completed(futures):
                case = completed.result()
                print(json.dumps({"candidate_id": case["candidate_id"], "status": case["status"], "decision": case["decision"],
                                  "reported_calls": len(case.get("calls", [])), "outcome_joined": False}), flush=True)
        return seal(root, protocol)


def verified_cases(root, protocol):
    manifest = read(root/"sealed_decisions.json")
    if manifest["protocol_hash"] != bt.digest(protocol) or [r["candidate_id"] for r in manifest["records"]] != protocol["candidate_ids"]:
        raise ValueError("Missing/mismatched independent decision seal")
    cases = []
    for record in manifest["records"]:
        case = read(root/(record["candidate_id"]+".result.json")); validate_case(case, record["candidate_id"])
        if bt.digest(case) != record["result_hash"] or case["decision"] != record["decision"] or case["status"] != record["status"]:
            raise ValueError("Sealed decision modified")
        cases.append(case)
    return cases


def filtered_decisions(decisions, cases):
    by_id = {c["candidate_id"]: c for c in cases}
    if len(by_id) != len(cases): raise ValueError("Duplicate Agent case")
    original_candidates = {d["snapshot_id"] for d in decisions if d.get("candidate")}
    if not set(by_id) <= original_candidates: raise ValueError("Unknown Agent candidate")
    result = copy.deepcopy(decisions)
    for d in result:
        case = by_id.get(d["snapshot_id"])
        if case and case["status"] == "COMPLETED" and case["decision"] == "REJECT":
            d.update(candidate=None, reason="D4.1_FROZEN_AGENT_REJECT")
    return result


def continuous_replay(inputs, decisions):
    data, start, end = inputs["data"], inputs["start"], inputs["end"]
    bars = bt.normalized([{"ts": int(r[0]), "Open": r[1], "High": r[2], "Low": r[3], "Close": r[4]}
                          for r in data["candles"]["1h"] if start <= int(r[0]) <= end])
    marks = {r["ts"]: r["open"] for r in data["mark_records"]}
    result = bt.simulate(bars, data["funding"], decisions, data["instrument"], inputs["config"], start, end,
                        funding_evidence=data.get("funding_evidence"), max_window_days=731, funding_prices=marks)
    result["decisions"] = decisions
    result["arithmetic_audit"] = diagnostics.audit_trades(data, result, bt.SimulationConfig.model_validate(inputs["config"]), funding_prices=marks)
    return result


def _analysis_source_provenance(root, protocol):
    """Permit the audited stdlib-import repair ONLY after sealing decisions.

    Research, execution and numerical analysis ASTs must be unchanged. The
    original protocol/source snapshot remains immutable; no model is rerun.
    """
    current = source_fingerprints()
    if current == protocol["code"]:
        return
    name = Path(__file__).name
    if set(current) != set(protocol["code"]) or any(current[k] != v for k, v in protocol["code"].items() if k != name):
        raise ValueError("Protected research/replay source changed")
    original = read(root/"source_snapshot.json")[name]
    if hashlib.sha256(original.encode("utf-8")).hexdigest() != protocol["code"][name]:
        raise ValueError("Original source snapshot changed")
    allowed_imports = {ast.dump(ast.parse("import ast").body[0]), ast.dump(ast.parse("from collections import Counter").body[0])}
    def normalized(source):
        tree = ast.parse(source)
        nodes = []
        for node in tree.body:
            if ast.dump(node) in allowed_imports:
                continue
            if isinstance(node, ast.FunctionDef) and node.name == "_analysis_source_provenance":
                continue
            if isinstance(node, ast.FunctionDef) and node.name == "analyze":
                node.body = [stmt for stmt in node.body if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == "_analysis_source_provenance")]
                node.body[0].value.keywords = []  # Only verify_frozen's analysis code-check switch differs.
            nodes.append(ast.dump(node))
        return nodes
    if normalized(original) != normalized(Path(__file__).read_text(encoding="utf-8")):
        raise ValueError("Analysis repair changed protected algorithm or prompts")
    immutable(root/"analysis_source_amendment.json", {"scope": "SEALED_RESULTS_ANALYSIS_ONLY_NO_MODEL_RERUN",
        "reason": "Add missing standard-library Counter import; verification guard only, protected ASTs unchanged",
        "original_module_hash": protocol["code"][name], "repaired_module_hash": current[name],
        "sealed_decisions_hash": bt.digest(read(root/"sealed_decisions.json")),
        "research_and_numerical_AST": "UNCHANGED", "execution_enabled": False})


def analyze(root, run):
    protocol, _ = verify_frozen(root, check_code=False)
    cases = verified_cases(root, protocol)  # Enforced BEFORE opening outcome files.
    _analysis_source_provenance(root, protocol)
    inputs, original = read(run/"inputs.json"), read(run/"result.json")
    if bt.digest(inputs) != protocol["source_digest"] or bt.digest(original) != protocol["baseline_result_digest"]:
        raise ValueError("Frozen baseline modified")
    costs.audit(inputs, original)
    decisions, lineage = locate_candidates(inputs, original)
    if [d["snapshot_id"] for d in decisions] != protocol["candidate_ids"] or bt.digest(lineage) != protocol["lineage_hash"]:
        raise ValueError("Frozen lineage mismatch")
    A = continuous_replay(inputs, original["decisions"])
    if any(A[k] != original[k] for k in ("trades", "events", "metrics", "equity_curve")):
        raise ValueError("Baseline continuous replay mismatch")
    B = continuous_replay(inputs, filtered_decisions(original["decisions"], cases))
    by_trade = {t["snapshot_id"]: t for t in original["trades"]}; B_ids = {t["snapshot_id"] for t in B["trades"]}
    attribution = []; counts = Counter()
    for case in cases:
        trade = by_trade[case["candidate_id"]]
        action = case["decision"] if case["status"] == "COMPLETED" else None
        category = ("REJECTED_WIN" if trade["net_pnl"] > 0 else "REJECTED_LOSS") if action == "REJECT" else (
            ("ACCEPTED_WIN" if trade["net_pnl"] > 0 else "ACCEPTED_LOSS") if action == "ACCEPT" else
            "ABSTAIN_NOT_SUCCESSFUL_REJECTION" if action == "ABSTAIN" else "FAILURE_NOT_SUCCESSFUL_REJECTION")
        counts[category] += 1
        usage = [c.get("usage") or {} for c in case.get("calls", [])]
        attribution.append({"candidate_id": case["candidate_id"], "cutoff_utc": case["cutoff_utc"],
            "direction": trade["direction"], "status": case["status"], "decision": action,
            "rationale": case.get("choice", {}).get("rationale"), "original_entry_utc": trade["entry_utc"],
            "original_net_pnl": trade["net_pnl"], "original_fees": trade["fees_usdt"],
            "original_funding_proxy": trade["funding_cashflow"], "classification": category,
            "entered_in_B_continuous_replay": case["candidate_id"] in B_ids,
            "reported_calls": len(case.get("calls", [])), "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
            "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
            "elapsed_seconds": case.get("elapsed_seconds"), "scope": "PAIRED_ORIGINAL_TRADE_ATTRIBUTION_NOT_PORTFOLIO_RETURN"})
    calls = [call for case in cases for call in case.get("calls", [])]
    summary = {"version": VERSION, "protocol_hash": bt.digest(protocol), "sealed_decisions_hash": bt.digest(read(root/"sealed_decisions.json")),
        "scope": protocol["scope"], "candidate_count": 279, "conditioned_cases": 14, "unstudied_candidates": 265,
        "decision_counts": {a: sum(c["status"] == "COMPLETED" and c["decision"] == a for c in cases) for a in ("ACCEPT", "REJECT", "ABSTAIN")},
        "status_counts": dict(Counter(c["status"] for c in cases)), "paired_classifications": dict(counts), "cases": attribution,
        "A_metrics": A["metrics"], "B_hybrid_continuous_metrics": B["metrics"],
        "B_not_a_full_agent_strategy": True, "uncertain_policy": UNCERTAIN_POLICY,
        "B_new_unstudied_trades": [t["snapshot_id"] for t in B["trades"] if t["snapshot_id"] not in by_trade],
        "B_original_opportunities_not_filled": [sid for sid in by_trade if sid not in B_ids],
        "A_audit": A["arithmetic_audit"], "B_audit": B["arithmetic_audit"],
        "usage": {"reported_calls": len(calls), "retries": 0,
                  "input_tokens": sum((c.get("usage") or {}).get("input_tokens", 0) for c in calls),
                  "output_tokens": sum((c.get("usage") or {}).get("output_tokens", 0) for c in calls),
                  "calls_without_usage": sum(not c.get("usage") for c in calls),
                  "call_seconds": sum(c.get("elapsed_seconds", 0) for c in calls), "actual_monetary_cost": None,
                  "cost_note": "UNKNOWN_NO_BILLING_ACCESS; reported usage excludes unknown interrupted inflight requests"},
        "limitations": protocol["historical_limitations"]+[protocol["LLM_hindsight"],
            "14 retrospectively filled opportunities condition on future execution, not all-candidate representativeness or strict out-of-sample evidence",
            "B is a hybrid with 265 unstudied rule-pass-through candidates, not full Agent coverage or real returns"],
        "execution_enabled": False, "allow_new_risk": False}
    with locked(root/"campaign"):
        immutable(root/"A_continuous.json", A); immutable(root/"B_hybrid_continuous.json", B)
        immutable(root/"analysis.json", summary)
        for name, records in (("paired_cases", attribution), ("A_trades", A["trades"]), ("B_trades", B["trades"]),
                              ("A_equity", A["equity_curve"]), ("B_equity", B["equity_curve"]), ("B_events", B["events"])):
            target = root/(name+".csv")
            if not target.exists() and records:
                with target.open("x", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in records for k in r)))
                    writer.writeheader(); writer.writerows(records)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, help="Original D.3 directory, only for prepare/analyze")
    parser.add_argument("--experiment", type=Path)
    parser.add_argument("--output", type=Path, default=Path.home()/".tradingagents/logs/okx_traded_comparison")
    mode = parser.add_mutually_exclusive_group(); mode.add_argument("--execute", action="store_true"); mode.add_argument("--analyze", action="store_true")
    args = parser.parse_args()
    if args.execute:
        if args.run or not args.experiment: parser.error("Execution accepts only --experiment, never original outcome --run")
        value = execute(args.experiment)
        print(json.dumps({"sealed": str(args.experiment/"sealed_decisions.json"), "cases": len(value["records"]), "outcome_joined": False}))
    elif args.analyze:
        if not args.run or not args.experiment: parser.error("Analysis requires --run and --experiment")
        value = analyze(args.experiment, args.run)
        print(json.dumps({"report": str(args.experiment), "decisions": value["decision_counts"], "A": value["A_metrics"],
                          "B_hybrid": value["B_hybrid_continuous_metrics"], "usage": value["usage"]}, indent=2))
    else:
        if not args.run or args.experiment: parser.error("Preparation requires --run")
        print(json.dumps({"experiment": str(prepare(args.run, args.output)), "new_model_calls": 0}))


if __name__ == "__main__": main()
