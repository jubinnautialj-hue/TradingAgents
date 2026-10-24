"""F3 deterministic isolated fixtures. Never registered as real forward trades."""
import copy
import multiprocessing

import pytest

from tradingagents import forward_paper as p, forward_tracking as f2
from tradingagents.forward_validation import digest, read, atomic
from tradingagents.dataflows.plan_lifecycle import HOUR, timing
from tests.test_forward_validation import setup as f1_setup, register as register_f1
from tests.test_perpetual_plan import research
from tests.test_observation_eligibility import CandleClient, bar as rawbar

T=HOUR*500000


def proof(checked):
    v={"status":"VALID","checked_at_ms":checked,"expires_at_ms":checked+60000}
    v["validation_id"]=digest(v)
    return {"validation":v,"checked_ms":checked,"completed_ms":checked+1,"expires_ms":checked+60000}


def state(direction="LONG"):
    info={"instId":"BTC-USDT-SWAP","instType":"SWAP","instFamily":"BTC-USDT","settleCcy":"USDT",
          "ctType":"linear","state":"live","ctValCcy":"BTC","quoteCcy":"USDT",
          "ctVal":"0.01","ctMult":"1","tickSz":"0.1","lotSz":"1","minSz":"1"}
    plan={"direction":direction,"entry":{"price":100},"stop":{"price":90 if direction=="LONG" else 110},
          "target":{"price":120 if direction=="LONG" else 80}}
    return p.PaperState(paper_trade_id="paper-fixture",registration_id="a"*64,research_id="fixture",
        namespace="FIXTURE",group="g",pair_id="pair",identity={"schedule_persisted_ms":T-1000},plan=plan,info=info,
        trigger={"signal_confirmed_ms":T-HOUR,"hypothesis_expires_ms":T+6*HOUR},
        paper_evaluation_eligible=True,status="SCHEDULED",planned_entry_ms=T,revalidation=proof(T-10000))


def opening(target=T):
    return {"reference":100.,"boundary_ms":target,"request_started_ms":target+100,
            "observed_ms":target+1000,"collection_mode":"CONTEMPORANEOUS"}


def bar(ts=T,open_=100,high=105,low=95,close=102):
    return {"ts":ts,"open":open_,"high":high,"low":low,"close":close,"confirm":"1"}


