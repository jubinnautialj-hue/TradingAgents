import copy
import json
import pytest
from tradingagents import okx_backtest as bt
from tradingagents import okx_diagnostics as diag
from tests.test_okx_backtest import START, CONFIG, bars, decision
from tests.test_okx_research import INFO
from tests.test_okx_multitimeframe import MultiClient, NOW


def raw_funding(times):
    return [{"instId":bt.SYMBOL,"instType":"SWAP","fundingTime":str(t),"realizedRate":"0.0001","fundingRate":"0.0002"} for t in sorted(times,reverse=True)]


def evidence(times):
    rows=raw_funding(times);server=max(times)+bt.HOUR
    e={"path":diag.deriv.FUNDING_HISTORY,"initial_cursor":server+1,"server_ms":server,
       "pages":[{"after":server+1,"rows":rows,"hash":bt.digest(rows)}]}
    return diag.deriv.parse_funding_history(rows,bt.SYMBOL,server)["records"],e


def test_variable_funding_schedule_not_fixed_eight_hours():
    times=[START+i*bt.HOUR for i in (0,8,12,14,15,16)]
    fs,e=evidence(times)
    assert set(diag.verify_funding_evidence(fs,e,START,START+16*bt.HOUR))=={"1.0","2.0","4.0","8.0"}
    cfg=CONFIG.model_copy(update={"funding_validation":"PAGINATED_OBSERVED_EVENTS"})
    r=bt.simulate(bars(16),fs,[decision()],INFO,cfg,START,START+16*bt.HOUR,funding_evidence=e)
    assert r["trades"] and len([ev for ev in r["events"] if ev["type"]=="FUNDING"])==3


@pytest.mark.parametrize("bad",["hash","cursor","normalized","bracket","hour","gap","missing_evidence"])
def test_funding_evidence_fail_closed(bad):
    times=[START+i*bt.HOUR for i in (0,8,16)]
    if bad=="hour": times[1]+=1
    if bad=="gap": times=[START,START+16*bt.HOUR]
    fs,e=evidence(times)
    if bad=="hash": e["pages"][0]["hash"]="tampered"
    if bad=="cursor": e["pages"][0]["after"]-=1
    if bad=="normalized": fs[0]["realized_rate"]=1
    if bad=="bracket": fs,e=evidence(times[1:])
    if bad=="missing_evidence":e=None
    with pytest.raises(ValueError):diag.verify_funding_evidence(fs,e,START,START+16*bt.HOUR)


class Source:
    def __init__(self):self.calls=0
    def get(self,path,params=None):self.calls+=1;return [["history"]]


def test_cached_page_resume_and_no_duplicate_requests(tmp_path):
    source=Source();c=diag.CachedPublicClient(tmp_path,source)
    params={"after":"100","instId":bt.SYMBOL}
    a=c.get("/api/v5/market/history-candles",params)
    resumed=diag.CachedPublicClient(tmp_path,source)
    assert resumed.get("/api/v5/market/history-candles",params)==a
    assert source.calls==1 and resumed.hits==1
    with pytest.raises(ValueError):c.get("/api/v5/trade/order",{})


def test_corrupt_cache_not_refetched(tmp_path):
    source=Source();c=diag.CachedPublicClient(tmp_path,source)
    c.get("/api/v5/market/history-candles",{})
    file=next(tmp_path.glob("*.json"));obj=json.loads(file.read_text());obj["rows"]=[];file.write_text(json.dumps(obj))
    with pytest.raises(ValueError,match="Corrupt"):c.get("/api/v5/market/history-candles",{})
    assert source.calls==1


def synthetic_data():
    start=NOW//(8*bt.HOUR)*(8*bt.HOUR);end=start+8*bt.HOUR
    client=MultiClient(pending=False)
    data={"symbol":bt.SYMBOL,"instrument":INFO,"server_ms":end+2*bt.HOUR,"candles":{
        p:sorted(client.candles(bt.SYMBOL,p,end+bt.HOUR,500),key=lambda r:int(r[0])) for p in diag.mtf.PERIODS},
        "coverage":{}}
    data["funding"],data["funding_evidence"]=evidence([start,start+8*bt.HOUR])
    return data,start,end


def test_diagnostics_do_not_change_strategy_or_pnl():
    data,start,end=synthetic_data()
    plain=bt.run_data(data,start,end,CONFIG)
    observed=bt.run_data(data,start,end,CONFIG,diagnostics=True)
    assert plain["metrics"]==observed["metrics"] and plain["trades"]==observed["trades"]
    assert observed["signal_funnel"]["decisions"]==2
    assert all(d["diagnostic"]["outcome"]=="NO_CANDIDATE" for d in observed["decisions"])


