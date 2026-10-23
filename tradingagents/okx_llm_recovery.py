"""D.4.2 offline diagnosis and separately labelled exploratory role recovery."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from tradingagents import okx_traded_comparison as original
from tradingagents import okx_agent_comparison as research
from tradingagents.dataflows.files import locked
from tradingagents.llm_clients.recovery import RecoveryController, RecoveringModel, RoleJournal, digest

VERSION = "okx-llm-recovery-v1"
ROLES = ["Market Analyst", "Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
         "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager"]


def file_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def load_source(root):
    protocol, payloads = original.verify_frozen(root, check_code=False)
    cases = original.verified_cases(root, protocol)
    for case in cases:
        calls = root/(case["candidate_id"]+".calls.json")
        if calls.exists() and original.read(calls) != case["calls"]:
            raise ValueError("Original raw call log differs from sealed case")
    # This reads no ledger, outcome, memory or reflection file.
    return protocol, dict(zip(protocol["candidate_ids"], payloads, strict=True)), cases


def complete_prefix(case):
    """Validate both native completed nodes and corresponding completed calls."""
    prefix = []
    for call in case.get("calls", []):
        if call["status"] != "COMPLETED":
            break
        if call.get("call") != len(prefix)+1 or not isinstance(call.get("input"), str):
            raise ValueError("Invalid complete role prefix")
        if call.get("schema"):
            choice = research.validate_choice(call["parsed"], case["candidate_id"])
            if call["schema"] != type(choice).__name__:
                raise ValueError("Unknown role schema")
        elif not call.get("output") or call.get("schema_tool_calls"):
            raise ValueError("Invalid complete text role")
        prefix.append(copy.deepcopy(call))
    if case.get("nodes") != ROLES[:len(prefix)]:
        raise ValueError("Completed calls do not match completed native roles")
    return prefix


def audit(root):
    protocol, _, cases = load_source(root)
    rows = []
    for case in cases:
        prefix = complete_prefix(case)
        failed = [c for c in case["calls"] if c["status"] != "COMPLETED"]
        rows.append({"candidate_id": case["candidate_id"], "cutoff_utc": case["cutoff_utc"],
            "status": case["status"], "completed_roles": ROLES[:len(prefix)],
            "failed_role": ROLES[len(prefix)] if len(prefix) < 9 else None,
            "failures": [{"failure": c.get("failure"), "exception_type": c.get("exception_type"),
                          "usage": c.get("usage"), "elapsed_seconds": c.get("elapsed_seconds")} for c in failed],
            "automatic_recovery": "BLOCKED_UNKNOWN_OR_PARTIAL" if failed else "COMPLETE_NO_CALL_NEEDED"})
    failures = [f for row in rows for f in row["failures"]]
    return {"version": VERSION, "scope": "EXPLORATORY_DIAGNOSIS_NOT_FROZEN_EXPERIMENT",
        "source_protocol_hash": research.bt.digest(protocol), "cases": rows,
        "case_count": len(cases), "failure_count": len(failures),
        "failure_kinds": dict(Counter(f.get("failure", {}).get("kind", "UNKNOWN") for f in failures)),
        "failed_call_usage_unknown": sum(f["usage"] is None for f in failures),
        "root_cause": "Original sanitized logs lack provider code/status/request ID; specific service cause unproven",
        "client": {"streaming": True, "timeout_seconds": 120, "sdk_max_retries": 0,
                   "original_concurrent_cases": original.WORKERS, "recovery_concurrent_requests": 1,
                   "pool_default_max_connections": 1000, "pool_default_max_keepalive": 100},
        "versions": {p: version(p) for p in ("openai", "langchain-openai", "httpx", "httpcore", "httpx2", "httpcore2")},
        "execution_enabled": False}


def recover(root, output, candidate_id, *, backend_factory=None, restart_incomplete=False, controller=None):
    """Explicit separate experiment; never automatically restart old partial calls.

    restart_incomplete is a deliberate NEW generation of a tool-free role, not
    continuation of old text. The CLI requires an extra explicit switch. No
    such paid restart is performed by offline diagnosis or automated tests.
    """
    if root.resolve() == output.resolve() or root.resolve() in output.resolve().parents:
        raise ValueError("Recovery output must be outside original experiment")
    protocol, payloads, cases = load_source(root)
    case = next(c for c in cases if c["candidate_id"] == candidate_id)
    if case["status"] == "COMPLETED":
        raise ValueError("Original complete decision must not be resampled")
    prefix = complete_prefix(case)
    if not restart_incomplete:
        raise ValueError("Original partial/unknown error requires diagnosis and explicit fresh-role restart")
    config = research.model_config()
    if original.safe_config(config) != protocol["model_config"]:
        raise ValueError("Frozen model/config changed")
    payload = payloads[candidate_id]
    identity = {"version": VERSION, "scope": "EXPLORATORY_RECOVERY_EXCLUDED_FROM_D4_1",
                "transport_mode": "MOCK_NO_NETWORK" if backend_factory else "REAL_MODEL",
                "source_protocol_hash": research.bt.digest(protocol), "case_hash": research.bt.digest(case),
                "payload_hash": research.bt.digest(payload), "candidate_id": candidate_id,
                "code": original.source_fingerprints(), "model_config": protocol["model_config"],
                "recovery_code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "execution_enabled": False}
    output.mkdir(parents=True, exist_ok=True)
    with locked(output/"campaign"):
        final_path = output/(candidate_id+".exploratory.json")
        if final_path.exists():
            saved = original.read(final_path)
            if saved["identity"] != identity:
                raise ValueError("Completed recovery identity changed")
            return saved
        if (output/"campaign_stop.json").exists():
            raise ValueError("Campaign stopped after terminal service failure; diagnose before a new experiment")
        journal = RoleJournal(output/(candidate_id+".journal.json"), identity, imported=prefix)
        config.update(llm_max_retries=0)
        if backend_factory is None:
            # Do not construct clients at all during audit. Lazy creation here.
            quick = research.create_tier_client(config, "quick").get_llm()
            deep = research.create_tier_client(config, "deep").get_llm()
        else:
            quick, deep = backend_factory(config)
        for model in (quick, deep):
            if getattr(model, "max_retries", 0) != 0:
                raise ValueError("Disable nested SDK retries")
        control = controller or RecoveryController()
        result = research.run_case(payload, RecoveringModel(quick, journal, control, "quick"),
            RecoveringModel(deep, journal, control, "deep"), config)
        if control.stopped:
            original.immutable(output/"campaign_stop.json", {"reason": control.stopped,
                               "execution_enabled": False})
        original.validate_case(result, candidate_id)
        saved = {"identity": identity, "result": result, "reused_complete_roles": len(prefix),
                 "new_attempt_count": sum(len(r["attempts"]) for r in journal.data["roles"]),
                 "unknown_usage_is_not_zero_cost": True, "execution_enabled": False}
        # Failed runs retain their journal; complete decisions are sealed once.
        if result["status"] == "COMPLETED":
            original.immutable(final_path, saved)
        return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--restart-incomplete-role", action="store_true",
                        help="Explicit paid NEW tool-free generation; never part of frozen D.4.1")
    args = parser.parse_args()
    root, output = args.experiment.resolve(), args.output.resolve()
    if output == root or root in output.parents or output in root.parents:
        raise ValueError("Output must be separate from original experiment tree")
    before = file_hashes(root)
    if args.recover:
        if not args.candidate or not args.restart_incomplete_role:
            parser.error("Recovery requires one candidate and explicit --restart-incomplete-role")
        value = recover(root, output, args.candidate, restart_incomplete=True)
        print(json.dumps({"status": value["result"]["status"], "output": str(output), "execution_enabled": False}))
    else:
        value = audit(root)
        output.mkdir(parents=True, exist_ok=True)
        original.immutable(output/"diagnosis.json", value)
        original.immutable(output/"original_artifact_hashes.json", before)
        print(json.dumps({"cases": value["case_count"], "failed": value["failure_count"],
                          "output": str(output), "model_calls": 0, "execution_enabled": False}))
    if before != file_hashes(root):
        raise ValueError("Original experiment changed during audit/recovery")


if __name__ == "__main__":
    main()
