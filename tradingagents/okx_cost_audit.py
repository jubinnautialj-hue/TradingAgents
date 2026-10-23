"""Offline D.3.1 audit. Fixed executed-trade repricing is NOT a strategy backtest."""
from __future__ import annotations
import argparse
import csv
import json
import math
from decimal import Decimal
from pathlib import Path
from tradingagents import okx_backtest as bt, okx_continuous as continuous, okx_diagnostics as diag
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv

VERSION = "okx-cost-audit-v1"
# Predeclared research assumptions, not account fee tiers or measured liquidity.
SCENARIOS = {"lower": (3, 1), "base": (5, 2), "higher": (8, 5)}


def reprice(trade, tick, fee_bps, slip_bps):
    """Keep size, references, timestamps and exit selection fixed; slip only fills."""
    if not all(math.isfinite(x) and 0 <= x <= 100 for x in (fee_bps, slip_bps)):
        raise ValueError("Invalid cost assumption")
    sign = 1 if trade["direction"] == "LONG" else -1
    if trade["direction"] not in {"LONG", "SHORT"}:
        raise ValueError("Invalid direction")
    qty = trade["base_quantity"]
    if not math.isfinite(qty) or qty <= 0:
        raise ValueError("Invalid quantity")
    entry = bt.plans.round_tick(trade["entry_reference"]*(1+sign*slip_bps/10000), tick, sign > 0)
    exit_price = bt.plans.round_tick(trade["exit_reference"]*(1-sign*slip_bps/10000), tick, sign < 0)
    fees = qty*(entry+exit_price)*fee_bps/10000
    slippage = qty*(abs(entry-trade["entry_reference"])+abs(exit_price-trade["exit_reference"]))
    gross = sign*qty*(exit_price-entry)
    geometry = trade["stop"] < entry < trade["target"] if sign > 0 else trade["target"] < entry < trade["stop"]
    return {"entry_price": entry, "exit_price": exit_price, "fees_usdt": fees,
            "slippage_usdt": slippage, "funding_cashflow": trade["funding_cashflow"],
            "gross_pnl_after_slippage": gross, "net_pnl": gross-fees+trade["funding_cashflow"],
            "entry_geometry_preserved": geometry}


def funding_details(trade, funding, marks):
    """Actual recorded rates + hourly MARK-open proxy; boundary ambiguity explicit."""
    events = []
    intrabar = trade["exit_reason"] in {"STOP", "TARGET", "STOP_FIRST_AMBIGUOUS"}
    for row in funding:
        ts = row["ts"]
        held = trade["entry_ms"] < ts and (ts < trade["exit_ms"] if intrabar else ts <= trade["exit_ms"])
        boundary = ts == trade["entry_ms"] or ts == trade["exit_ms"]
        if not held and not boundary:
            continue
        if ts not in marks:
            raise ValueError("Missing historical mark evidence")
        mark = marks[ts]
        values = [mark[k] for k in ("open", "high", "low", "close")]
        if not all(math.isfinite(v) and v > 0 for v in values) or not mark["low"] <= min(values[0], values[3]) <= max(values[0], values[3]) <= mark["high"]:
            raise ValueError("Invalid historical mark evidence")
        flow = lambda price: deriv.funding_cashflow(trade["base_quantity"], price, row["realized_rate"], trade["direction"])
        bounds = sorted([flow(mark["low"]), flow(mark["high"])])
        events.append({"ts": ts, "utc": bt.iso(ts), "realized_rate": row["realized_rate"],
                       "model_charged": held, "settlement_boundary_ambiguous": boundary,
                       "mark_hour_open_proxy": mark["open"], "cashflow": flow(mark["open"]) if held else 0.,
                       "if_held_hour_range_min": bounds[0], "if_held_hour_range_max": bounds[1],
                       "scope": "Hourly range valuation sensitivity, NOT exact assessment price/time"})
    return events


