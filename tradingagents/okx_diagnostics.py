"""D.2 observational funnel and resumable public-history batches.
No strategy tuning, authenticated API, account, monitoring or execution.
"""
from __future__ import annotations
import argparse
import copy
import csv
import json
import math
from decimal import Decimal, ROUND_FLOOR
from collections import Counter
from datetime import datetime, UTC
from pathlib import Path
from tradingagents import okx_backtest as bt
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv, okx_multitimeframe as mtf
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.errors import VendorUnavailableError

D2_VERSION="okx-funnel-v1"


def decision_diagnostic(bundle, decision, config):
    """Observational side checks do NOT add filters to the frozen strategy.
    4h structure = original pivot candidates and valid numeric template geometry;
    HH/HL etc are descriptive, never secretly made a new hard requirement.
    """
    templates=bt.plans.candidates(bundle,config)
    evidence={}
    for p,a in bundle["timeframe_analysis"].items():
        evidence[p]={k:a[k] for k in ("trend","pivot_structure","close","indicators","sma50_change_5_bars","previous_range","support_candidates","resistance_candidates","latest_close_ms")}
    directions={}
    for side,wanted in (("LONG","UP"),("SHORT","DOWN")):
        key=side+"_BREAKOUT_RETEST"
        structure=key in templates
        geometry=False
        if structure:
            try: bt.plans.validate_prices(templates[key],bundle["instrument"]); geometry=True
            except ValueError: pass
        checks={"data_quality":True,"trend_1d":evidence["1d"]["trend"]==wanted,
            "structure_4h":evidence["4h"]["trend"]==wanted and structure and geometry,
            "direction_1h":evidence["1h"]["trend"]==wanted,
            "resonance":bundle["confluence"]["state"]=="TREND_RESONANCE" and bundle["confluence"]["primary_regime"]==wanted,
            "candidate":bool(decision["candidate"] and decision["candidate"]["direction"]==side)}
        directions[side]={"checks":checks,"template_available":structure,"geometry_valid":geometry,
            "unpassed_conditions":[k for k,v in checks.items() if not v]}
    return {"data_quality":True,"evidence":evidence,"directions":directions,
        "confluence":bundle["confluence"]["state"],"exclusion_reason":None if decision["candidate"] else decision["reason"],
        "confirmation_scope":"1h direction is observed at research cutoff; future breakout/retest is counted only from actual event replay"}


