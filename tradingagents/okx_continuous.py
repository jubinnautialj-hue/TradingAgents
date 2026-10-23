"""D.3 continuous research ledger and official long-history evidence.
No private API, order, account, optimizer or execution authorization.
"""
from __future__ import annotations
import argparse
import base64
import copy
import csv
import hashlib
import io
import json
import math
import re
import time
import zipfile
from collections import Counter
from datetime import datetime, UTC, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
import requests
from tradingagents import okx_backtest as bt, okx_diagnostics as diag
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv, okx_multitimeframe as mtf
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.errors import VendorUnavailableError

VERSION="okx-continuous-evidence-v1"
ARCHIVE="/api/v5/public/market-data-history"
TZ8=timezone(timedelta(hours=8))
DEVELOPMENT_END=int(datetime(2026,10,22,tzinfo=UTC).timestamp()*1000)


def fixed_config():
    return bt.SimulationConfig(taker_fee_bps=5,slippage_bps=2,
        funding_validation="OFFICIAL_ARCHIVE_PLUS_REST",contract_parameter_assumption="CURRENT_SPEC_UNVERIFIED_HISTORICAL_SCENARIO")


def strategy_fingerprint():
    freeze=json.loads((Path(__file__).parent.parent/"docs"/"okx_d3_strategy_freeze.json").read_text(encoding="utf-8"))
    # Numerical strategy sources must remain byte-for-byte at the D.3 freeze.
    for path in ("tradingagents/dataflows/perpetual_plan.py","tradingagents/dataflows/plan_lifecycle.py","tradingagents/dataflows/vendors/okx_multitimeframe.py"):
        current=hashlib.sha256((Path(__file__).parent.parent/path).read_bytes()).hexdigest()
        if current!=freeze["prechange_sources"][str(Path(path))]:raise ValueError("Frozen strategy source changed")
    return bt.digest({"config":freeze["config_fingerprint"],"frozen_sources":{p:h for p,h in freeze["prechange_sources"].items() if "dataflows" in p}})


def validate_archive_url(url):
    u=urlsplit(url)
    if u.scheme!="https" or u.hostname!="static.okx.com" or u.port not in (None,443) or not u.path.startswith("/cdn/okex/traderecords/swaprates/monthly/") or not u.path.endswith(".zip"):
        raise ValueError("Unapproved official funding archive URL")


def parse_archive(blob,url):
    validate_archive_url(url)
    if len(blob)>5_000_000:raise ValueError("Funding ZIP compressed size exceeds bound")
    name=Path(urlsplit(url).path).name
    match=re.fullmatch(r"BTC-USDT-SWAP-fundingrates-(\d{4})-(\d{2})\.zip",name)
    if not match:raise ValueError("Wrong instrument/archive filename")
    month="-".join(match.groups())
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        if len(z.infolist())!=1 or z.infolist()[0].file_size>20_000_000 or z.namelist()[0]!=name[:-4]+".csv":raise ValueError("Unexpected funding ZIP members/size")
        rows=csv.DictReader(z.read(z.namelist()[0]).decode("utf-8-sig").splitlines())
        if set(rows.fieldnames or [])!={"instrument_name","funding_rate","funding_time"}:raise ValueError("Unrecognized official funding archive schema")
        result=[];seen=set()
        for r in rows:
            ts=int(r["funding_time"]);rate=float(r["funding_rate"])
            if r["instrument_name"]!=bt.SYMBOL or ts in seen or ts%bt.HOUR or not math.isfinite(rate):raise ValueError("Invalid archive settlement")
            if datetime.fromtimestamp(ts/1000,TZ8).strftime("%Y-%m")!=month:raise ValueError("Archive UTC+8 monthly partition mismatch")
            seen.add(ts);result.append({"ts":ts,"realized_rate":rate})
        if not result or result!=sorted(result,key=lambda r:r["ts"]):raise ValueError("Empty/unordered archive")
    return result


