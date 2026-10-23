"""D.1 offline BTC perpetual ledger, distinct from decision-quality backtest.
No authenticated client, account, leverage, liquidation or executable orders.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, UTC
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Literal
from pydantic import Field
from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents.dataflows import perpetual_plan as plans
from tradingagents.dataflows.plan_lifecycle import HOUR, HistoricalAssumptions, evaluate_signal, timing, prepare_historical_signal
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv, okx_multitimeframe as mtf
from tradingagents.dataflows.files import locked

SYMBOL = "BTC-USDT-SWAP"
VERSION = "okx-candle-ledger-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def iso(ms):
    return datetime.fromtimestamp(ms/1000, UTC).isoformat()


class SimulationConfig(ResearchModel):
    initial_equity: float = Field(default=10000, gt=0)
    notional_usdt: float = Field(default=1000, gt=0)
    taker_fee_bps: float = Field(ge=0, le=100)
    slippage_bps: float = Field(ge=0, le=100)
    max_holding_bars: int = Field(default=12, ge=1, le=48)
    confirmation_bars: int = Field(default=6, ge=2, le=48)
    # Explicit historical schedule ASSUMPTION; each expected event must exist.
    funding_interval_hours: Literal[1, 2, 4, 8] = 8
    contract_parameter_assumption: Literal["CURRENT_SPEC_ASSUMED_CONSTANT_SHORT_WINDOW", "CURRENT_SPEC_UNVERIFIED_HISTORICAL_SCENARIO"]
    funding_validation: Literal["DECLARED_FIXED_INTERVAL", "PAGINATED_OBSERVED_EVENTS", "OFFICIAL_ARCHIVE_PLUS_REST"] = "DECLARED_FIXED_INTERVAL"
    execution_enabled: Literal[False] = False


def normalized(frame):
    return [{"ts":int(r["ts"]), **{k.lower():float(r[k]) for k in ("Open","High","Low","Close")}, "confirm":"1"} for r in frame]


def validate_execution(bars, funding, start, end, config, funding_evidence=None, *, max_window_days=14):
    if end <= start or start % HOUR or end % HOUR or end-start > max_window_days*24*HOUR:
        raise ValueError(f"Use aligned UTC window of at most {max_window_days} days")
    expected=list(range(start,end+HOUR,HOUR))
    if [b["ts"] for b in bars] != expected:
        raise ValueError("Execution candles missing, duplicated or out of order (terminal open required)")
    for b in bars:
        values=[float(b[k]) for k in ("open","high","low","close")]
        o,h,l,c=values
        if b["confirm"]!="1" or not all(math.isfinite(v) and v>0 for v in values) or not l<=min(o,c)<=max(o,c)<=h:
            raise ValueError("Invalid or unconfirmed execution candle")
    if config.funding_validation == "OFFICIAL_ARCHIVE_PLUS_REST":
        from tradingagents.okx_continuous import verify_archive_evidence
        verify_archive_evidence(funding, funding_evidence, start, end)
        return
    if config.funding_validation == "PAGINATED_OBSERVED_EVENTS":
        from tradingagents.okx_diagnostics import verify_funding_evidence
        verify_funding_evidence(funding, funding_evidence, start, end)
        return
    period=config.funding_interval_hours*HOUR
    times=[f["ts"] for f in funding]
    # Require one bracketing event each side, plus every UTC-aligned assumed settlement.
    lo=(start//period)*period
    hi=((end+period-1)//period)*period
    required=list(range(lo,hi+period,period))
    if len(set(times))!=len(times) or times!=sorted(times):
        raise ValueError("Duplicate or unordered funding")
    if [t for t in times if lo<=t<=hi] != required:
        raise ValueError("Realized funding coverage incomplete or schedule differs from declared assumption")
    if any(not math.isfinite(f["realized_rate"]) for f in funding):
        raise ValueError("Invalid realized funding")


def simulate(bars, funding, decisions, info, config, start, end, *, funding_evidence=None, max_window_days=14, funding_prices=None):
    """Fixed plans or historical regenerated plans; one position, no borrowing.

    Funding: positions carried into settlement-hour OPEN, before scheduled open exits.
    New open entries do not receive/pay that boundary; intrabar exits are not
    carried into the next boundary. Trade-open price is an explicit mark proxy.
    Prices include adverse slippage; separately reported slippage is NOT deducted twice.
    Stop/target gaps: worse stop at open, target capped at target (no improvement).
    """
    config=SimulationConfig.model_validate(config)
    validate_execution(bars,funding,start,end,config,funding_evidence,max_window_days=max_window_days)
    okx.validate_instrument(info,SYMBOL)
    if config.notional_usdt>config.initial_equity:
        raise ValueError("No leverage: notional exceeds initial equity")
    decision_map={}
    for item in decisions:
        cutoff=item["cutoff_ms"]
        if cutoff in decision_map or cutoff%HOUR or not start<=cutoff<end:
            raise ValueError("Invalid or duplicate decision cutoff")
        if item.get("evidence_latest_ms",cutoff)>cutoff or not item.get("snapshot_id"):
            raise ValueError("Future or unidentified research evidence")
        if item.get("candidate"):
            plans.validate_prices(item["candidate"],info)
        decision_map[cutoff]=item
    cash=config.initial_equity
    position=pending=None
    events=[]; trades=[]; curve=[]
    schedule={f["ts"]:f["realized_rate"] for f in funding}
    fee_rate=config.taker_fee_bps/10000; slip=config.slippage_bps/10000
    multiplier=deriv.multiplier(info)
    config_clock={"okx_plan_confirmation_bars":config.confirmation_bars}
    def event(ts,kind,**details):
        active=pending or position
        if active: details.setdefault("snapshot_id",active["snapshot_id"])
        events.append({"event_id":len(events),"ts":ts,"utc":iso(ts),"type":kind,**details})
    def close(ts,reference,reason):
        nonlocal cash,position
        p=position
        exit_price=plans.round_tick(reference*(1-p["sign"]*slip),info["tickSz"],p["sign"]<0)
        gross=p["sign"]*p["base_quantity"]*(exit_price-p["entry_price"])
        exit_fee=p["base_quantity"]*exit_price*fee_rate
        cash+=gross-exit_fee
        trade={**p,"exit_ms":ts,"exit_utc":iso(ts),"exit_price":exit_price,"exit_reference":reference,
            "exit_reason":reason,"fees_usdt":p["entry_fee"]+exit_fee,
            "slippage_usdt":p["entry_slippage"]+p["base_quantity"]*abs(exit_price-reference),
            "gross_pnl_after_slippage":gross,"net_pnl":gross-p["entry_fee"]-exit_fee+p["funding_cashflow"],
            "resolved_at_ms":ts,"simulated":True,"exit_time_scope":"Intrabar exit known by bar close; position not held into next settlement boundary" if reason in {"STOP","TARGET","STOP_FIRST_AMBIGUOUS"} else "Scheduled boundary open"}
        trades.append(trade); event(ts,"EXIT",trade_index=len(trades)-1,reason=reason,net_pnl=trade["net_pnl"])
        position=None
    for i,b in enumerate(bars):
        ts=b["ts"]; terminal=ts==end
        if position and ts in schedule and position["entry_ms"]<ts:
            funding_price=b["open"] if funding_prices is None else funding_prices.get(ts)
            if funding_price is None or not math.isfinite(funding_price) or funding_price<=0:raise ValueError("Historical funding mark price unavailable; fallback forbidden")
            amount=deriv.funding_cashflow(position["base_quantity"],funding_price,schedule[ts],position["direction"])
            position["funding_cashflow"]+=amount; cash+=amount
            event(ts,"FUNDING",rate=schedule[ts],price_proxy=funding_price,price_source="TRADE_OPEN_PROXY" if funding_prices is None else "HISTORICAL_MARK_OPEN_PROXY",cashflow=amount)
        if position and (terminal or ts>=position["entry_ms"]+config.max_holding_bars*HOUR):
            close(ts,b["open"],"WINDOW_END" if terminal else "MAX_HOLDING")
        if not terminal and not position and pending:
            rows=bars[pending["start_index"]:i]
            signal=evaluate_signal(pending["candidate"],timing(pending["cutoff_ms"],config_clock),rows,ts)
            if signal.state!=pending.get("last_state"):
                event(ts,signal.state,snapshot_id=pending["snapshot_id"],reason=signal.reason,
                    breakout_confirmed_ms=signal.breakout_confirmed_ms,retest_confirmed_ms=signal.retest_confirmed_ms,
                    last_closed_price=rows[-1]["close"] if rows else None,entry_boundary=pending["candidate"]["entry"]["price"],
                    invalidation_price=pending["candidate"]["stop"]["price"])
                pending["last_state"]=signal.state
            if signal.state=="ENTRY_ELIGIBLE":
                plan=pending["candidate"]; sign=1 if plan["direction"]=="LONG" else -1
                price=plans.round_tick(b["open"]*(1+sign*slip),info["tickSz"],sign>0)
                stop=plan["stop"]["price"]; target=plan["target"]["price"]
                if not (stop<price<target if sign==1 else target<price<stop):
                    event(ts,"ENTRY_REJECTED",reason="Gap/slippage outside plan geometry")
                else:
                    lot=Decimal(info["lotSz"])
                    size=(Decimal(str(config.notional_usdt/price/multiplier))/lot).to_integral_value(rounding=ROUND_FLOOR)*lot
                    base=float(size)*multiplier; fee=base*price*fee_rate
                    if size<Decimal(info["minSz"]) or base*price+fee>cash:
                        event(ts,"ENTRY_REJECTED",reason="Minimum size or unlevered cash budget")
                    else:
                        cash-=fee
                        position={"direction":plan["direction"],"sign":sign,"entry_ms":ts,"entry_utc":iso(ts),"entry_price":price,
                            "entry_reference":b["open"],"contracts":float(size),"base_quantity":base,"entry_fee":fee,
                            "entry_slippage":base*abs(price-b["open"]),"stop":stop,"target":target,
                            "funding_cashflow":0.,"snapshot_id":pending["snapshot_id"],"research_cutoff_ms":pending["cutoff_ms"],
                            "market_regime":pending.get("market_regime","FIXED_TEST_PLAN")}
                        event(ts,"ENTRY",direction=position["direction"],contracts=float(size),price=price)
                pending=None
            elif signal.state in {"EXPIRED","INVALIDATED"}:
                pending=None
        if ts in decision_map:
            decision=decision_map[ts]
            if position or pending:
                event(ts,"DECISION_SKIPPED",snapshot_id=decision["snapshot_id"],reason="Single active position or hypothesis")
            elif not decision.get("candidate"):
                event(ts,"WAIT",snapshot_id=decision["snapshot_id"],reason=decision.get("reason","No deterministic candidate"))
            else:
                pending={**decision,"start_index":i,"last_state":"PENDING_BREAKOUT"}
                event(ts,"PENDING_BREAKOUT",snapshot_id=decision["snapshot_id"])
        # Intrabar exit has unknown instant; resolve at close for feedback filtering.
        if position and not terminal:
            p=position; long=p["sign"]==1
            hit_stop=b["low"]<=p["stop"] if long else b["high"]>=p["stop"]
            hit_target=b["high"]>=p["target"] if long else b["low"]<=p["target"]
            if hit_stop:
                reference=min(b["open"],p["stop"]) if long else max(b["open"],p["stop"])
                close(ts+HOUR,reference,"STOP_FIRST_AMBIGUOUS" if hit_target else "STOP")
            elif hit_target:
                close(ts+HOUR,p["target"],"TARGET")
        equity=cash+(position["sign"]*position["base_quantity"]*(b["close"]-position["entry_price"]) if position else 0)
        curve.append({"ts":ts if terminal else ts+HOUR,"utc":iso(ts if terminal else ts+HOUR),"equity":equity})
    if pending: event(end,"UNFILLED_WINDOW_END",snapshot_id=pending["snapshot_id"])
    # Merge identical close/next-open timestamps, terminal settlement takes precedence.
    curve=[{"ts":start,"utc":iso(start),"equity":config.initial_equity}]+list({r["ts"]:r for r in curve}.values())
    pnl=[t["net_pnl"] for t in trades]; wins=[v for v in pnl if v>0]; losses=[v for v in pnl if v<0]
    peak=config.initial_equity; drawdown=0.
    for r in curve:
        peak=max(peak,r["equity"]); drawdown=max(drawdown,(peak-r["equity"])/peak)
    if not math.isclose(cash-config.initial_equity,sum(pnl),abs_tol=1e-7):
        raise ArithmeticError("Ledger reconciliation failed")
    return {"status":"COMPLETED" if trades else "NO_TRADES", "execution_enabled":False,"simulated":True,
        "strategy_version":VERSION,"config":config.model_dump(),"start_utc":iso(start),"end_utc":iso(end),
        "metrics":{"trades":len(trades),"long_trades":sum(t["direction"]=="LONG" for t in trades),
            "short_trades":sum(t["direction"]=="SHORT" for t in trades),"win_rate":len(wins)/len(pnl) if pnl else None,
            "average_win":sum(wins)/len(wins) if wins else None,"average_loss":sum(losses)/len(losses) if losses else None,
            "profit_factor":sum(wins)/-sum(losses) if losses else None,"initial_equity":config.initial_equity,"final_equity":cash,
            "net_pnl":cash-config.initial_equity,"net_return":cash/config.initial_equity-1,"max_drawdown":drawdown,
            "fees_usdt":sum(t["fees_usdt"] for t in trades),"funding_cashflow_usdt":sum(t["funding_cashflow"] for t in trades),
            "slippage_usdt":sum(t["slippage_usdt"] for t in trades)},"trades":trades,"events":events,"equity_curve":curve,
        "limitations":["No leverage, margin or liquidation simulation","Current contract parameters assumed constant, NOT historically verified",
            "Funding settlement notional uses settlement-hour TRADE OPEN proxy, not historical mark price",
            "Explicit UTC funding interval assumption checked against every returned realized settlement; not proof of exchange archive completeness",
            "Hourly close equity drawdown; intrabar equity extremes not measured","No claim of actual liquidity or fillability","Rule-only test, no LLM selection evaluation"]}


def feedback(result, as_of_ms):
    """Separate simulation feedback, never written into stock Memory."""
    return [{"simulation_only":True,"strategy_version":result["strategy_version"],**t} for t in result["trades"] if t["resolved_at_ms"]<=as_of_ms]


def save_run(directory, inputs, result):
    """Content-addressed, atomic completed run; interruption safely reruns, no append accounting."""
    run_id=digest({"version":VERSION,"inputs":inputs})
    root=Path(directory)/run_id; root.mkdir(parents=True,exist_ok=True)
    with locked(root/"run"):
        path=root/"result.json"
        if path.exists():
            previous=json.loads(path.read_text(encoding="utf-8"))
            if previous!=result: raise ValueError("Non-deterministic rerun for same input")
            return root
        for name,value in (("inputs.json",inputs),):
            temp=root/(name+".tmp"); temp.write_text(json.dumps(value,indent=2,allow_nan=False),encoding="utf-8"); temp.replace(root/name)
        for name,records in (("trades.csv",result["trades"]),("equity.csv",result["equity_curve"]),("events.csv",result["events"])):
            with (root/name).open("w",newline="",encoding="utf-8-sig") as f:
                fields=list(dict.fromkeys(k for r in records for k in r)) or (
                    ["entry_utc","exit_utc","direction","contracts","fees_usdt","funding_cashflow","net_pnl"] if name=="trades.csv" else ["event_id","ts","type"])
                writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader(); writer.writerows(records)
        temp=root/"result.json.tmp"
        temp.write_text(json.dumps(result,indent=2,allow_nan=False),encoding="utf-8")
        temp.replace(path)
    return root


def historical_bundle(data, cutoff, config):
    """Regenerate indicators/levels solely from bars closed by decision cutoff.
    Existing validators/gates unchanged; cached public history supplies data.
    """
    children={}; info=data["instrument"]
    okx.validate_instrument(info,SYMBOL)
    fetched=okx.utc_ms()
    for period in mtf.PERIODS:
        step=okx.BARS[period][1]
        available=[r for r in data["candles"][period] if int(r[0])+step<=cutoff]
        df,notes=okx.validate_candles(available,period,cutoff,250,400,0)
        multiplier=deriv.multiplier(info)
        if not ((df.Volume-df.VolumeContracts*multiplier).abs() <= df.Volume.abs()*1e-6+1e-8).all():
            raise ValueError("Historical contract/base volume mismatch")
        records=df.assign(Date=df.Date.map(lambda v:v.isoformat())).to_dict("records")
        child={"source":"OKX cached public history; offline point-in-time reconstruction","exchange":"OKX","symbol":SYMBOL,
            "instrument":info,"interval":period,"cutoff_ms":cutoff,"server_ms":data["server_ms"],"fetched_at_ms":fetched,
            "latest_close_ms":int(df.ts.iloc[-1])+step,"historical":True,"records":records,
            "volume_units":{"Volume":"BTC","VolumeContracts":"contracts","VolumeQuote":"USDT"},"status":"VALID",
            "allow_new_risk":True,"execution_enabled":False,"issues":notes}
        child["snapshot_id"]=okx.fingerprint(child); children[period]=child
    rules=mtf.structure_rules(config)
    analyses={p:mtf.analyze_timeframe(b,rules) for p,b in children.items()}
    if any(not math.isfinite(v) for a in analyses.values() for v in a["indicators"].values()):
        raise ValueError("Non-finite historical indicators")
    root={**children["1h"],"multi_timeframe":True,"timeframes":children,"timeframe_analysis":analyses,
        "confluence":mtf.confluence(analyses),"structure_rules":rules}
    root["snapshot_id"]=okx.fingerprint(root)
    checked=okx.check_bundle(root,config,SYMBOL)
    if not checked["allow_new_risk"]: raise ValueError("Historical bundle rejected by existing gate")
    return checked


def regenerate_decisions(data,start,end,simulation_config, *, diagnostics=False):
    """One decision every 4h, directional resonance only, no majority vote/LLM.
    Stable evidence IDs exclude retrieval wall-clock; full cached input archived.
    """
    config={"okx_multi_timeframe":True,"okx_interval":"1h","okx_min_bars":250,"okx_history_bars":400,
        "okx_max_lag_bars":0,"okx_plan_confirmation_bars":simulation_config.confirmation_bars}
    assumptions=HistoricalAssumptions(taker_fee_bps=simulation_config.taker_fee_bps,
        slippage_bps_per_fill=simulation_config.slippage_bps)
    decisions=[]
    for cutoff in range(start,end,4*HOUR):
        try:
            bundle=historical_bundle(data,cutoff,config)
        except (ValueError, okx.DataQualityError) as exc:
            if not diagnostics: raise
            decisions.append({"cutoff_ms":cutoff,"snapshot_id":digest({"cutoff":cutoff,"error":str(exc)}),"candidate":None,
                "reason":"DATA_INVALID", "diagnostic":{"data_quality":False,"error":str(exc),"directions":{}}})
            continue
        c=bundle["confluence"]
        evidence_id=digest({"cutoff":cutoff,"version":VERSION,"instrument":data["instrument"],
            "rules":bundle["structure_rules"],"bars":{p:b["records"] for p,b in bundle["timeframes"].items()}})
        decision={"cutoff_ms":cutoff,"snapshot_id":evidence_id,"evidence_latest_ms":max(b["latest_close_ms"] for b in bundle["timeframes"].values()),
            "market_regime":c["primary_regime"],"candidate":None,"reason":"No three-timeframe directional resonance"}
        if c["state"]=="TREND_RESONANCE":
            key="LONG_BREAKOUT_RETEST" if c["primary_regime"]=="UP" else "SHORT_BREAKOUT_RETEST"
            templates=plans.candidates(bundle,config)
            if key in templates:
                template=prepare_historical_signal(bundle,config,key,assumptions)
                candidate=template["candidate"]
                try: plans.validate_prices(candidate,data["instrument"])
                except ValueError: decision["reason"]="Historical price geometry invalid"
                else:
                    # Original source identity is regenerated from stable input, never today's research report.
                    candidate={**candidate,**{k:{**candidate[k],"source_snapshot_id":evidence_id} for k in ("entry","stop","target")}}
                    decision.update(candidate=candidate,reason="Verified directional resonance and structural candidate")
            else: decision["reason"]="Required confirmed historical pivot unavailable"
        if diagnostics:
            from tradingagents.okx_diagnostics import decision_diagnostic
            decision["diagnostic"]=decision_diagnostic(bundle,decision,config)
        decisions.append(decision)
    return decisions


def fetch_public(start,end,client=None):
    if start%HOUR or end%HOUR or end-start>14*24*HOUR or end<=start: raise ValueError("Window must be 1 hour..14 days")
    client=client or okx.OKXPublicClient()
    server=int(client.get("/api/v5/public/time")[0]["ts"])
    if end+HOUR>server: raise ValueError("Terminal hourly candle must already be confirmed closed")
    data={"symbol":SYMBOL,"server_ms":server,"instrument":client.instrument(SYMBOL),"candles":{},
        "source":"OKX API V5 public REST","coverage":{},"fetched_at_utc":iso(okx.utc_ms())}
    for period in mtf.PERIODS:
        step=okx.BARS[period][1]; count=402+math.ceil((end-start+HOUR)/step)
        rows=client.candles(SYMBOL,period,end+HOUR,count)
        frame,_=okx.validate_candles(rows,period,end+HOUR,250,count,0)
        # Store only confirmed data, preserving original volume units.
        records=[[int(r.ts),r.Open,r.High,r.Low,r.Close,r.VolumeContracts,r.Volume,r.VolumeQuote,"1"] for r in frame.itertuples()]
        data["candles"][period]=records
        data["coverage"][period]={"count":len(records),"first_open_utc":iso(records[0][0]),"last_close_utc":iso(records[-1][0]+step)}
    rows=deriv.history_pages(client,deriv.FUNDING_HISTORY,{"instId":SYMBOL},server+1,cursor_field="after",
        time_key=lambda r:r["fundingTime"],count=200,max_pages=3)
    data["funding"]=deriv.parse_funding_history(rows,SYMBOL,server)["records"]
    data["coverage"]["funding"]={"count":len(data["funding"]),"first_utc":iso(data["funding"][0]["ts"]),"last_utc":iso(data["funding"][-1]["ts"])}
    return data


def run_data(data,start,end,config, *, diagnostics=False):
    config=SimulationConfig.model_validate(config)
    if data.get("symbol") != SYMBOL or data.get("server_ms",0) < end+HOUR:
        raise ValueError("Cached source identity or observation-time bound invalid")
    bars=normalized([{"ts":int(r[0]),"Open":r[1],"High":r[2],"Low":r[3],"Close":r[4]} for r in data["candles"]["1h"] if start<=int(r[0])<=end])
    for period in mtf.PERIODS:
        rows=data["candles"][period]
        if any(str(r[8]) != "1" or int(r[0])+okx.BARS[period][1] > end+HOUR for r in rows):
            raise ValueError("Cached input contains future or unconfirmed candles")
        okx.validate_candles(rows,period,end+HOUR,250,len(rows),0)
    # Validate execution/funding coverage BEFORE spending work generating plans.
    validate_execution(bars,data["funding"],start,end,config,data.get("funding_evidence"))
    decisions=regenerate_decisions(data,start,end,config,diagnostics=diagnostics)
    if diagnostics and any(not d["diagnostic"]["data_quality"] for d in decisions):
        return {"status":"DATA_INVALID","metrics":None,"decisions":decisions,"trades":[],"events":[],"equity_curve":[],"execution_enabled":False}
    result=simulate(bars,data["funding"],decisions,data["instrument"],config,start,end,funding_evidence=data.get("funding_evidence"))
    result["coverage"]=data["coverage"]
    result["decision_counts"]={"total":len(decisions),"candidates":sum(bool(d["candidate"]) for d in decisions),"wait":sum(not d["candidate"] for d in decisions)}
    result["decisions"]=decisions
    if diagnostics:
        from tradingagents.okx_diagnostics import funnel_report
        result["signal_funnel"]=funnel_report(decisions,result["events"])
    import platform
    from importlib.metadata import version
    result["runtime_versions"]={"python":platform.python_version(),**{p:version(p) for p in ("pandas","stockstats","pydantic")}}
    result["rule_source_hash"]=digest({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
        (Path(__file__),Path(plans.__file__),Path(mtf.__file__),Path(okx.__file__),Path(deriv.__file__),Path(__file__).parent/"dataflows"/"plan_lifecycle.py")})
    return result


def main():
    parser=argparse.ArgumentParser(description="Offline OKX BTC perpetual rule-only simulation; execution disabled")
    parser.add_argument("--start",required=True,help="UTC ISO inclusive, e.g. 2026-10-12T00:00:00Z")
    parser.add_argument("--end",required=True,help="UTC ISO terminal open, e.g. 2026-10-22T00:00:00Z")
    parser.add_argument("--taker-fee-bps",type=float,required=True)
    parser.add_argument("--slippage-bps",type=float,required=True)
    parser.add_argument("--assume-current-contract-spec",action="store_true",required=True)
    parser.add_argument("--initial-equity",type=float,default=10000)
    parser.add_argument("--notional-usdt",type=float,default=1000)
    parser.add_argument("--input",help="Replay previously saved inputs.json, no network")
    parser.add_argument("--output",default=str(Path.home()/".tradingagents"/"logs"/"okx_simulations"))
    args=parser.parse_args()
    def parse(value):
        date=datetime.fromisoformat(value.replace("Z","+00:00"))
        if date.tzinfo is None: raise ValueError("Explicit UTC offset required")
        return int(date.timestamp()*1000)
    try:
        start,end=parse(args.start),parse(args.end)
        config=SimulationConfig(taker_fee_bps=args.taker_fee_bps,slippage_bps=args.slippage_bps,
            initial_equity=args.initial_equity,notional_usdt=args.notional_usdt,
            contract_parameter_assumption="CURRENT_SPEC_ASSUMED_CONSTANT_SHORT_WINDOW")
        data=json.loads(Path(args.input).read_text(encoding="utf-8"))["data"] if args.input else fetch_public(start,end)
        result=run_data(data,start,end,config)
        inputs={"data":data,"start":start,"end":end,"config":config.model_dump(),"rule_source_hash":result["rule_source_hash"],"runtime_versions":result["runtime_versions"]}
        root=save_run(args.output,inputs,result)
        print(json.dumps({"status":result["status"],"execution_enabled":False,"metrics":result["metrics"],"report":str(root)},indent=2))
    except (ValueError,KeyError,okx.DataQualityError) as exc:
        print(json.dumps({"status":"DATA_OR_CONFIGURATION_INVALID","error_type":type(exc).__name__,"reason":str(exc),"execution_enabled":False}))
        raise SystemExit(2)


if __name__=="__main__": main()