def funnel_report(decisions, events):
    stages=("data_quality","trend_1d","structure_4h","direction_1h","resonance","candidate")
    side_reports={}
    for side in ("LONG","SHORT"):
        previous=len(decisions); running=0; table=[]
        for index,stage in enumerate(stages):
            def checks(d):return d.get("diagnostic",{}).get("directions",{}).get(side,{}).get("checks",{})
            independent=sum(bool(checks(d).get(stage,False)) for d in decisions)
            running=sum(all(checks(d).get(s,False) for s in stages[:index+1]) for d in decisions)
            table.append({"stage":stage,"independent_pass":independent,"sequential_pass":running,
                "all_decisions":len(decisions),"pass_rate_all":independent/len(decisions) if decisions else None,
                "pass_rate_after_prior":running/previous if previous else None})
            previous=running
        side_reports[side]=table
    counts=Counter(e["type"] for e in events)
    pending=counts["PENDING_BREAKOUT"]; breakout=counts["PENDING_RETEST"]; retest=counts["ENTRY_ELIGIBLE"]
    groups={}
    traces={}
    for e in events:
        if e.get("snapshot_id"):traces.setdefault(e["snapshot_id"],[]).append(e)
    for d in decisions:
        diag=d.get("diagnostic",{})
        diag["lifecycle_events"]=[{k:e.get(k) for k in ("ts","type","reason","last_closed_price","entry_boundary","invalidation_price","breakout_confirmed_ms","retest_confirmed_ms")} for e in traces.get(d["snapshot_id"],[])]
        types=[e["type"] for e in diag["lifecycle_events"]]
        diag["outcome"]=("DATA_INVALID" if not diag.get("data_quality") else "NO_CANDIDATE" if not d["candidate"] else
            "FILLED" if "ENTRY" in types else "SKIPPED_ACTIVE" if "DECISION_SKIPPED" in types else
            "INVALIDATED" if "INVALIDATED" in types else "EXPIRED" if "EXPIRED" in types else
            "ENTRY_REJECTED" if "ENTRY_REJECTED" in types else "UNFILLED_WINDOW_END")
        diag["confirmation_failure"]=("RETEST_WINDOW_EXPIRED" if "PENDING_RETEST" in types else "BREAKOUT_WINDOW_EXPIRED") if "EXPIRED" in types else None
        period=bt.iso(d["cutoff_ms"])[:10]
        g=groups.setdefault(period,{"decisions":0,"candidates":0,"outcomes":{}})
        g["decisions"]+=1;g["candidates"]+=bool(d["candidate"])
        g["outcomes"][diag["outcome"]]=g["outcomes"].get(diag["outcome"],0)+1
    candidate_skipped=sum(bool(d["candidate"]) and any(e["type"]=="DECISION_SKIPPED" for e in traces.get(d["snapshot_id"],[])) for d in decisions)
    return {"candidate_skipped_active":candidate_skipped,"noncandidate_skipped_active":counts["DECISION_SKIPPED"]-candidate_skipped,
        "version":D2_VERSION,"decisions":len(decisions),"data_valid":sum(d.get("diagnostic",{}).get("data_quality",False) for d in decisions),
        "side_funnels":side_reports,"wait_reasons":dict(Counter(d["reason"] for d in decisions if not d["candidate"])),
        "trend_distribution":{p:dict(Counter(d.get("diagnostic",{}).get("evidence",{}).get(p,{}).get("trend","DATA_INVALID") for d in decisions)) for p in mtf.PERIODS},
        "long_candidates":sum(bool(d["candidate"] and d["candidate"]["direction"]=="LONG") for d in decisions),
        "short_candidates":sum(bool(d["candidate"] and d["candidate"]["direction"]=="SHORT") for d in decisions),
        "events":dict(counts),"breakout_success_rate":breakout/pending if pending else None,
        "retest_success_rate":retest/breakout if breakout else None,"fill_rate":counts["ENTRY"]/retest if retest else None,
        "by_utc_day":groups,"scope":"Independent observations and sequential funnel denominators are explicit; not alternate strategy returns"}