def funding(start=T,end=T+HOUR,rate=.001):
    lo=start//(4*HOUR)*(4*HOUR)
    hi=(end//(4*HOUR)+1)*(4*HOUR)
    times=list(range(lo,hi+1,4*HOUR))
    rows=[{"instId":"BTC-USDT-SWAP","instType":"SWAP","fundingTime":str(t),
           "realizedRate":str(rate),"fundingRate":".12345"} for t in reversed(times)]
    records=p.deriv.parse_funding_history(rows,"BTC-USDT-SWAP",hi+HOUR)["records"]
    evidence={"path":p.deriv.FUNDING_HISTORY,"initial_cursor":hi+1,"server_ms":hi+HOUR,
              "pages":[{"after":hi+1,"rows":rows,"hash":digest(rows)}]}
    return {"records":records,"evidence":evidence,"available_ms":hi+HOUR}


@pytest.mark.parametrize("delay",[0,1100,HOUR-1,HOUR])
def test_strict_future_hour_after_confirmation_and_revalidation(delay):
    assert p.next_open(T+delay)>T+delay
    assert p.next_open(T+delay)%HOUR==0
    assert p.next_open(T+1100)==T+HOUR


@pytest.mark.parametrize("field",["simulation_admitted","allow_new_risk","execution_enabled"])
def test_protocol_never_grants_permission(field):
    with pytest.raises(ValueError): p.PaperProtocol.model_validate({**p.POLICY,field:True})
    with pytest.raises(ValueError): p.PaperState.model_validate({**state().model_dump(),field:True})


def test_paper_observation_not_automatically_a_fill():
    s=state()
    assert s.paper_evaluation_eligible and s.status=="SCHEDULED" and not s.position
    assert not s.simulation_admitted and not s.allow_new_risk


@pytest.mark.parametrize("kind",["late","early","backfill","wrong_boundary","stale","late_validation","unarmed","expired"])
def test_no_backdating_or_stale_fill(kind):
    s=state();o=opening();now=o["observed_ms"]
    if kind=="late": o["observed_ms"]=now=T+16000
    if kind=="early": o["request_started_ms"]=T-1
    if kind=="backfill": o["collection_mode"]="BACKFILL"
    if kind=="wrong_boundary": o["boundary_ms"]+=HOUR
    if kind=="stale": s.revalidation=proof(T-100000)
    if kind=="late_validation": s.revalidation=proof(T+1)
    if kind=="unarmed": s.identity={}
    if kind=="expired": s.trigger["hypothesis_expires_ms"]=T
    with pytest.raises(ValueError):p.fill(s,o,now)
    assert s.status=="SCHEDULED" and not s.position


@pytest.mark.parametrize("direction,win",[("LONG",True),("LONG",False),("SHORT",True),("SHORT",False)])
def test_long_short_profit_loss_and_costs(direction,win):
    s=p.fill(state(direction),opening(),T+1000)
    # Use second bar to exclude the explicitly unresolved first-bar target path.
    s=p.advance_bar(s,bar(),T+HOUR+1000)
    target=s.plan["target"]["price"];stop=s.plan["stop"]["price"]
    if direction=="LONG":b=bar(T+HOUR,high=target+1 if win else 105,low=95 if win else stop-1)
    else:b=bar(T+HOUR,high=105 if win else stop+1,low=target-1 if win else 95)
    s=p.advance_bar(s,b,T+2*HOUR+1000)
    s=p.settle(s,funding(end=T+2*HOUR),[bar(),b],as_of_ms=T+20*HOUR)
    assert s.status=="SETTLED"
    assert (s.exit["net_pnl"]>0)==win
    base=s.position["base_quantity"]
    expected=base*(s.position["entry_price"]+s.exit["exit_price"])*.0005
    assert s.exit["fees_usdt"]==pytest.approx(expected)
    assert s.exit["net_pnl"]==pytest.approx(s.exit["gross_pnl_after_slippage"]-expected+s.funding["cashflow_usdt"])
    assert s.position["entry_ms"]==T+1000  # NEVER the past boundary
    assert s.exit["slippage_usdt"]>0


def test_same_bar_stop_first_and_gap_conservative():
    s=p.fill(state(),opening(),T+1000)
    result=p.advance_bar(s,bar(high=125,low=80),T+HOUR)
    assert result.exit["reason"]=="STOP_FIRST_AMBIGUOUS"
    assert result.exit["exit_reference"]==90
    s=p.advance_bar(s,bar(),T+HOUR)
    result=p.advance_bar(s,bar(T+HOUR,open_=85,high=95,low=80,close=90),T+2*HOUR)
    assert result.exit["exit_reference"]==85


def test_first_bar_target_order_is_not_fabricated():
    s=p.fill(state(),opening(),T+1000)
    result=p.advance_bar(s,bar(high=125,close=102),T+HOUR)
    assert result.status=="UNRESOLVED" and not result.exit
    result=p.advance_bar(s,bar(high=125,close=122),T+HOUR)
    assert result.exit["reason"]=="TARGET"


@pytest.mark.parametrize("kind",["future","gap","duplicate","unconfirmed","nan","bad_ohlc"])
def test_execution_candles_fail_closed(kind):
    s=p.fill(state(),opening(),T+1000);b=bar();available=T+HOUR
    if kind=="future":available-=1
    if kind=="gap":b["ts"]+=HOUR
    if kind=="duplicate":s.cursor_ms+=HOUR
    if kind=="unconfirmed":b["confirm"]="0"
    if kind=="nan":b["close"]=float('nan')
    if kind=="bad_ohlc":b["high"]=90
    with pytest.raises(ValueError):p.advance_bar(s,b,available)


def test_funding_realized_and_direction_and_known_time():
    for direction,expected_sign in [("LONG",-1),("SHORT",1)]:
        s=p.fill(state(direction),opening(),T+1000)
        bars=[bar(T+i*HOUR) for i in range(6)]
        for b in bars[:5]:s=p.advance_bar(s,b,b['ts']+HOUR)
        b=bar(T+5*HOUR,high=125 if direction=="LONG" else 105,low=95 if direction=="LONG" else 75)
        s=p.advance_bar(s,b,b['ts']+HOUR)
        f=funding(end=T+6*HOUR)
        s=p.settle(s,f,bars,as_of_ms=T+20*HOUR)
        assert s.funding["cashflow_usdt"]*expected_sign>0
        assert all(c['realized_rate']==.001 for c in s.funding['charges'])
        assert not p.feedback([s],s.exit['result_known_at_ms']-1,"FIXTURE")
        assert len(p.feedback([s],s.exit['result_known_at_ms'],"FIXTURE"))==1
        assert not p.feedback([s],10**16,"PRODUCTION")


def test_funding_missing_brackets_and_tamper_pause_settlement():
    s=p.advance_bar(p.fill(state(),opening(),T+1000),bar(low=80),T+HOUR)
    f=funding()
    f['records']=[]
    with pytest.raises(ValueError):p.settle(s,f,[bar()],as_of_ms=T+20*HOUR)
    assert s.status=="EXIT_PENDING" and 'net_pnl' not in s.exit


def test_max_holding_uses_following_open_not_last_close():
    s=p.fill(state(),opening(),T+1000)
    for i in range(12):s=p.advance_bar(s,bar(T+i*HOUR),T+(i+1)*HOUR)
    assert s.status=="OPEN"
    s=p.advance_bar(s,bar(T+12*HOUR,open_=101),T+13*HOUR)
    assert s.exit['reason']=="MAX_HOLDING" and s.exit['exit_reference']==101


def test_ledger_idempotency_resume_tamper_and_namespace(tmp_path):
    ledger=p.Ledger(tmp_path,"FIXTURE");s=state();events=[]
    with p.locked(ledger.path):ledger.append(events,s.registration_id,"SCHEDULED",{'state':s.model_dump()},T-500)
    ledger.append(events,s.registration_id,"SCHEDULED",{'state':s.model_dump()},T)
    assert len(ledger.load())==1
    assert p.Ledger.states(p.Ledger(tmp_path,"FIXTURE").load())[s.registration_id]==s
    raw=read(ledger.path);raw['events'][0]['payload']['state']['status']='OPEN';atomic(ledger.path,raw)
    with pytest.raises(ValueError,match='fingerprint'):ledger.load()
    assert not p.Ledger(tmp_path,"PRODUCTION").load()


def test_atomic_interruption_cannot_leave_partial_trade(tmp_path,monkeypatch):
    ledger=p.Ledger(tmp_path,"FIXTURE");events=[]
    def fail(*a):raise OSError('interrupted')
    monkeypatch.setattr(p,'atomic',fail)
    with pytest.raises(OSError):ledger.append(events,state().registration_id,"SCHEDULED",{'state':state().model_dump()},T)
    assert not ledger.load() and not events


def test_empty_real_report_and_no_fixture_contamination():
    r=p.report([],[])
    assert r['paper']['trades']==0 and r['paper']['win_rate'] is None and r['paper']['net_pnl'] is None
    assert r['pairing']['performance_comparison'] is None
    event={'registration_id':state().registration_id,'payload':{'state':state().model_dump()},'kind':'SCHEDULED'}
    assert p.report([],[event])['paper']['trades']==0


@pytest.mark.parametrize('change',[
    {'action':'WAIT'},{'workflow_status':'FAILED'},{'plan_valid':False},
    {'origin':'TEST_REPLAY'},{'origin':'HISTORICAL_IMPORT'},{'data_quality':'STALE_DATA'}])
def test_invalid_research_cannot_become_paper(f1_setup,change):
    r=register_f1(f1_setup).model_copy(update=change)
    assert p.admission(r,None,f1_setup[4][0])


def test_opening_must_be_observed_unclosed_and_not_synthesized(monkeypatch):
    clock=[T+1000];monkeypatch.setattr(p.okx,'utc_ms',lambda:clock[0])
    class Client:
        def get(self,path,params):
            assert path=='/api/v5/market/candles'
            return [[str(T),'100','999','1','998','1','1','1','0']]
    market=p.Market(Client(),f2.ScanLimits())
    o=market.opening('BTC-USDT-SWAP',T)
    assert o['reference']==100 and 'high' not in o
    clock[0]=T+16000
    with pytest.raises(ValueError):market.opening('BTC-USDT-SWAP',T)


def test_mock_f1_f2_schedule_miss_no_backfill_and_originals_unchanged(f1_setup,tmp_path,monkeypatch):
    r=register_f1(f1_setup);registry=f1_setup[0];clock=f1_setup[4]
    monkeypatch.setattr(p.okx,'utc_ms',lambda:clock[0])
    f2root=tmp_path/'f2';paper=tmp_path/'f3'
    entry=r.original_result['entry_reference']['price'];gap=entry-r.original_result['stop_loss']['price']
    clock[0]=r.first_check_ms+1000
    f2.scan(registry.root,f2root,client=CandleClient(clock,[rawbar(r.first_bar_open_ms,entry,gap)]))
    clock[0]+=HOUR
    f2.scan(registry.root,f2root,client=CandleClient(clock,[rawbar(r.first_bar_open_ms+HOUR,entry,gap,True)]))
    archived=(registry.root/(r.registration_id+'.json')).read_bytes()
    f2path=f2root/'production'/(r.registration_id+'.events.json');old=f2path.read_bytes()
    class MockMarket:
        def now(self):return clock[0]
        def refresh(self,*args):return proof(clock[0]-1)
    result=p.scan(registry.root,f2root,paper,market=MockMarket())
    assert result['research']['paper_scheduled']==1
    ledger=p.Ledger(paper);s=next(iter(ledger.states(ledger.load()).values()))
    assert s.planned_entry_ms>clock[0] and not s.position
    clock[0]=s.planned_entry_ms+16000
    result=p.scan(registry.root,f2root,paper,market=MockMarket())
    assert result['reliability']['missed']==1 and result['paper']['trades']==0
    count=len(ledger.load());p.scan(registry.root,f2root,paper,market=MockMarket())
    assert len(ledger.load())==count
    assert archived==(registry.root/(r.registration_id+'.json')).read_bytes() and old==f2path.read_bytes()


def worker(args):
    root=args
    ledger=p.Ledger(root,"FIXTURE")
    with p.locked(ledger.path):
        events=ledger.load()
        ledger.append(events,state().registration_id,'SCHEDULED',{'state':state().model_dump()},T)
    return True


def test_windows_multiprocess_no_duplicate_accounting(tmp_path):
    ctx=multiprocessing.get_context('spawn')
    with ctx.Pool(2) as pool:assert all(pool.map(worker,[str(tmp_path)]*2))
    assert len(p.Ledger(tmp_path,"FIXTURE").load())==1


def test_future_funding_cannot_settle_and_forecast_is_not_realized():
    s=p.advance_bar(p.fill(state(),opening(),T+1000),bar(low=80),T+HOUR)
    f=funding()
    with pytest.raises(ValueError,match='Future'):
        p.settle(s,f,[bar()],as_of_ms=f['available_ms']-1)
    f['evidence']['pages'][0]['rows'][0].pop('realizedRate')
    with pytest.raises(ValueError):p.settle(s,f,[bar()],as_of_ms=T+20*HOUR)


def test_cost_equity_and_drawdown_are_deterministic():
    s=p.advance_bar(p.fill(state(),opening(),T+1000),bar(low=80),T+HOUR)
    s=p.settle(s,funding(),[bar()],as_of_ms=T+20*HOUR)
    event={'registration_id':s.registration_id,'kind':'SETTLED','payload':{'state':s.model_dump()}}
    r=p.report([],[event],namespace='FIXTURE')
    assert r['paper']['net_pnl']==s.exit['net_pnl']<0
    assert r['paper']['equity_curves']['g'][-1]['equity']==pytest.approx(10000+s.exit['net_pnl'])
    assert r['paper']['realized_only_drawdown_by_group']['g']==pytest.approx(-s.exit['net_pnl']/10000)
    assert r['paper']['settled_history_closed_candle_drawdown_by_group']['g']>0


def test_pairing_requires_same_window_model_and_prospective_origin():
    a=state();a.namespace='PRODUCTION'
    a.identity.update(origin='PROSPECTIVE',research_config={'quick_think_llm':'fixed'},paper_protocol_hash=p.POLICY_HASH,
                      instrument='BTC-USDT-SWAP',research_cutoff_ms=T-HOUR)
    b=a.model_copy(deep=True);b.registration_id='b'*64;b.group='enhanced'
    assert p.compare_pair(a,b)['status']=='PROSPECTIVE_DESIGN_MATCH'
    for change in ('historical','window','model'):
        c=b.model_copy(deep=True)
        if change=='historical':c.identity['origin']='HISTORICAL_IMPORT'
        if change=='window':c.planned_entry_ms+=HOUR
        if change=='model':c.identity['research_config']['quick_think_llm']='changed'
        assert p.compare_pair(a,c)['status']=='NOT_COMPARABLE'


def test_original_spec_precision_and_minimum_and_gap_reject():
    for kind in ('spec','min','tick','gap','budget'):
        s=state();o=opening();equity=10000
        if kind=='spec':s.info['ctValCcy']='USD'
        if kind=='min':s.info['minSz']='999999'
        if kind=='tick':s.plan['stop']['price']=90.01
        if kind=='gap':o['reference']=130
        if kind=='budget':equity=10
        with pytest.raises(ValueError):p.fill(s,o,T+1000,equity)


def test_full_mock_f1_f2_paper_fill_settle_restart(f1_setup,tmp_path,monkeypatch):
    """Test-only temporary archives. No real market or model API is invoked."""
    r=register_f1(f1_setup);registry=f1_setup[0];clock=f1_setup[4]
    monkeypatch.setattr(p.okx,'utc_ms',lambda:clock[0])
    f2root=tmp_path/'f2';output=tmp_path/'f3'
    entry=r.original_result['entry_reference']['price'];stop=r.original_result['stop_loss']['price']
    target=r.original_result['take_profit'][0]['price'];gap=entry-stop
    clock[0]=r.first_check_ms+1000
    f2.scan(registry.root,f2root,client=CandleClient(clock,[rawbar(r.first_bar_open_ms,entry,gap)]))
    clock[0]+=HOUR
    f2.scan(registry.root,f2root,client=CandleClient(clock,[rawbar(r.first_bar_open_ms+HOUR,entry,gap,True)]))
    class MockMarket:
        planned=None
        def now(self):return clock[0]
        def refresh(self,*a):return proof(clock[0]-1)
        def opening(self,symbol,t):
            self.planned=t
            return {**opening(t),'reference':entry+gap*.2}
        def bars(self,symbol,start,end):
            rows=[]
            for ts in range(start,end,HOUR):
                h=entry+gap*.4;c=entry+gap*.3
                if self.planned is not None and ts>=self.planned+HOUR:h=target+gap;c=target+gap*.1
                rows.append(bar(ts,open_=entry+gap*.2,low=entry-gap*.05 if self.planned is None else entry+gap*.1,high=h,close=c))
            return {'bars':rows,'fetched_at_ms':clock[0]}
        def funding(self,symbol,start,end):return funding(start,end)
    market=MockMarket()
    p.scan(registry.root,f2root,output,market=market)
    ledger=p.Ledger(output)
    scheduled=next(iter(ledger.states(ledger.load()).values()))
    clock[0]=scheduled.planned_entry_ms-10000
    p.scan(registry.root,f2root,output,market=market)
    clock[0]=scheduled.planned_entry_ms+1000
    result=p.scan(registry.root,f2root,output,market=market)
    assert result['paper']['trades']==1
    s=next(iter(ledger.states(ledger.load()).values()))
    assert s.position['entry_ms']==clock[0] and not s.simulation_admitted
    clock[0]+=2*HOUR
    result=p.scan(registry.root,f2root,output,market=market)
    assert result['paper']['unsettled']==1  # funding upper bracket not yet available
    clock[0]=r.expires_ms+12*HOUR
    result=p.scan(registry.root,f2root,output,market=market)
    assert result['paper']['settled']==1 and result['paper']['net_pnl']>0
    count=len(ledger.load());p.scan(registry.root,f2root,output,market=market)
    assert len(ledger.load())==count
    assert registry.load(r.registration_id)==r


def test_live_revalidation_reuses_original_spec_drift_rules(f1_setup,monkeypatch):
    from tests.test_perpetual_plan import PlanClient, NOW
    r=register_f1(f1_setup)
    monkeypatch.setattr(p.okx,'utc_ms',lambda:NOW)
    market=p.Market(PlanClient(),f2.ScanLimits())
    result=market.refresh(r,{},NOW)
    assert result['validation']['status']=='VALID'
    assert result['validation']['research_snapshot_id']==r.snapshot_id
    assert result['expires_ms']<=NOW+60000


def test_f1_tamper_refuses_paper_before_network(f1_setup,tmp_path):
    r=register_f1(f1_setup);registry=f1_setup[0]
    path=registry.root/(r.registration_id+'.json')
    value=read(path);value['plan_valid']=False;atomic(path,value)
    source=tmp_path/'f2';source.mkdir()
    with pytest.raises(ValueError,match='fingerprint'):
        p.scan(registry.root,source,tmp_path/'f3')


def test_protocol_schema_matches_implementation_and_cost_freeze():
    from pydantic import TypeAdapter
    from pathlib import Path
    assert read(p.ROOT/'docs/forward_paper_v1.json')==p.POLICY
    schema=TypeAdapter(p.PaperProtocol|p.PaperJournal|p.PaperState|p.PaperReport).json_schema()
    assert read(p.ROOT/'docs/forward_f3.schema.json')==schema
    frozen=read(p.ROOT/'docs/okx_d3_strategy_freeze.json')['rules']
    assert p.PROTOCOL.taker_fee_bps==frozen['taker_bps']
    assert p.PROTOCOL.slippage_bps==frozen['slippage_bps']
    assert p.PROTOCOL.max_holding_bars==frozen['maximum_holding_bars']