def download_archive(url,cache):
    validate_archive_url(url)
    folder=Path(cache)/"funding_zips";folder.mkdir(parents=True,exist_ok=True)
    file=folder/(bt.digest(url)+".zip");meta=file.with_suffix(".json")
    with locked(file):
        if meta.exists():
            content=file.read_bytes()
            if json.loads(meta.read_text())["sha256"]!=hashlib.sha256(content).hexdigest():raise ValueError("Funding ZIP cache corruption")
        else:
            content=None
            for attempt in range(3):
                with requests.Session() as session:
                    session.auth=lambda request:request
                    response=session.get(url,timeout=(5,30),allow_redirects=False,stream=True)
                    with response:
                        if response.status_code==200:
                            parts=[];size=0
                            for part in response.iter_content(65536):
                                size+=len(part)
                                if size>5_000_000:raise ValueError("Oversized ZIP download")
                                parts.append(part)
                            content=b"".join(parts);break
                        if response.status_code!=429 and response.status_code<500:raise ValueError("Official ZIP HTTP rejection")
                time.sleep(2**attempt)
            if content is None:raise ValueError("Official ZIP unavailable after bounded retries")
            parse_archive(content,url)
            temp=file.with_suffix(".tmp");temp.write_bytes(content);temp.replace(file)
            temp=meta.with_suffix(".tmp");temp.write_text(json.dumps({"sha256":hashlib.sha256(content).hexdigest(),"downloaded_at_utc":bt.iso(okx.utc_ms())}),encoding="utf-8");temp.replace(meta)
    return {"url":url,"zip_sha256":hashlib.sha256(content).hexdigest(),"zip_base64":base64.b64encode(content).decode(),"downloaded_at_utc":json.loads(meta.read_text())["downloaded_at_utc"]}


def verify_archive_evidence(funding,evidence,start,end):
    if not evidence or evidence.get("type")!="OFFICIAL_MONTHLY_ARCHIVE_PLUS_REST":raise ValueError("Missing long-history funding evidence")
    union={};archive={}
    for file in evidence["files"]:
        blob=base64.b64decode(file["zip_base64"],validate=True)
        if hashlib.sha256(blob).hexdigest()!=file["zip_sha256"]:raise ValueError("Archive evidence hash mismatch")
        for r in parse_archive(blob,file["url"]):
            if r["ts"] in archive:raise ValueError("Overlapping archive months")
            archive[r["ts"]]=r["realized_rate"]
    raw=[r for p in evidence["rest"]["pages"] for r in p["rows"]]
    rest=deriv.parse_funding_history(raw,bt.SYMBOL,evidence["rest"]["server_ms"])["records"]
    diag.verify_funding_evidence(rest,evidence["rest"],rest[0]["ts"],rest[-1]["ts"])
    overlap=0;union.update(archive)
    for r in rest:
        if r["ts"] in archive:
            if not math.isclose(archive[r["ts"]],r["realized_rate"],rel_tol=1e-10,abs_tol=1e-12):raise ValueError("Official archive/REST actual-rate disagreement")
            overlap+=1
        union[r["ts"]]=r["realized_rate"]
    if not overlap:raise ValueError("No actual-rate overlap to verify archive semantics")
    expected=[{"ts":t,"realized_rate":v} for t,v in sorted(union.items())]
    if funding!=expected:raise ValueError("Merged funding differs from official evidence")
    times=[r["ts"] for r in expected]
    if min(times)>start or max(times)<end:raise ValueError("Historical funding does not bracket simulation")
    required=[t for t in times if start-8*bt.HOUR<=t<=end+8*bt.HOUR]
    if any(t%bt.HOUR for t in required) or any(b-a>8*bt.HOUR for a,b in zip(required,required[1:])):raise ValueError("Funding gap/subhour event unsupported")
    return {"archive_records":len(archive),"rest_records":len(rest),"matched_overlap":overlap,"observed_gap_hours":dict(Counter(str((b-a)/bt.HOUR) for a,b in zip(required,required[1:])))}