class CachedPublicClient(okx.OKXPublicClient):
    """Immutable historical GET pages only; dynamic metadata/time never cached.
    Each saved page binds request+payload hash. Successful pages survive failure.
    Same request on retry/resume makes no network call; corrupt cache fails closed.
    """
    def __init__(self, directory, upstream=None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.upstream=upstream or okx.OKXPublicClient()
        self.hits=self.requests=0
    def get(self,path,params=None):
        if path not in okx.PUBLIC_PATHS:raise ValueError("Non-public path blocked")
        if path not in {"/api/v5/market/history-candles",deriv.FUNDING_HISTORY,deriv.MARK_HISTORY,"/api/v5/public/market-data-history"}:
            return self.upstream.get(path,params)
        request={"base":okx.BASE_URL,"path":path,"params":params or {}}
        file=self.directory/(bt.digest(request)+".json")
        with locked(file):
            if file.exists():
                obj=json.loads(file.read_text(encoding="utf-8"))
                if obj["request"]!=request or obj["payload_hash"]!=bt.digest(obj["rows"]):raise ValueError("Corrupt history cache")
                self.hits+=1;return obj["rows"]
            rows=self.upstream.get(path,params);self.requests+=1
            # Do not freeze an empty response (e.g. transient archive unavailability).
            if rows:
                obj={"request":request,"rows":rows,"payload_hash":bt.digest(rows),"fetched_at_utc":bt.iso(okx.utc_ms())}
                temp=file.with_suffix(".tmp");temp.write_text(json.dumps(obj,allow_nan=False),encoding="utf-8");temp.replace(file)
            return rows


def verify_funding_evidence(funding,evidence,start,end):
    if not evidence or evidence.get("path")!=deriv.FUNDING_HISTORY:raise ValueError("Missing paginated funding evidence")
    cursor=evidence["initial_cursor"];rows=[]
    for page in evidence["pages"]:
        if page["after"]!=cursor or not page["rows"] or bt.digest(page["rows"])!=page["hash"]:
            raise ValueError("Broken funding pagination evidence")
        stamps=[int(r["fundingTime"]) for r in page["rows"]]
        if len(stamps)>100 or len(set(stamps))!=len(stamps) or max(stamps)>=cursor or stamps!=sorted(stamps,reverse=True):
            raise ValueError("Funding cursor did not move strictly backward")
        # All non-final pages must be full: no silently skipped response tail.
        if page is not evidence["pages"][-1] and len(stamps)!=100:raise ValueError("Incomplete interior funding page")
        cursor=min(stamps);rows.extend(page["rows"])
    parsed=deriv.parse_funding_history(rows,bt.SYMBOL,evidence["server_ms"])["records"]
    if parsed!=funding:raise ValueError("Funding records differ from raw pagination evidence")
    times=[r["ts"] for r in parsed]
    if not times or min(times)>start or max(times)<end:raise ValueError("Funding does not bracket requested window")
    # Hourly execution cannot resolve subhour settlements; block rather than round.
    if any(t%bt.HOUR for t in times):raise ValueError("Subhour funding cannot be simulated by hourly ledger")
    selected=[t for t in times if start-8*bt.HOUR<=t<=end+8*bt.HOUR]
    if any(b-a>8*bt.HOUR for a,b in zip(selected,selected[1:])):raise ValueError("Funding gap exceeds supported maximum event gap")
    return dict(Counter(str((b-a)/bt.HOUR) for a,b in zip(times,times[1:])))


def fetch_expanded(start,end,cache,client=None):
    if start%bt.HOUR or end%bt.HOUR or not 0<end-start<=90*24*bt.HOUR:raise ValueError("Use aligned expanded window <=90 days")
    cached=CachedPublicClient(cache,client)
    server=int(cached.get("/api/v5/public/time")[0]["ts"])
    if end+bt.HOUR>server:raise ValueError("Historical terminal bar not closed")
    info=cached.instrument(bt.SYMBOL)
    data={"symbol":bt.SYMBOL,"server_ms":server,"instrument":info,"candles":{},"coverage":{},"source":"OKX cached public REST"}
    # Candle cursors pinned to end; page keys stable across interrupted invocations.
    for p in mtf.PERIODS:
        step=okx.BARS[p][1];count=402+(end-start+bt.HOUR+step-1)//step
        rows=cached.candles(bt.SYMBOL,p,end+bt.HOUR,count)
        frame,_=okx.validate_candles(rows,p,end+bt.HOUR,250,count,0)
        data["candles"][p]=[[int(r.ts),r.Open,r.High,r.Low,r.Close,r.VolumeContracts,r.Volume,r.VolumeQuote,"1"] for r in frame.itertuples()]
        data["coverage"][p]={"count":len(frame),"first_open_utc":bt.iso(int(frame.ts.iloc[0])),"last_close_utc":bt.iso(int(frame.ts.iloc[-1])+step)}
    # Anchor to end+8h, not changing wall-clock. The whole upper bracket must exist.
    initial=end+8*bt.HOUR+1
    if initial>server:initial=server+1
    cursor=initial;pages=[]
    for _ in range(40):
        rows=cached.get(deriv.FUNDING_HISTORY,{"instId":bt.SYMBOL,"after":str(cursor),"limit":"100"})
        if not rows:break
        stamps=[int(r["fundingTime"]) for r in rows]
        if max(stamps)>=cursor:raise ValueError("Funding pagination did not advance")
        pages.append({"after":cursor,"rows":rows,"hash":bt.digest(rows)})
        cursor=min(stamps)
        if cursor<=start:break
    evidence={"path":deriv.FUNDING_HISTORY,"initial_cursor":initial,"server_ms":server,"pages":pages}
    data["funding"]=deriv.parse_funding_history([r for p in pages for r in p["rows"]],bt.SYMBOL,server)["records"]
    data["funding_evidence"]=evidence
    if not data["funding"]:raise ValueError("No realized funding archive available")
    covered_start=max(start,data["funding"][0]["ts"])
    covered_end=min(end,data["funding"][-1]["ts"])
    if covered_start>=covered_end:raise ValueError("No overlapping realized funding window")
    intervals=verify_funding_evidence(data["funding"],evidence,covered_start,covered_end)
    data["coverage"]["funding"]={"count":len(data["funding"]),"first_utc":bt.iso(data["funding"][0]["ts"]),"last_utc":bt.iso(data["funding"][-1]["ts"]),"observed_interval_hours":intervals,
        "coverage_basis":"Validated API page chain and observed event timestamps; not an assertion that the vendor archive is infallible"}
    data["cache_statistics"]={"hits":cached.hits,"network_history_requests":cached.requests}
    return data


def subset(data,start,end):
    result=copy.deepcopy(data)
    for p in mtf.PERIODS:
        step=okx.BARS[p][1]
        result["candles"][p]=[r for r in data["candles"][p] if int(r[0])+step<=end+bt.HOUR]
        rows=result["candles"][p]
        result["coverage"][p]={"count":len(rows),"first_open_utc":bt.iso(int(rows[0][0])),"last_close_utc":bt.iso(int(rows[-1][0])+step)}
    return result


def run_expanded(data,start,end,output, *, progress=None):
    requested={"start_utc":bt.iso(start),"end_utc":bt.iso(end)}
    if start%bt.HOUR or end%bt.HOUR or not 0<end-start<=90*24*bt.HOUR:raise ValueError("Invalid expanded window")
    if any(len(data["candles"].get(p,[]))<400 for p in mtf.PERIODS):raise ValueError("Each period needs 400 warmup observations for expansion")
    warmup_start=max(sorted(int(r[0]) for r in data["candles"][p])[399]+okx.BARS[p][1] for p in mtf.PERIODS)
    first=max(start,warmup_start,data["funding"][0]["ts"])
    last=min(end,max(int(r[0]) for r in data["candles"]["1h"]),data["funding"][-1]["ts"])
    start=((first+4*bt.HOUR-1)//(4*bt.HOUR))*4*bt.HOUR
    end=(last//bt.HOUR)*bt.HOUR
    if start>=end:raise ValueError("No common warmup/candle/funding window")
    config=bt.SimulationConfig(taker_fee_bps=5,slippage_bps=2,
        funding_validation="PAGINATED_OBSERVED_EVENTS",contract_parameter_assumption="CURRENT_SPEC_ASSUMED_CONSTANT_SHORT_WINDOW")
    # Freeze D.1 numeric rules explicitly; expanding history is not a parameter search.
    frozen={"strategy":bt.VERSION,"initial_equity":10000,"notional_usdt":1000,"max_holding_bars":12,
        "confirmation_bars":6,"taker_fee_bps":5,"slippage_bps":2,"target_atr":3,"stop_atr_buffer":0.25,
        "warmup_min_bars":250,"indicator_history_bars":400,"decision_interval_hours":4,"structure_rules":mtf.structure_rules({})}
    batches=[];all_decisions=[];all_events=[]
    for left in range(start,end,14*24*bt.HOUR):
        right=min(end,left+14*24*bt.HOUR)
        selected=subset(data,left,right)
        try:
            result=bt.run_data(selected,left,right,config,diagnostics=True)
            if result["metrics"] is not None:result["trade_audit"]=audit_trades(selected,result,config)
            inputs={"data":selected,"start":left,"end":right,"config":config.model_dump(),"rule_source_hash":result.get("rule_source_hash"),"d2_version":D2_VERSION,"d2_source_hash":bt.digest(Path(__file__).read_text(encoding="utf-8"))}
            root=bt.save_run(Path(output)/"batches",inputs,result)
            batches.append({"start_utc":bt.iso(left),"end_utc":bt.iso(right),"status":result["status"],"metrics":result["metrics"],"report":str(root)})
            all_decisions.extend(result["decisions"])
            all_events.extend(result["events"])
            if progress:progress(batches[-1])
        except (ValueError,okx.DataQualityError) as exc:
            batches.append({"start_utc":bt.iso(left),"end_utc":bt.iso(right),"status":"DATA_INVALID","metrics":None,"reason":str(exc)})
            if progress:progress(batches[-1])
            all_decisions.extend({"cutoff_ms":t,"snapshot_id":bt.digest({"cutoff":t,"error":str(exc)}),"candidate":None,"reason":"DATA_INVALID",
                "diagnostic":{"data_quality":False,"error":str(exc),"directions":{}}} for t in range(left,right,4*bt.HOUR))
    report={"requested_window":requested,"available_windows":{
        "candles_with_400_bar_warmup":{"start_utc":bt.iso(warmup_start),"end_utc":bt.iso(max(int(r[0]) for r in data["candles"]["1h"]))},
        "settled_funding_events":{"start_utc":bt.iso(data["funding"][0]["ts"]),"end_utc":bt.iso(data["funding"][-1]["ts"])},
        "conditional_simulation":{"start_utc":bt.iso(start),"end_utc":bt.iso(end)},
        "historically_verified_contract_parameters":False,"historically_verified_taker_fees":False},"version":D2_VERSION,"execution_enabled":False,"frozen_rules":frozen,"coverage":data["coverage"],
        "batch_scope":"Independent <=14-day scenarios; cash and pending signals RESET each batch; NOT a continuous portfolio return",
        "batches":batches,"signal_funnel":funnel_report(all_decisions,all_events),"decisions":all_decisions,
        "limitations":["Current contract specs are an explicit unverified assumption in EACH independent short batch",
            "Funding completeness relies on raw API pagination evidence and an 8h maximum-gap safety bound, not a fixed 8h schedule",
            "Missing a shorter-interval settlement may not be detectable if the vendor itself omits it; raw page evidence retained",
            "No historical OI/orderbook used; no leverage/margin/liquidation test; no LLM selection evaluation",
            "No automatic optimization and no statistical edge claim"]}
    report["totals"]={"valid_batches":sum(b["metrics"] is not None for b in batches),"invalid_batches":sum(b["metrics"] is None for b in batches),
        **{k:sum(b["metrics"][k] for b in batches if b["metrics"] is not None) for k in ("trades","long_trades","short_trades","fees_usdt","funding_cashflow_usdt","slippage_usdt")}}
    report["status"]="PARTIAL" if report["totals"]["invalid_batches"] else "COMPLETED"
    report["source_hash"]=bt.digest({"d1":Path(bt.__file__).read_text(encoding="utf-8"),"d2":Path(__file__).read_text(encoding="utf-8")})
    key=bt.digest({"data":data,"requested":requested,"start":start,"end":end,"frozen":frozen,"source":report["source_hash"]})
    root=Path(output)/key;root.mkdir(parents=True,exist_ok=True)
    with locked(root/"report"):
        file=root/"diagnostics.json"
        if file.exists():
            if json.loads(file.read_text(encoding="utf-8"))!=report:raise ValueError("Expanded rerun differs")
        else:
            for name,value in (("source.json",{"data":data,"start":start,"end":end}),):
                temp=root/(name+".tmp");temp.write_text(json.dumps(value,indent=2,allow_nan=False),encoding="utf-8");temp.replace(root/name)
            with (root/"decisions.csv").open("w",newline="",encoding="utf-8-sig") as f:
                writer=csv.DictWriter(f,fieldnames=["cutoff_utc","reason","candidate","outcome","diagnostic"]);writer.writeheader()
                for d in all_decisions:writer.writerow({"cutoff_utc":bt.iso(d["cutoff_ms"]),"reason":d["reason"],"candidate":d["candidate"]["direction"] if d["candidate"] else None,"outcome":d["diagnostic"]["outcome"],"diagnostic":json.dumps(d["diagnostic"])})
            (root/"report.md").write_text(render_report(report),encoding="utf-8")
            temp=root/"diagnostics.json.tmp"
            temp.write_text(json.dumps(report,indent=2,allow_nan=False),encoding="utf-8");temp.replace(file)
    return root,report




def render_report(report):
    f=report["signal_funnel"]
    lines=["# OKX BTC-USDT-SWAP D.2 signal diagnostics", "", "execution_enabled=False; simulation only.",
        "", "Independent <=14-day scenarios, reset each batch. No continuous portfolio return is claimed.",
        "Current contract parameters and Taker fees remain assumptions, not verified historical settings.", "", "## Coverage", "", json.dumps(report["available_windows"],indent=2),
        "", "## Signal funnel", "", f"Decisions: {f['decisions']}; data-valid: {f['data_valid']}", "",
        "| Side | Stage | Independent passes | Sequential passes | Conditional pass rate |", "|---|---|---:|---:|---:|"]
    for side,stages in f["side_funnels"].items():
        for stage in stages:
            rate=stage["pass_rate_after_prior"]
            lines.append(f"| {side} | {stage['stage']} | {stage['independent_pass']} | {stage['sequential_pass']} | {rate if rate is not None else 'N/A'} |")
    lines += ["", "## Outcomes", "", json.dumps({k:f[k] for k in ("long_candidates","short_candidates","candidate_skipped_active","noncandidate_skipped_active","events","wait_reasons","breakout_success_rate","retest_success_rate","fill_rate")},indent=2),
        "", "## Independent batch performance", "", "| UTC start | UTC end | Status | Trades | Net PnL USDT | Max drawdown |", "|---|---|---|---:|---:|---:|"]
    for b in report["batches"]:
        m=b["metrics"]
        lines.append(f"| {b['start_utc']} | {b['end_utc']} | {b['status']} | {m['trades'] if m else 'N/A'} | {m['net_pnl'] if m else 'N/A'} | {m['max_drawdown'] if m else 'N/A'} |")
    lines += ["", "Totals below are event/cost sums only, not a compounded portfolio result:", json.dumps(report["totals"],indent=2),
        "", "## Limitations", "", *report["limitations"], "", "Small or absent trade samples do not establish a statistical advantage. No automatic tuning or Reflection injection.",
        "", "Full per-decision evidence: decisions.csv and diagnostics.json. Per-trade records, equity and audit: each batch result.json/trades.csv/equity.csv."]
    return "\n".join(lines)


def audit_trades(data,result,config, *, funding_prices=None):
    """Independent per-trade confirmation/fill/fee/funding arithmetic audit.
    Does not manufacture or adjust a fill. Any discrepancy blocks the batch.
    """
    rows={int(r[0]):r for r in data["candles"]["1h"]}
    decisions={d["snapshot_id"]:d for d in result["decisions"]}
    info=data["instrument"];fee=config.taker_fee_bps/10000;slip=config.slippage_bps/10000
    def equal(a,b):
        if not math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-7):raise ValueError("Trade audit arithmetic mismatch")
    for t in result["trades"]:
        d=decisions[t["snapshot_id"]];plan=d["candidate"];sign=1 if t["direction"]=="LONG" else -1
        entry=t["entry_ms"];exit=t["exit_ms"]
        confirmation=bt.normalized([{"ts":ts,"Open":r[1],"High":r[2],"Low":r[3],"Close":r[4]} for ts,r in sorted(rows.items()) if d["cutoff_ms"]<=ts<entry])
        signal=bt.evaluate_signal(plan,bt.timing(d["cutoff_ms"],{"okx_plan_confirmation_bars":config.confirmation_bars}),confirmation,entry)
        if signal.state!="ENTRY_ELIGIBLE" or signal.next_tradable_ms!=entry:raise ValueError("Trade audit: premature or delayed entry")
        equal(t["entry_price"],bt.plans.round_tick(float(rows[entry][1])*(1+sign*slip),info["tickSz"],sign>0))
        lot=Decimal(info["lotSz"])
        size=(Decimal(str(config.notional_usdt/t["entry_price"]/deriv.multiplier(info)))/lot).to_integral_value(rounding=ROUND_FLOOR)*lot
        equal(t["contracts"],float(size));equal(t["base_quantity"],float(size)*deriv.multiplier(info))
        intrabar=t["exit_reason"] in {"STOP","TARGET","STOP_FIRST_AMBIGUOUS"}
        if intrabar:
            r=rows[exit-bt.HOUR];o,h,l=map(float,r[1:4])
            stop_hit=l<=t["stop"] if sign>0 else h>=t["stop"]
            target_hit=h>=t["target"] if sign>0 else l<=t["target"]
            if t["exit_reason"]=="TARGET" and (stop_hit or not target_hit):raise ValueError("Trade audit: wrong target priority")
            if t["exit_reason"]!="TARGET" and not stop_hit:raise ValueError("Trade audit: stop not touched")
            reference=t["target"] if t["exit_reason"]=="TARGET" else (min(o,t["stop"]) if sign>0 else max(o,t["stop"]))
        else:
            reference=float(rows[exit][1])
            if t["exit_reason"]=="MAX_HOLDING" and exit!=entry+config.max_holding_bars*bt.HOUR:raise ValueError("Trade audit: maximum holding mismatch")
        equal(t["exit_reference"],reference)
        equal(t["exit_price"],bt.plans.round_tick(reference*(1-sign*slip),info["tickSz"],sign<0))
        expected_funding=0.
        for event in data["funding"]:
            ts=event["ts"]
            if entry<ts and (ts<exit if intrabar else ts<=exit):
                price=float(rows[ts][1]) if funding_prices is None else funding_prices[ts]
                expected_funding+=-sign*t["base_quantity"]*price*event["realized_rate"]
        equal(t["funding_cashflow"],expected_funding)
        expected_fees=t["base_quantity"]*(t["entry_price"]+t["exit_price"])*fee
        equal(t["fees_usdt"],expected_fees)
        expected_slip=t["base_quantity"]*(abs(t["entry_price"]-float(rows[entry][1]))+abs(t["exit_price"]-reference))
        equal(t["slippage_usdt"],expected_slip)
        equal(t["net_pnl"],sign*t["base_quantity"]*(t["exit_price"]-t["entry_price"])-expected_fees+expected_funding)
    equal(sum(t["net_pnl"] for t in result["trades"]),result["metrics"]["final_equity"]-config.initial_equity)
    return {"status":"PASSED","trades_checked":len(result["trades"]),"checks":["closed-bar confirmation","next-open fill","quantity/tick","exit priority","fees","actual settlement funding","slippage not double counted","cash reconciliation"]}


def main():
    parser=argparse.ArgumentParser(description="Frozen BTC research signal diagnostics; no execution")
    parser.add_argument("--start",required=True);parser.add_argument("--end",required=True)
    parser.add_argument("--assume-current-contract-spec",action="store_true",required=True)
    base=Path.home()/".tradingagents"/"logs"/"okx_diagnostics"
    parser.add_argument("--cache",default=str(base/"public_cache"));parser.add_argument("--output",default=str(base/"runs"))
    parser.add_argument("--input",help="Offline source.json from a prior diagnostic run")
    args=parser.parse_args()
    def parse(v):
        d=datetime.fromisoformat(v.replace("Z","+00:00"))
        if d.tzinfo is None:raise ValueError("Explicit UTC offset required")
        return int(d.timestamp()*1000)
    try:
        start,end=parse(args.start),parse(args.end)
        data=json.loads(Path(args.input).read_text(encoding="utf-8"))["data"] if args.input else fetch_expanded(start,end,args.cache)
        import sys
        root,report=run_expanded(data,start,end,args.output,progress=lambda b:print(json.dumps({"batch":b["start_utc"],"status":b["status"],"metrics":b["metrics"]}),file=sys.stderr,flush=True))
        print(json.dumps({"status":report["status"],"report":str(root),"totals":report["totals"],"funnel_events":report["signal_funnel"]["events"],"long_candidates":report["signal_funnel"]["long_candidates"],"short_candidates":report["signal_funnel"]["short_candidates"],"execution_enabled":False},indent=2))
        if report["totals"]["invalid_batches"]:raise SystemExit(2)
    except (ValueError,KeyError,okx.DataQualityError,VendorUnavailableError) as exc:
        print(json.dumps({"status":"DATA_INVALID","reason":str(exc),"execution_enabled":False}));raise SystemExit(2)


if __name__=="__main__":main()