def audit(inputs, original):
    """Fail closed on altered baseline, future holdout, evidence or ledger mismatch."""
    fingerprint = continuous.strategy_fingerprint()
    config = bt.SimulationConfig.model_validate(inputs["config"])
    if config != continuous.fixed_config() or inputs["strategy_fingerprint"] != fingerprint or original["strategy_fingerprint"] != fingerprint:
        raise ValueError("Frozen strategy/config mismatch")
    start, end = inputs["start"], inputs["end"]
    if end > continuous.DEVELOPMENT_END:
        raise ValueError("Protected holdout")
    data = inputs["data"]
    if data["symbol"] != bt.SYMBOL or original["execution_enabled"] is not False:
        raise ValueError("Identity/permission mismatch")
    marks = {r["ts"]: r for r in data["mark_records"]}
    if len(marks) != len(data["mark_records"]) or sorted(marks) != list(range(start, end+bt.HOUR, bt.HOUR)):
        raise ValueError("Historical mark coverage incomplete")
    for interval, raw in data["candles"].items():
        if any(str(r[8]) != "1" or int(r[0])+okx.BARS[interval][1] > end+bt.HOUR for r in raw):
            raise ValueError("Unconfirmed/future cached candle")
        frame, _ = okx.validate_candles(raw, interval, end+bt.HOUR, 250, len(raw), 0)
        if not ((frame.Volume-frame.VolumeContracts*deriv.multiplier(data["instrument"])).abs() <= frame.Volume.abs()*1e-6+1e-8).all():
            raise ValueError("Historical volume-unit mismatch")
    decisions = original["decisions"]
    for d in decisions:
        if not start <= d["cutoff_ms"] < end or d["evidence_latest_ms"] > d["cutoff_ms"] or not d["diagnostic"]["data_quality"]:
            raise ValueError("Invalid decision/PIT evidence")
        for evidence in d["diagnostic"]["evidence"].values():
            if evidence["latest_close_ms"] > d["cutoff_ms"]:
                raise ValueError("Future timeframe evidence")
    raw = [r for r in data["candles"]["1h"] if start <= int(r[0]) <= end]
    bars = bt.normalized([{"ts": int(r[0]), "Open": r[1], "High": r[2], "Low": r[3], "Close": r[4]} for r in raw])
    prices = {ts: r["open"] for ts, r in marks.items()}
    replay = bt.simulate(bars, data["funding"], decisions, data["instrument"], config, start, end,
                         funding_evidence=data["funding_evidence"], max_window_days=731, funding_prices=prices)
    for field in ("trades", "events", "equity_curve", "metrics"):
        if replay[field] != original[field]:
            raise ValueError("Baseline replay mismatch: "+field)
    replay["decisions"] = decisions
    independent = diag.audit_trades(data, replay, config, funding_prices=prices)
    funding_proof = continuous.verify_archive_evidence(data["funding"], data["funding_evidence"], start, end)
    rows, scenario_rows = [], {name: [] for name in SCENARIOS}
    info = data["instrument"]
    for number, t in enumerate(replay["trades"], 1):
        events = funding_details(t, data["funding"], marks)
        total = sum(e["cashflow"] for e in events)
        if not math.isclose(total, t["funding_cashflow"], abs_tol=1e-9):
            raise ValueError("Funding reconciliation failed")
        contracts = Decimal(str(t["contracts"]))
        if contracts < Decimal(info["minSz"]) or contracts % Decimal(info["lotSz"]):
            raise ValueError("Quantity does not conform to assumed spec")
        row = {"trade_number": number, **t, "quantity_status": "PASS_UNDER_ASSUMED_SPEC",
               "funding_events": events, "funding_price_status": "HOURLY_MARK_OPEN_PROXY",
               "historical_spec_status": "CONTINUITY_UNVERIFIED", "audit_status": "ARITHMETIC_PASSED"}
        rows.append(row)
        for name, (fee, slip) in SCENARIOS.items():
            repriced = reprice(t, info["tickSz"], fee, slip)
            if name == "base":
                for k in ("entry_price", "exit_price", "fees_usdt", "slippage_usdt", "net_pnl"):
                    if not math.isclose(repriced[k], t[k], abs_tol=1e-9):
                        raise ValueError("Base repricing mismatch")
            scenario_rows[name].append({"trade_number": number, "direction": t["direction"],
                                       "entry_utc": t["entry_utc"], "exit_utc": t["exit_utc"],
                                       "contracts": t["contracts"], **repriced})
    scenarios = {}
    for name, records in scenario_rows.items():
        pnl = [r["net_pnl"] for r in records]
        wins, losses = [v for v in pnl if v > 0], [v for v in pnl if v < 0]
        scenarios[name] = {"fee_bps_per_leg": SCENARIOS[name][0], "slippage_bps_per_leg": SCENARIOS[name][1],
                           "trades": len(records), "net_pnl": sum(pnl), "final_equity": config.initial_equity+sum(pnl),
                           "fees_usdt": sum(r["fees_usdt"] for r in records),
                           "slippage_usdt": sum(r["slippage_usdt"] for r in records),
                           "funding_cashflow": sum(r["funding_cashflow"] for r in records),
                           "win_rate": len(wins)/len(pnl) if pnl else None,
                           "profit_factor": sum(wins)/-sum(losses) if losses else None,
                           "geometry_failures": sum(not r["entry_geometry_preserved"] for r in records), "rows": records}
    events = [e for row in rows for e in row["funding_events"]]
    charged = [e for e in events if e["model_charged"]]
    evidence_sources = json.loads((Path(__file__).parent.parent/"docs/okx_d3_1_evidence.json").read_text(encoding="utf-8"))
    return {"version": VERSION, "status": "ARITHMETIC_PASSED_WITH_HISTORICAL_LIMITATIONS",
            "execution_enabled": False, "allow_new_risk": False, "simulated": True,
            "scope": "FIXED_14_TRADE_SCHEDULE_AND_QUANTITY_COST_SENSITIVITY_NOT_NEW_STRATEGY_BACKTEST",
            "source_digest": bt.digest(inputs), "result_digest": bt.digest(original),
            "strategy_fingerprint": fingerprint, "independent_audit": independent, "funding_proof": funding_proof,
            "official_evidence_review": evidence_sources,
            "validation_protocol": original["validation_protocol"], "availability": data["availability"],
            "trades": rows, "scenarios": scenarios,
            "funding_uncertainty": {"charged_events": len(charged),
                "boundary_events": [e for e in events if e["settlement_boundary_ambiguous"]],
                "charged_boundary_cashflow": sum(e["cashflow"] for e in charged if e["settlement_boundary_ambiguous"]),
                "hour_range_min_cashflow": sum(e["if_held_hour_range_min"] for e in charged),
                "hour_range_max_cashflow": sum(e["if_held_hour_range_max"] for e in charged)},
            "limitations": ["No complete historical instrument parameter version series",
                "Hourly MARK open is a proxy, not exact fee-assessment mark; assessment may take up to one minute",
                "Funding position membership at boundaries remains a declared model assumption",
                "Archives are ex-post outcome evidence, not decision-time features; original publication latency unknown",
                "Cost scenarios are assumptions, fixed quantities/schedule, no admission or signal reselection",
                "All viewed history remains development; no account fees, leverage, margin or liquidation verified"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path, help="Completed D.3 directory (inputs.json/result.json)")
    parser.add_argument("--output", type=Path, default=Path.home()/".tradingagents/logs/okx_cost_audit")
    args = parser.parse_args()
    inputs = json.loads((args.run/"inputs.json").read_text(encoding="utf-8"))
    original = json.loads((args.run/"result.json").read_text(encoding="utf-8"))
    result = audit(inputs, original)
    # Content-addressed immutable report, no changes to original reports or ledger.
    root = args.output/bt.digest(result)
    root.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, indent=2, allow_nan=False)
    target = root/"audit.json"
    if target.exists():
        if target.read_text(encoding="utf-8") != content:
            raise ValueError("Existing audit content mismatch")
    else:
        with target.open("x", encoding="utf-8") as f:
            f.write(content)
    for name, records in [("trades", result["trades"]), *[(n, s["rows"]) for n, s in result["scenarios"].items()]]:
        path = root/(name+".csv")
        if not path.exists() and records:
            with path.open("x", newline="", encoding="utf-8-sig") as f:
                export = [{k: v for k, v in r.items() if k != "funding_events"} for r in records]
                writer = csv.DictWriter(f, fieldnames=list(export[0]))
                writer.writeheader(); writer.writerows(export)
    report = root/"report.md"
    if not report.exists():
        lines = ["# D.3.1 historical cost audit", "", "execution_enabled=False; all viewed data is DEVELOPMENT.", "",
                 "Baseline replays the original ledger exactly; every trade passes independent arithmetic checks.",
                 "Cost scenarios FIX original 14 trade times, directions, quantities and exit rules. They are post-trade cost sensitivity, not new strategy backtests or actual account fees.", "",
                 "| Scenario | Fee bps/leg | Slip bps/leg | Fees USDT | Embedded slippage | Funding proxy | Net USDT |",
                 "|---|---:|---:|---:|---:|---:|---:|"]
        for name, s in result["scenarios"].items():
            lines.append(f"| {name} | {s['fee_bps_per_leg']} | {s['slippage_bps_per_leg']} | {s['fees_usdt']:.8f} | {s['slippage_usdt']:.8f} | {s['funding_cashflow']:.8f} | {s['net_pnl']:.8f} |")
        lines += ["", "| # | Entry UTC | Direction | Contracts | Fees | Slippage | Funding proxy | Net |",
                  "|---|---|---|---:|---:|---:|---:|---:|"]
        for t in result["trades"]:
            lines.append(f"| {t['trade_number']} | {t['entry_utc']} | {t['direction']} | {t['contracts']} | {t['fees_usdt']:.8f} | {t['slippage_usdt']:.8f} | {t['funding_cashflow']:.8f} | {t['net_pnl']:.8f} |")
        lines += ["", "## Evidence and limitations", ""] + ["- "+s for s in result["limitations"]]
        lines += ["", "Funding audit (hourly range is an ex-post valuation sensitivity, not a confidence interval or exact one-minute price bound):", "", json.dumps(result["funding_uncertainty"], indent=2), ""]
        for source in result["official_evidence_review"]["sources"]:
            lines += ["- "+source["finding"]+" Source: "+source["url"]]
        lines += ["", "D.4: suitable only for a small engineering pilot under explicit historical/PIT assumptions. Not sufficient to certify point-in-time model benefit or statistical advantage. No D.4 executed."]
        with report.open("x", encoding="utf-8") as f:
            f.write("\n".join(lines)+"\n")
    print(json.dumps({"report": str(root), "status": result["status"],
                      "scenarios": {n: {k: v for k, v in s.items() if k != "rows"} for n, s in result["scenarios"].items()},
                      "execution_enabled": False}, indent=2))


if __name__ == "__main__":
    main()
