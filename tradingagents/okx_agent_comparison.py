"""D.4: bounded native-workflow historical filtering, no trading or memory writes."""
from __future__ import annotations
import argparse
import copy
import json
import os
import time
from collections import Counter
from datetime import datetime, UTC
from pathlib import Path
from importlib.metadata import version
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from tradingagents import okx_backtest as bt, okx_continuous as continuous, okx_cost_audit as costs
from tradingagents.agents.candidate_filter import INSTRUCTIONS, PROMPT_VERSION, validate_choice
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.files import locked
from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.llm_clients import create_tier_client
from tradingagents.llm_clients.reliability import classify_failure

VERSION = "okx-agent-comparison-v1.1"
REQUIRED_NODES = {"Market Analyst", "Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
                  "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager"}


def sample_candidates(decisions):
    """Predeclared first/middle/last of ALL candidates; no fills or outcomes used."""
    candidates = sorted((d for d in decisions if d.get("candidate")), key=lambda d: d["cutoff_ms"])
    if not candidates:
        raise ValueError("No valid candidates, not zero-return experimental evidence")
    return [candidates[i] for i in sorted({0, len(candidates)//2, len(candidates)-1})]


def case_input(data, decision, config):
    cutoff = decision["cutoff_ms"]
    if cutoff >= continuous.DEVELOPMENT_END:
        raise ValueError("Protected holdout")
    settings = {"okx_multi_timeframe": True, "okx_interval": "1h", "okx_min_bars": 250,
                "okx_history_bars": 400, "okx_max_lag_bars": 0,
                "okx_plan_confirmation_bars": config.confirmation_bars}
    # Allowlist the data handed to reconstruction: funding, marks, outcomes,
    # server wall-clock and any account/memory fields cannot enter the prompt.
    causal = {"symbol": data["symbol"], "server_ms": cutoff,
              "instrument": copy.deepcopy(data["instrument"]),
              "candles": {p: [copy.deepcopy(r) for r in rows if int(r[0])+bt.okx.BARS[p][1] <= cutoff]
                          for p, rows in data["candles"].items()}}
    regenerated = bt.regenerate_decisions(causal, cutoff, cutoff+4*bt.HOUR, config, diagnostics=True)[0]
    # D.2 adds lifecycle/outcome fields to stored diagnostics after settlement.
    # Compare only freshly reconstructed causal fields; NEVER serialize extras.
    causal_original = {k: ({field: decision[k].get(field) for field in value} if k == "diagnostic" else decision[k])
                       for k, value in regenerated.items()}
    if regenerated != causal_original:
        raise ValueError("Historical decision reconstruction mismatch")
    bundle = bt.historical_bundle(causal, cutoff, settings)
    evidence = copy.deepcopy(bundle["timeframe_analysis"])
    for p, child in bundle["timeframes"].items():
        if child["latest_close_ms"] > cutoff or child["status"] != "VALID":
            raise ValueError("Future/invalid historical timeframe")
        evidence[p]["latest_close_ms"] = child["latest_close_ms"]
        # Native live bundle IDs include retrieval time. Historical experiment
        # identity must depend on causal content, not today's clock.
        evidence[p]["snapshot_id"] = bt.digest({"candidate_id": regenerated["snapshot_id"],
            "timeframe": p, "closed_raw_candles": causal["candles"][p][-400:]})
        for pivot in evidence[p].get("pivots", []):
            if pivot.get("confirmed_at_ms", cutoff+1) > cutoff:
                raise ValueError("Unconfirmed pivot")
    return {"candidate_id": decision["snapshot_id"], "exchange": "OKX", "instrument": bt.SYMBOL,
            "cutoff_ms": cutoff, "cutoff_utc": bt.iso(cutoff), "snapshot_id": regenerated["snapshot_id"],
            "timeframe_snapshot_id_definition": "Hash of candidate ID, timeframe and last 400 closed raw candles; excludes retrieval clock",
            "candidate": copy.deepcopy(regenerated["candidate"]), "timeframes": evidence,
            "recent_closed_candles": {p: copy.deepcopy(rows[-12:]) for p, rows in causal["candles"].items()},
            "history_context": "Indicators use up to 400 closed bars per timeframe; recent 12 bars shown, full source pinned by experiment hash",
            "cost_assumptions": config.model_dump(),
            "quantity": "Original fixed-notional sizing rule only, realized quantity is not yet knowable",
            "missing_derivatives": ["historical OI", "orderbook", "forecast funding", "news", "account positions"],
            "parameter_evidence": "Current instrument spec assumed historically; continuity unverified",
            "publication_assumption": "Closed bars observable at close; actual dissemination/revision history unknown",
            "execution_enabled": False}


class AuditedModel:
    """Bounded model facade; only native-role text and schema calls, no tools."""
    def __init__(self, backend, log, budget, tier, schema=None):
        self.backend, self.log, self.budget, self.tier, self.schema = backend, log, budget, tier, schema

    def bind_tools(self, *args, **kwargs):
        raise ValueError("External tools forbidden in historical comparison")

    def with_structured_output(self, schema):
        return AuditedModel(self.backend, self.log, self.budget, self.tier, schema)

    def invoke(self, prompt):
        if self.budget["calls"] >= 9 or time.monotonic() > self.budget["deadline"]:
            raise RuntimeError("EXPERIMENT_BUDGET_EXHAUSTED")
        if not isinstance(prompt, str):
            raise ValueError("Only auditable role prompts are allowed")
        self.budget["calls"] += 1
        # Prefix applies even to the existing Bull/Bear and risk-role prompts.
        text = INSTRUCTIONS + "\n" + prompt
        item = {"call": self.budget["calls"], "tier": self.tier,
                "schema": self.schema.__name__ if self.schema else None,
                "input": text, "started_at_utc": datetime.now(UTC).isoformat(),
                "status": "STARTED", "retries": 0}
        self.log.append(item)
        began = time.monotonic()
        try:
            if self.schema:
                envelope = self.backend.with_structured_output(self.schema, include_raw=True).invoke(text)
                raw, parsed = envelope["raw"], envelope["parsed"]
                item["output"] = raw.content
                item["schema_tool_calls"] = getattr(raw, "tool_calls", [])
                if envelope.get("parsing_error") or parsed is None:
                    raise ValueError("STRUCTURED_FORMAT_ERROR")
                value = self.schema.model_validate(parsed.model_dump() if hasattr(parsed, "model_dump") else parsed)
                item["parsed"] = value.model_dump(mode="json")
            else:
                raw = self.backend.invoke(text)
                item["output"] = raw.content
                if getattr(raw, "tool_calls", None) or not raw.content:
                    raise ValueError("UNEXPECTED_TOOL_OR_EMPTY_OUTPUT")
                value = raw
            item["usage"] = getattr(raw, "usage_metadata", None)
            metadata = getattr(raw, "response_metadata", {})
            item["model_metadata"] = {k: metadata[k] for k in ("model_name", "model", "system_fingerprint", "finish_reason") if k in metadata}
            item["status"] = "COMPLETED"
            return value
        except Exception as exc:
            failure = classify_failure(exc)
            item["status"] = "MODEL_FAILED" if failure else "FORMAT_OR_PROGRAM_ERROR"
            item["exception_type"] = type(exc).__name__
            if failure:
                item["failure"] = failure.summary()
            raise
        finally:
            item["elapsed_seconds"] = time.monotonic()-began
            if self.budget.get("persist"):
                self.budget["persist"](self.log)


def run_case(payload, quick, deep, config, *, persist=None):
    before = bt.digest(payload)
    audit, nodes = [], []
    budget = {"calls": 0, "deadline": time.monotonic()+1200, "persist": persist}
    state = Propagator().create_initial_state(bt.SYMBOL, payload["cutoff_utc"][:10], "crypto",
        instrument_context=INSTRUCTIONS+"\nCutoff: "+payload["cutoff_utc"]+"\n"+json.dumps(payload, ensure_ascii=False))
    state.update(plan_mode=True, candidate_filter_mode=True, candidate_filter_input=copy.deepcopy(payload),
                 position_context="UNKNOWN", execution_enabled=False, allow_new_risk=False,
                 data_quality="VALID_HISTORICAL_ASSUMPTIONS", past_context="", memory_note="Disabled in D.4")
    began = time.monotonic()
    try:
        graph = GraphSetup(AuditedModel(quick, audit, budget, "quick"), AuditedModel(deep, audit, budget, "deep"),
                           ConditionalLogic(1, 1), 1).setup_graph(["market"], historical_filter=True).compile()
        with run_config(config):
            for update in graph.stream(state, config={"recursion_limit": 30}, stream_mode="updates"):
                for node, value in update.items():
                    nodes.append(node); state.update(value)
        if set(nodes) != REQUIRED_NODES or len(nodes) != 9:
            raise ValueError("Incomplete native workflow")
        if state.get("past_context") or state.get("execution_enabled") or before != bt.digest(payload) or state["candidate_filter_input"] != payload:
            raise ValueError("Isolation or immutable candidate violated")
        choice = validate_choice(state["portfolio_choice"], payload["candidate_id"])
        status = "COMPLETED"
        result = {"decision": choice.decision, "choice": choice.model_dump()}
    except Exception as exc:
        failure = classify_failure(exc)
        status = "MODEL_FAILED" if failure else ("BUDGET_EXHAUSTED" if str(exc) == "EXPERIMENT_BUDGET_EXHAUSTED" else "FORMAT_OR_WORKFLOW_ERROR")
        result = {"decision": None, "exception_type": type(exc).__name__}
        if failure:
            result["failure"] = failure.summary()
    return {"candidate_id": payload["candidate_id"], "cutoff_utc": payload["cutoff_utc"], "status": status,
            **result, "nodes": nodes, "calls": audit, "elapsed_seconds": time.monotonic()-began,
            "execution_enabled": False, "allow_new_risk": False}


def paired_ledger(inputs, original, ids):
    """Hold original admitted opportunities fixed, no replacement opportunities.

    Acceptance cannot alter fills, sizing or conditions. Never evaluates an
    originally blocked candidate as a new replacement trade after a rejection.
    """
    executed = {t["snapshot_id"] for t in original["trades"]}
    decisions = [d for d in original["decisions"] if d["snapshot_id"] in ids & executed]
    start, end = inputs["start"], inputs["end"]; data = inputs["data"]
    bars = bt.normalized([{"ts": int(r[0]), "Open": r[1], "High": r[2], "Low": r[3], "Close": r[4]}
                          for r in data["candles"]["1h"] if start <= int(r[0]) <= end])
    replay = bt.simulate(bars, data["funding"], decisions, data["instrument"], inputs["config"], start, end,
        funding_evidence=data["funding_evidence"], max_window_days=731,
        funding_prices={r["ts"]: r["open"] for r in data["mark_records"]})
    expected = [t for t in original["trades"] if t["snapshot_id"] in ids]
    if replay["trades"] != expected:
        raise ValueError("Filter altered original fill/quantity/outcome")
    return replay


def comparison(inputs, original, cases):
    selected = {c["candidate_id"] for c in cases}
    decisive = {c["candidate_id"] for c in cases if c["status"] == "COMPLETED" and c["decision"] in {"ACCEPT", "REJECT"}}
    accepted = {c["candidate_id"] for c in cases if c["status"] == "COMPLETED" and c["decision"] == "ACCEPT"}
    trades = {t["snapshot_id"]: t for t in original["trades"]}
    rows = []
    for case in cases:
        t = trades.get(case["candidate_id"])
        decision = case["decision"] if case["status"] == "COMPLETED" else None
        rows.append({"candidate_id": case["candidate_id"], "cutoff_utc": case["cutoff_utc"],
            "status": case["status"], "decision": decision,
            "A_original_trade": bool(t), "A_net_pnl": t["net_pnl"] if t else None,
            "B_trade": bool(t and decision == "ACCEPT"),
            "interpretation": "NOT_A_SUCCESSFUL_REJECTION" if decision in {None, "ABSTAIN"} else
                ("NO_ORIGINAL_TRADE_NO_PNL_EFFECT" if not t else
                 ("KEPT_WIN" if t["net_pnl"] > 0 else "KEPT_LOSS") if decision == "ACCEPT" else
                 ("MISSED_WIN" if t["net_pnl"] > 0 else "FILTERED_LOSS")),
            "rationale": case.get("choice", {}).get("rationale")})
    groups = {"A_all": original, "A_studied": paired_ledger(inputs, original, selected),
              "A_decisive_only": paired_ledger(inputs, original, decisive),
              "B_accepted_only": paired_ledger(inputs, original, accepted)}
    return {"all_candidates": sum(bool(d.get("candidate")) for d in original["decisions"]),
            "selected_cases": len(cases), "agent_attempted": sum(bool(c.get("calls")) for c in cases),
            "completed": sum(c["status"] == "COMPLETED" for c in cases),
            "decision_counts": {k: sum(c["status"] == "COMPLETED" and c["decision"] == k for c in cases) for k in ("ACCEPT", "REJECT", "ABSTAIN")},
            "status_counts": dict(Counter(c["status"] for c in cases)),
            "unresearched_candidates": sum(bool(d.get("candidate")) for d in original["decisions"])-len(cases),
            "B_all": {"status": "NOT_EVALUATED", "metrics": None},
            "paired_scope": "Original admitted opportunities fixed; no reallocation/replacement. A_decisive_only versus B_accepted_only excludes abstentions/failures from claimed filtering benefit.",
            "groups": {n: {"metrics": g["metrics"], "trades": g["trades"], "equity_curve": g["equity_curve"]} for n, g in groups.items()},
            "cases": rows,
            "cost_sensitivity": {n: {s: {"fee_bps_per_leg": fee, "slippage_bps_per_leg": slip,
                "net_pnl": sum(costs.reprice(t, inputs["data"]["instrument"]["tickSz"], fee, slip)["net_pnl"] for t in g["trades"])}
                for s, (fee, slip) in costs.SCENARIOS.items()} for n, g in groups.items()},
            "execution_enabled": False}


def model_config():
    from tradingagents.default_config import DEFAULT_CONFIG
    from cli.prefs import load_last_run, sanitize
    from cli.models import AssetType
    config = copy.deepcopy(DEFAULT_CONFIG)
    prefs = sanitize(load_last_run(), AssetType.CRYPTO)
    for key, env in {"llm_provider": "TRADINGAGENTS_LLM_PROVIDER", "quick_think_llm": "TRADINGAGENTS_QUICK_THINK_LLM",
                     "deep_think_llm": "TRADINGAGENTS_DEEP_THINK_LLM", "backend_url": "TRADINGAGENTS_LLM_BACKEND_URL"}.items():
        if key in prefs and not os.environ.get(env):
            config[key] = prefs[key]
    config.update(llm_max_retries=0, max_debate_rounds=1, max_risk_discuss_rounds=1, max_tokens=1200,
                  output_language="English", checkpoint_enabled=False)
    return config


def code_fingerprint():
    import hashlib
    root = Path(__file__).parent
    paths = sorted({*root.glob("agents/**/*.py"), *root.glob("graph/*.py"), *root.glob("llm_clients/*.py"),
                    root/"okx_agent_comparison.py", root/"okx_backtest.py", root/"okx_continuous.py"})
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def write_new(path, value):
    text = json.dumps(value, indent=2, allow_nan=False)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError("Immutable artifact mismatch")
    else:
        with path.open("x", encoding="utf-8") as f: f.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--real-llm", action="store_true", help="Run the registered 3-case pilot, at most 27 requests; no retries")
    parser.add_argument("--resume-experiment", type=Path, help="Read an already completed immutable experiment offline; no model calls even after code updates")
    parser.add_argument("--output", type=Path, default=Path.home()/".tradingagents/logs/okx_agent_comparison")
    args = parser.parse_args()
    inputs = json.loads((args.run/"inputs.json").read_text(encoding="utf-8"))
    original = json.loads((args.run/"result.json").read_text(encoding="utf-8"))
    proof = costs.audit(inputs, original)
    if args.resume_experiment:
        if args.real_llm:
            raise ValueError("Offline resume cannot request real LLM calls")
        protocol = json.loads((args.resume_experiment/"protocol.json").read_text(encoding="utf-8"))
        if protocol["source_digest"] != bt.digest(inputs) or protocol["baseline_result_digest"] != bt.digest(original):
            raise ValueError("Resume source mismatch")
        for sid, expected_hash in zip(protocol["candidate_ids"], protocol["payload_hashes"], strict=True):
            payload = json.loads((args.resume_experiment/(sid+".input.json")).read_text(encoding="utf-8"))
            if bt.digest(payload) != expected_hash:
                raise ValueError("Resume input hash mismatch")
        summary = json.loads((args.resume_experiment/"comparison.json").read_text(encoding="utf-8"))
        if summary["execution_enabled"] is not False:
            raise ValueError("Invalid research-only artifact")
        print(json.dumps({"report": str(args.resume_experiment), "status": "OFFLINE_EXISTING_RESULT",
                          "original_version": protocol["version"], "decision_counts": summary["decision_counts"],
                          "new_model_calls": 0, "execution_enabled": False}), flush=True)
        return
    config = model_config()
    safe_config = {k: config.get(k) for k in ("llm_provider", "quick_think_provider", "deep_think_provider", "quick_think_llm", "deep_think_llm", "llm_max_retries", "max_tokens", "temperature", "openai_reasoning_effort", "output_language")}
    payloads = []
    for d in sample_candidates(original["decisions"]):
        payloads.append(case_input(inputs["data"], d, bt.SimulationConfig.model_validate(inputs["config"])))
    protocol = {"version": VERSION, "strategy_fingerprint": proof["strategy_fingerprint"],
        "source_digest": bt.digest(inputs), "baseline_result_digest": bt.digest(original),
        "sample_rule": "ALL_CANDIDATES_SORTED_BY_CUTOFF_INDICES_0_FLOOR_N_DIV_2_N_MINUS_1_NO_OUTCOME_SELECTION",
        "candidate_ids": [p["candidate_id"] for p in payloads], "payload_hashes": [bt.digest(p) for p in payloads],
        "code": code_fingerprint(), "prompt_version": PROMPT_VERSION,
        "model_config": safe_config, "max_requests_per_case": 9, "max_cases": 3,
        "max_seconds_per_case": 1200, "retry_policy": "ZERO_SDK_OR_EXPERIMENT_RETRIES_NO_RESAMPLING",
        "fill_protocol": "Original admitted opportunity schedule fixed; no reallocation or replacement; reject/abstain/failed are distinct",
        "knowledge_contamination": "Historical truncation cannot eliminate LLM training hindsight; exploratory DEVELOPMENT only",
        "validation_protocol": original["validation_protocol"],
        "runtime": {p: version(p) for p in ("langgraph", "langchain-openai", "openai", "pydantic")},
        "historical_assumptions": proof["limitations"], "execution_enabled": False}
    root = args.output/bt.digest(protocol); root.mkdir(parents=True, exist_ok=True)
    write_new(root/"protocol.json", protocol)
    for p in payloads: write_new(root/(p["candidate_id"]+".input.json"), p)
    print(json.dumps({"protocol": str(root), "candidates": len(payloads), "models": safe_config,
                      "execution_enabled": False}), flush=True)
    if not args.real_llm:
        return
    with locked(root/"experiment"):
        cases = []
        quick = deep = None
        for payload in payloads:
            target = root/(payload["candidate_id"]+".result.json")
            started = root/(payload["candidate_id"]+".started.json")
            calls_path = root/(payload["candidate_id"]+".calls.json")
            if target.exists():
                case = json.loads(target.read_text(encoding="utf-8"))
            elif started.exists():
                case = {"candidate_id": payload["candidate_id"], "cutoff_utc": payload["cutoff_utc"],
                    "status": "INTERRUPTED_NOT_RETRIED", "decision": None,
                    "calls": json.loads(calls_path.read_text(encoding="utf-8")) if calls_path.exists() else [],
                    "execution_enabled": False}
                write_new(target, case)
            else:
                write_new(started, {"started_at_utc": datetime.now(UTC).isoformat(), "protocol": bt.digest(protocol)})
                def persist(records):
                    temp = calls_path.with_suffix(".tmp")
                    temp.write_text(json.dumps(records, indent=2, allow_nan=False), encoding="utf-8")
                    temp.replace(calls_path)
                try:
                    if quick is None:
                        quick = create_tier_client(config, "quick").get_llm()
                        deep = create_tier_client(config, "deep").get_llm()
                    case = run_case(payload, quick, deep, config, persist=persist)
                except Exception as exc:
                    failure = classify_failure(exc)
                    case = {"candidate_id": payload["candidate_id"], "cutoff_utc": payload["cutoff_utc"],
                            "status": "MODEL_INITIALIZATION_FAILED", "decision": None, "calls": [],
                            "exception_type": type(exc).__name__, "execution_enabled": False}
                    if failure: case["failure"] = failure.summary()
                write_new(target, case)
            cases.append(case)
            print(json.dumps({"case": case["candidate_id"], "status": case["status"], "decision": case["decision"],
                              "calls": len(case.get("calls", []))}), flush=True)
        summary = comparison(inputs, original, cases)
        calls = [call for case in cases for call in case.get("calls", [])]
        summary["model_usage"] = {"calls": len(calls), "retries": 0,
            "reported_input_tokens": sum((c.get("usage") or {}).get("input_tokens", 0) for c in calls),
            "reported_output_tokens": sum((c.get("usage") or {}).get("output_tokens", 0) for c in calls),
            "calls_without_usage": sum(not c.get("usage") for c in calls),
            "call_elapsed_seconds": sum(c.get("elapsed_seconds", 0) for c in calls),
            "actual_monetary_cost": None, "cost_status": "UNKNOWN_NO_BILLING_ACCESS; subscription output cap may be unsupported"}
        write_new(root/"comparison.json", summary)
        print(json.dumps({"report": str(root), "decision_counts": summary["decision_counts"], "model_usage": summary["model_usage"]}), flush=True)


if __name__ == "__main__": main()