def test_event_funnel_and_attribution():
    d=decision();d["diagnostic"]={"data_quality":True,"directions":{}}
    fs,e=evidence([START,START+8*bt.HOUR])
    r=bt.simulate(bars(),fs,[d],INFO,CONFIG,START,START+8*bt.HOUR)
    funnel=diag.funnel_report([d],r["events"])
    assert funnel["events"]["ENTRY"]==1 and funnel["breakout_success_rate"]==1
    assert funnel["retest_success_rate"]==1 and d["diagnostic"]["outcome"]=="FILLED"
    assert all(ev.get("snapshot_id")==d["snapshot_id"] for ev in r["events"])


def test_original_wait_root_cause_observations():
    data,start,end=synthetic_data()
    bundle=bt.historical_bundle(data,start,{"okx_multi_timeframe":True,"okx_interval":"1h"})
    bundle["timeframe_analysis"]["4h"]["trend"]="DOWN"
    bundle["confluence"]=diag.mtf.confluence(bundle["timeframe_analysis"])
    d={"candidate":None,"reason":"No three-timeframe directional resonance"}
    observed=diag.decision_diagnostic(bundle,d,{})
    assert observed["directions"]["LONG"]["checks"]["trend_1d"]
    assert not observed["directions"]["LONG"]["checks"]["structure_4h"]
    assert not observed["directions"]["LONG"]["checks"]["resonance"]


def test_data_invalid_not_zero_return(monkeypatch):
    data,start,end=synthetic_data()
    def invalid(*args):raise ValueError("Missing history")
    monkeypatch.setattr(bt,"historical_bundle",invalid)
    r=bt.run_data(data,start,end,CONFIG,diagnostics=True)
    assert r["status"]=="DATA_INVALID" and r["metrics"] is None
    assert all(not d["diagnostic"]["data_quality"] for d in r["decisions"])


def test_expanded_batch_idempotency_and_source_bounds(tmp_path):
    data,start,end=synthetic_data()
    a,r=diag.run_expanded(data,start,end,tmp_path)
    b,s=diag.run_expanded(data,start,end,tmp_path)
    assert a==b and r==s
    assert r["available_windows"]["conditional_simulation"]["start_utc"]==bt.iso(start)
    assert not r["execution_enabled"]


def test_all_eight_hour_observed_and_declared_modes_match():
    times=[START,START+8*bt.HOUR]
    fs,e=evidence(times)
    fixed=bt.simulate(bars(),fs,[decision()],INFO,CONFIG,START,START+8*bt.HOUR)
    observed=bt.simulate(bars(),fs,[decision()],INFO,CONFIG.model_copy(update={"funding_validation":"PAGINATED_OBSERVED_EVENTS"}),START,START+8*bt.HOUR,funding_evidence=e)
    assert fixed["metrics"]==observed["metrics"] and fixed["trades"]==observed["trades"]


@pytest.mark.parametrize("tamper",[None,"fees_usdt","funding_cashflow","entry_ms"])
def test_trade_audit_checks_fills_and_costs(tamper):
    rs=bars();fs,e=evidence([START,START+8*bt.HOUR]);d=decision()
    r=bt.simulate(rs,fs,[d],INFO,CONFIG,START,START+8*bt.HOUR);r["decisions"]=[d]
    data={"instrument":INFO,"funding":fs,"candles":{"1h":[[b["ts"],b["open"],b["high"],b["low"],b["close"],100,1,100,"1"] for b in rs]}}
    if tamper:
        r["trades"][0][tamper]+=1
        with pytest.raises((ValueError,KeyError)):diag.audit_trades(data,r,CONFIG)
    else:assert diag.audit_trades(data,r,CONFIG)["trades_checked"]==1


def test_warmup_not_silently_lowered(tmp_path):
    data,start,end=synthetic_data();data["candles"]["1d"]=data["candles"]["1d"][-399:]
    with pytest.raises(ValueError,match="400 warmup"):diag.run_expanded(data,start,end,tmp_path)


def test_interrupted_download_retains_successful_pages(tmp_path):
    class Flaky:
        def get(self,path,params):
            if params["after"]=="200":raise RuntimeError("interrupted")
            return [["safe-page"]]
    client=diag.CachedPublicClient(tmp_path,Flaky())
    client.get("/api/v5/market/history-candles",{"after":"100"})
    with pytest.raises(RuntimeError):client.get("/api/v5/market/history-candles",{"after":"200"})
    resumed=diag.CachedPublicClient(tmp_path,Flaky())
    assert resumed.get("/api/v5/market/history-candles",{"after":"100"})==[["safe-page"]]
    assert resumed.requests==0 and resumed.hits==1


def test_whole_invalid_batch_retains_each_decision_in_funnel(tmp_path,monkeypatch):
    data,start,end=synthetic_data()
    def fail(*args,**kwargs):raise ValueError("Missing required execution history")
    monkeypatch.setattr(bt,"run_data",fail)
    root,r=diag.run_expanded(data,start,end,tmp_path)
    assert r["status"]=="PARTIAL" and r["totals"]["invalid_batches"]==1
    assert r["signal_funnel"]["decisions"]==2 and r["signal_funnel"]["data_valid"]==0
    assert r["batches"][0]["metrics"] is None
    assert (root/"report.md").exists()