def fetch_history(start,end,cache,progress=print):
    if start%bt.HOUR or end%bt.HOUR or not 0<end-start<=731*24*bt.HOUR:raise ValueError("Aligned continuous window <=731 days required")
    if end>DEVELOPMENT_END:raise ValueError("Future validation interval protected; D.3 history must end by 2026-10-22 UTC")
    client=diag.CachedPublicClient(cache)
    server=int(client.get("/api/v5/public/time")[0]["ts"])
    if end+bt.HOUR>server:raise ValueError("Terminal candle not closed")
    data={"symbol":bt.SYMBOL,"server_ms":server,"instrument":client.instrument(bt.SYMBOL),"candles":{},"coverage":{},"source":"Official public REST + historical ZIPs"}
    for period in mtf.PERIODS:
        step=okx.BARS[period][1];count=402+(end-start+bt.HOUR+step-1)//step
        rows=client.candles(bt.SYMBOL,period,end+bt.HOUR,count)
        frame,_=okx.validate_candles(rows,period,end+bt.HOUR,250,count,0)
        data["candles"][period]=[[int(r.ts),r.Open,r.High,r.Low,r.Close,r.VolumeContracts,r.Volume,r.VolumeQuote,"1"] for r in frame.itertuples()]
        progress("Verified candles: "+period+" "+str(len(frame)))
    # Actual REST tail, pinned cursor and fully saved raw page chain.
    cursor=min(server+1,end+8*bt.HOUR+1);initial=cursor;pages=[]
    for _ in range(40):
        rows=client.get(deriv.FUNDING_HISTORY,{"instId":bt.SYMBOL,"after":str(cursor),"limit":"100"})
        if not rows:break
        stamps=[int(r["fundingTime"]) for r in rows]
        if max(stamps)>=cursor:raise ValueError("Funding cursor not advancing")
        pages.append({"after":cursor,"rows":rows,"hash":bt.digest(rows)});cursor=min(stamps)
        if cursor<=end-90*24*bt.HOUR:break
    rest_evidence={"path":deriv.FUNDING_HISTORY,"initial_cursor":initial,"server_ms":server,"pages":pages}
    rest=deriv.parse_funding_history([r for p in pages for r in p["rows"]],bt.SYMBOL,server)["records"]
    if not rest:raise ValueError("No REST settled funding")
    files=[];date=datetime.fromtimestamp(start/1000,TZ8).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    end_month=datetime.fromtimestamp(end/1000,TZ8).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    while date<end_month:
        next_month=(date.replace(day=28)+timedelta(days=4)).replace(day=1)
        params={"module":"3","instType":"SWAP","instFamilyList":"BTC-USDT","dateAggrType":"monthly", "begin":str(int(date.timestamp()*1000)),"end":str(int((next_month-timedelta(days=1)).timestamp()*1000))}
        time.sleep(0.45) # official archive listing: 5 requests / 2 seconds
        response=client.get(ARCHIVE,params)
        entries=[f for group in response for detail in group.get("details",[]) for f in detail.get("groupDetails",[]) if f.get("filename")=="BTC-USDT-SWAP-fundingrates-"+date.strftime("%Y-%m")+".zip"]
        if len(entries)!=1:raise ValueError("Official funding month missing/ambiguous: "+date.strftime("%Y-%m"))
        files.append(download_archive(entries[0]["url"],cache));progress("Verified funding archive: "+date.strftime("%Y-%m"))
        date=next_month
    evidence={"type":"OFFICIAL_MONTHLY_ARCHIVE_PLUS_REST","files":files,"rest":rest_evidence}
    union={}
    for file in files:
        for r in parse_archive(base64.b64decode(file["zip_base64"]),file["url"]):union[r["ts"]]=r["realized_rate"]
    for r in rest:union[r["ts"]]=r["realized_rate"]
    data["funding"]=[{"ts":t,"realized_rate":v} for t,v in sorted(union.items())]
    data["funding_evidence"]=evidence
    data["funding_audit"]=verify_archive_evidence(data["funding"],evidence,start,end)
    # Mark price history is separate public evidence, never current mark fallback.
    cursor=end+bt.HOUR;raw=[]
    for _ in range((end-start)//bt.HOUR//100+4):
        page=client.get(deriv.MARK_HISTORY,{"instId":bt.SYMBOL,"bar":"1H","after":str(cursor),"limit":"100"})
        if not page:break
        stamps=[int(r[0]) for r in page]
        if max(stamps)>=cursor:raise ValueError("Mark cursor did not progress")
        raw.extend(page);cursor=min(stamps)
        if cursor<=start:break
    mark=deriv.parse_price_history(raw,"1h",end+bt.HOUR)
    if mark["has_gaps"]:raise ValueError("Historical mark-price gap")
    data["mark_records"]=[r for r in mark["records"] if start<=r["ts"]<=end]
    if [r["ts"] for r in data["mark_records"]]!=list(range(start,end+bt.HOUR,bt.HOUR)):raise ValueError("Historical mark-price coverage incomplete")
    for p,rows in data["candles"].items():data["coverage"][p]={"count":len(rows),"first_open_utc":bt.iso(int(rows[0][0])),"last_close_utc":bt.iso(int(rows[-1][0])+okx.BARS[p][1])}
    data["coverage"].update(funding={"count":len(data["funding"]),"first_utc":bt.iso(data["funding"][0]["ts"]),"last_utc":bt.iso(data["funding"][-1]["ts"])},mark={"count":len(data["mark_records"]),"first_utc":bt.iso(start),"last_open_utc":bt.iso(end)})
    data["availability"]={"downloaded_at_utc":bt.iso(okx.utc_ms()),"archive_release":"Official typical T+2, monthly ZIP not assumed available at each event", "original_publication_timestamps":"UNKNOWN", "signal_prices":"Closed-bar underlying market assumed observable at close with zero dissemination latency, not archive download time", "funding":"Settled outcomes only; never a signal feature", "mark_valuation":"Historical hourly mark OPEN proxy; exact settlement mark not certified"}
    data["cache_statistics"]={"hits":client.hits,"new_history_requests":client.requests}
    return data


def merge_chunks(chunks):
    """Download/processing boundaries are NOT exits. Reject overlap, never double book."""
    rows=[]
    for chunk in chunks:
        for row in chunk:
            if rows and row["ts"]!=rows[-1]["ts"]+bt.HOUR:raise ValueError("Chunk gap/overlap/order error")
            rows.append(row)
    return rows


def continuous_simulation(chunks,funding,decisions,info,config,start,end,*,evidence=None,mark_prices=None):
    return bt.simulate(merge_chunks(chunks),funding,decisions,info,config,start,end,funding_evidence=evidence,max_window_days=731,funding_prices=mark_prices)


def validation_protocol(start,end):
    return {"protocol_version":"chronological-frozen-v1","newly_inspected_historical_data":[bt.iso(start),bt.iso(end)],
        "previously_inspected_development":["2026-07-24T00:00:00Z","2026-10-22T00:00:00Z"],
        "all_current_inspection_is_development":True,"future_holdout":{"start":"2026-10-24T00:00:00Z","end_exclusive":"2027-01-24T00:00:00Z","status":"NOT_RUN_NOT_YET_AVAILABLE"},
        "rules":["Freeze fingerprint before any holdout access","No parameter/strategy selection on holdout","Single scheduled retrospective evaluation after end and archive release","Carry development state using only past bars; do not force a split-boundary exit","Mark crossing-boundary trades separately; do not mix segment return with portfolio return","Keep 400 past-bar warmup, exclude future outcomes from feedback","Changing rules burns the tested holdout; require a new unseen period"]}



def decision_history(data,left,right):
    """After full-source validation, retain all 400 required past bars plus
    this processing segment. Per-cutoff filtering remains in the original
    historical_bundle validator; no future data becomes an indicator input.
    """
    selected={**data,"candles":{}}
    for p,rows in data["candles"].items():
        step=okx.BARS[p][1]
        ordered=sorted(rows,key=lambda r:int(r[0]))
        past=[r for r in ordered if int(r[0])+step<=left][-400:]
        following=[r for r in ordered if left<int(r[0])+step<=right]
        selected["candles"][p]=past+following
    return selected


def run_continuous(data,start,end,output,processing_days=14,progress=print):
    fingerprint=strategy_fingerprint();config=fixed_config()
    if end>DEVELOPMENT_END:raise ValueError("Protected future validation interval cannot be used as development history")
    if data.get("symbol")!=bt.SYMBOL or data.get("server_ms",0)<end+bt.HOUR:raise ValueError("Source identity/time bound invalid")
    for p,raw in data["candles"].items():
        if any(str(r[8])!="1" or int(r[0])+okx.BARS[p][1]>end+bt.HOUR for r in raw):raise ValueError("Future/unconfirmed cached candle")
        frame,_=okx.validate_candles(raw,p,end+bt.HOUR,250,len(raw),0)
        if not ((frame.Volume-frame.VolumeContracts*deriv.multiplier(data["instrument"])).abs() <= frame.Volume.abs()*1e-6+1e-8).all():raise ValueError("Full historical volume-unit validation failed")
    evidence_audit=verify_archive_evidence(data["funding"],data["funding_evidence"],start,end)
    raw=[r for r in data["candles"]["1h"] if start<=int(r[0])<=end]
    bars=bt.normalized([{"ts":int(r[0]),"Open":r[1],"High":r[2],"Low":r[3],"Close":r[4]} for r in raw])
    bt.validate_execution(bars,data["funding"],start,end,config,data["funding_evidence"],max_window_days=731)
    decisions=[]
    # Decision generation windows retain the original 4h grid and 400-bar warmup.
    if not 1<=processing_days<=60:raise ValueError("Processing batch must be 1..60 days")
    for left in range(start,end,processing_days*24*bt.HOUR):
        right=min(end,left+processing_days*24*bt.HOUR)
        decisions.extend(bt.regenerate_decisions(decision_history(data,left,right),left,right,config,diagnostics=True))
        progress("Generated historical decisions through "+bt.iso(right))
    if any(not d["diagnostic"]["data_quality"] for d in decisions):raise ValueError("Historical decision quality invalid; no zero-return substitution")
    mark_rows=data["mark_records"]
    if [r["ts"] for r in mark_rows]!=list(range(start,end+bt.HOUR,bt.HOUR)):raise ValueError("Mark coverage missing/duplicate")
    for r in mark_rows:
        if r["close_ms"]!=r["ts"]+bt.HOUR or r["close_ms"]>end+bt.HOUR or not all(math.isfinite(r[k]) and r[k]>0 for k in ("open","high","low","close")) or not r["low"]<=min(r["open"],r["close"])<=max(r["open"],r["close"])<=r["high"]:raise ValueError("Historical mark record invalid")
    marks={r["ts"]:r["open"] for r in mark_rows}
    width=processing_days*24
    result=continuous_simulation([bars[i:i+width] for i in range(0,len(bars),width)],data["funding"],decisions,data["instrument"],config,start,end,evidence=data["funding_evidence"],mark_prices=marks)
    result["decisions"]=decisions;result["signal_funnel"]=diag.funnel_report(decisions,result["events"])
    # Reuse D.2 independent audit with a bound historical mark valuation callback.
    result["trade_audit"]=diag.audit_trades(data,result,config,funding_prices=marks)
    result["funding_evidence_audit"]=evidence_audit
    funding_events=[e for e in result["events"] if e["type"]=="FUNDING"]
    trade_opens={int(r[0]):float(r[1]) for r in data["candles"]["1h"]}
    by_snapshot={t["snapshot_id"]:t for t in result["trades"]}
    trade_proxy=sum(deriv.funding_cashflow(by_snapshot[e["snapshot_id"]]["base_quantity"],trade_opens[e["ts"]],e["rate"],by_snapshot[e["snapshot_id"]]["direction"]) for e in funding_events)
    result["funding_valuation_comparison"]={"events":len(funding_events),"historical_mark_open_cashflow":result["metrics"]["funding_cashflow_usdt"],"old_trade_open_proxy_cashflow":trade_proxy,"scope":"Valuation sensitivity, neither certifies exact settlement mark"}
    result["strategy_fingerprint"]=fingerprint;result["continuous_version"]=VERSION
    result["configuration_fingerprint"]=bt.digest(config.model_dump())
    result["accounting_fingerprint"]=bt.digest({"config":config.model_dump(),"funding_price":"HISTORICAL_MARK_HOURLY_OPEN_PROXY","ledger":"CONTINUOUS_GLOBAL_TERMINAL_ONLY"})
    import platform
    from importlib.metadata import version
    result["runtime_versions"]={"python":platform.python_version(),**{p:version(p) for p in ("pandas","stockstats","pydantic")}}
    result["allow_new_risk"]=False
    result["coverage"]=data["coverage"];result["availability"]=data["availability"]
    result["validation_protocol"]=validation_protocol(start,end)
    result["limitations"]=["Continuous conditional historical scenario, not certified historical contract/execution reconstruction",
        "Current contract multiplier/tick/lot/minimum assumed, not historical parameter versions","No leverage/margin/liquidation or current OI/book",
        "Funding uses historical MARK hourly open proxy; exact exchange settlement mark remains unverified",
        "Unknown original publication latency; market close observability assumes zero latency; archives are ex-post evidence only",
        "Hourly mark-to-market drawdown excludes intrabar extremes","All inspected history is development; future holdout not run"]
    source_hash=bt.digest({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in map(Path,[bt.__file__,diag.__file__,__file__,okx.__file__,deriv.__file__])})
    inputs={"data":data,"start":start,"end":end,"config":config.model_dump(),"strategy_fingerprint":fingerprint,"continuous_source_hash":source_hash,"runtime_versions":result["runtime_versions"]}
    # Processing batch size is deliberately excluded: identical economic result must share identity.
    root=bt.save_run(output,inputs,result)
    reports={"coverage.json":{"coverage":data["coverage"],"availability":data["availability"],"funding_audit":evidence_audit},
        "validation_protocol.json":result["validation_protocol"]}
    for name,value in reports.items():
        file=root/name
        if not file.exists():file.write_text(json.dumps(value,indent=2),encoding="utf-8")
    file=root/"report.md"
    if not file.exists():
        file.write_text("# D.3 continuous conditional simulation\n\nexecution_enabled=False\n\n"+json.dumps({"metrics":result["metrics"],"funding_audit":evidence_audit,"funnel":result["signal_funnel"],"limitations":result["limitations"]},indent=2),encoding="utf-8")
    return root,result


def main():
    parser=argparse.ArgumentParser(description="Frozen continuous OKX research; no execution")
    parser.add_argument("--start",required=True);parser.add_argument("--end",required=True)
    parser.add_argument("--assume-current-contract-spec",required=True,action="store_true")
    base=Path.home()/".tradingagents/logs/okx_continuous"
    parser.add_argument("--cache",default=str(base/"cache"));parser.add_argument("--output",default=str(base/"runs"))
    parser.add_argument("--input",help="Offline prepared source.json or prior inputs.json")
    parser.add_argument("--processing-days",type=int,default=14)
    args=parser.parse_args()
    def parse(v):
        d=datetime.fromisoformat(v.replace("Z","+00:00"))
        if d.tzinfo is None:raise ValueError("Explicit UTC offset required")
        return int(d.timestamp()*1000)
    try:
        start,end=parse(args.start),parse(args.end);strategy_fingerprint()
        if args.input:data=json.loads(Path(args.input).read_text(encoding="utf-8"))["data"]
        else:
            data=fetch_history(start,end,args.cache)
            prepared=Path(args.output)/"prepared";prepared.mkdir(parents=True,exist_ok=True)
            file=prepared/(bt.digest(data)+".json")
            if not file.exists():file.write_text(json.dumps({"data":data,"start":start,"end":end},allow_nan=False),encoding="utf-8")
            print("Prepared source: "+str(file),flush=True)
        root,result=run_continuous(data,start,end,args.output,args.processing_days)
        print(json.dumps({"report":str(root),"metrics":result["metrics"],"strategy_fingerprint":result["strategy_fingerprint"],"events":result["signal_funnel"]["events"],"execution_enabled":False},indent=2))
    except (ValueError,KeyError,VendorUnavailableError,requests.RequestException,zipfile.BadZipFile) as exc:
        print(json.dumps({"status":"DATA_OR_EVIDENCE_INVALID","error_type":type(exc).__name__,"reason":str(exc),"execution_enabled":False}));raise SystemExit(2)


if __name__=="__main__":main()
