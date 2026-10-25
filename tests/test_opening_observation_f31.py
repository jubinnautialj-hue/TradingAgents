"""F3.1 isolated fixtures; never read/write real forward archives or use network."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import requests
from tradingagents import forward_paper_opening_observation as o
from tradingagents import opening_observation_transport as transport
from tradingagents import forward_paper as p,paper_instruments as r
from tradingagents.forward_validation import digest,read
from tests.test_automation import scheduled_case,cfg_for
from tests.test_forward_validation import setup as f1_setup
from tests.test_perpetual_plan import research
from tests.test_forward_paper import T,HOUR,state,opening,proof,bar
from tests.test_paper_instruments import profile,rates


def approval_for(tmp_path,profiles,*,cfg=None,activation=T-2*HOUR,namespace='FIXTURE'):
    root=tmp_path/'f31';root.mkdir(exist_ok=True)
    result=o.Approval(root=str(root.resolve()),activated_ms=activation,namespace=namespace,
        config=o.Config(enabled=True),automation=(cfg or cfg_for(tmp_path,tmp_path/'missing')).model_dump(),
        instruments=r.InstrumentConfig(profiles=profiles).model_dump(),code_hash=o.CODE_HASH,
        git_commit='ISOLATED_DETERMINISTIC_FIXTURE_NOT_A_REAL_BUILD')
    o.audit.seal(root/'approval.json',result.model_dump())
    return result


def scoped_state(tmp_path,symbol='BTC-USDT-SWAP',direction='LONG'):
    profiles=[profile('BTC-USDT-SWAP','.01','1')]
    if symbol!='BTC-USDT-SWAP':profiles.append(profile(symbol,'0.1' if symbol.startswith('ETH') else '1','.01'))
    prof=next(x for x in profiles if x.instrument==symbol)
    approval=approval_for(tmp_path,profiles)
    core=o.engine(prof,tmp_path/'registry',approval)
    s=state(direction);s.info=prof.certificate.specification.copy()
    s.identity.update(instrument=symbol,paper_protocol_hash=core.PROTOCOL.parent_policy_hash,
        opening_observation_policy_hash=core.POLICY_HASH,opening_approval_hash=digest(approval.model_dump()))
    s.revalidation['instrument_specification']={'specification':s.info.copy(),'source':'/api/v5/public/instruments',
        'fetched_ms':s.revalidation['checked_ms']}
    v=s.revalidation['instrument_specification'];v['fingerprint']=digest(v)
    event=opening();event['opening_policy_hash']=core.POLICY_HASH;event['attempt_id']='a'*64
    event['http_evidence']={'http_status':200,'path':'/api/v5/market/candles','query':{'instId':symbol},
        'context':{'attempt_id':event['attempt_id'],'registration_id':s.registration_id},
        'http_dispatched_ms':event['request_started_ms'],'response_received_ms':event['observed_ms']-1,
        'completed_ms':event['observed_ms']}
    return approval,core,s,event


@pytest.mark.parametrize('field',['execution_enabled','allow_new_risk','simulation_admitted'])
def test_risk_flags_never_enabled(field):
    with pytest.raises(ValueError):o.Config.model_validate({field:True})


def test_schema_defaults_and_disabled_status(tmp_path):
    assert o.Config().enabled is False
    assert o.status(tmp_path/'absent')['status']=='DISABLED'
    assert not (tmp_path/'absent').exists()
    assert read(Path('docs/forward_paper_opening_observation_v1.schema.json'))==o.Config.model_json_schema()
    assert read(Path('docs/forward_paper_opening_observation_journal_v1.schema.json'))==o.Journal.model_json_schema()
    for data in ({'window_ms':16000},{'round_offsets_ms':[1000,4000,9000]},{'total_http_budget':100}):
        with pytest.raises(ValueError):o.Config.model_validate(data)


@pytest.mark.parametrize('symbol',['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP'])
@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_fifth_second_fill_cost_units_and_time(tmp_path,symbol,direction):
    approval,core,s,event=scoped_state(tmp_path,symbol,direction)
    event['request_started_ms']=T+5000;event['observed_ms']=T+5041
    event['http_evidence'].update(http_dispatched_ms=T+5001,response_received_ms=T+5039,completed_ms=T+5041)
    opened=core.fill(s,event,T+5042)
    assert opened.position['entry_ms']==T+5041 and opened.position['reference_boundary_ms']==T
    assert opened.position['entry_fee']==pytest.approx(opened.position['base_quantity']*opened.position['entry_price']*.0005)
    assert opened.position['contracts']*float(s.info['ctVal'])==pytest.approx(opened.position['base_quantity'])
    assert s.identity['paper_protocol_hash']==core.PROTOCOL.parent_policy_hash
    assert not opened.allow_new_risk and not opened.simulation_admitted and not opened.execution_enabled
    assert opened.position['entry_ms']!=T


@pytest.mark.parametrize('bad',['late','check_late','stale','unarmed','legacy','http','context','future_spec'])
def test_fill_guards_cannot_be_relaxed(tmp_path,bad):
    approval,core,s,event=scoped_state(tmp_path)
    now=T+1000
    if bad=='late':event['observed_ms']=now=T+15001
    if bad=='check_late':now=T+15001
    if bad=='stale':s.revalidation['expires_ms']=T
    if bad=='unarmed':s.identity.pop('schedule_persisted_ms')
    if bad=='legacy':s.identity.pop('opening_observation_policy_hash')
    if bad=='http':event['http_evidence']['http_status']=500
    if bad=='context':event['http_evidence']['context']['registration_id']='foreign'
    if bad=='future_spec':s.revalidation['instrument_specification']['fetched_ms']=T+100000
    with pytest.raises(ValueError):core.fill(s,event,now)
    assert not s.position


def test_ledger_requires_attempt_and_rejects_old_book(tmp_path):
    approval,core,s,event=scoped_state(tmp_path)
    ledger=core.Ledger(tmp_path/'new-book');events=[]
    ledger.append(events,s.registration_id,'SCHEDULED',{'state':s.model_dump()},T-1000)
    ledger.append(events,s.registration_id,'OPENING_ATTEMPT_STARTED',{'attempt_id':event['attempt_id']},T+100)
    opened=core.fill(s,event,T+1000)
    ledger.append(events,s.registration_id,'OPEN',{'state':opened.model_dump()},T+1000)
    assert ledger.states(ledger.load())[s.registration_id].position['entry_ms']==T+1000
    legacy=p.Ledger(tmp_path/'legacy','FIXTURE');old=[]
    legacy.append(old,s.registration_id,'SCHEDULED',{'state':state().model_dump()},T-1000)
    before=legacy.path.read_bytes()
    with pytest.raises(ValueError):core.Ledger(tmp_path/'legacy').load()
    assert legacy.path.read_bytes()==before and legacy.load()==old


@pytest.mark.parametrize('symbol',['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP'])
def test_parent_exit_and_actual_funding_reused(tmp_path,symbol):
    approval,core,s,event=scoped_state(tmp_path,symbol)
    s=core.fill(s,event,T+1000)
    s=core.advance_bar(s,bar(),T+HOUR)
    s=core.advance_bar(s,bar(T+HOUR,high=125),T+2*HOUR)
    result=core.settle(s,rates(symbol,end=T+2*HOUR),[bar(),bar(T+HOUR,high=125)],as_of_ms=T+20*HOUR)
    assert result.status=='SETTLED'
    assert result.exit['net_pnl']==pytest.approx(result.exit['gross_pnl_after_slippage']-result.position['entry_fee']-
        result.exit['exit_fee']+result.funding['cashflow_usdt'])
    assert all(x['ts']>result.position['entry_ms'] for x in result.funding['charges'])
    assert core.advance_bar is not None and core.settle is not None


@pytest.fixture
def flow(scheduled_case,tmp_path,monkeypatch):
    cfg,old_source,record,old_state,old_ledger,clock,original_market=scheduled_case
    monkeypatch.setattr(o,'_reviewed',lambda commit:None)  # fixture gate ONLY; CLI still rejects these approvals
    info=record.quality_evidence['market_snapshot']['instrument']
    prof=r.InstrumentProfile(instrument='BTC-USDT-SWAP',enabled=True,
        certificate=r.Certificate(instrument='BTC-USDT-SWAP',fetched_ms=record.cutoff_ms,
            specification=r.specification(info,'BTC-USDT-SWAP'),public_evidence_hash='a'*64))
    # Full-chain fixtures use native PRODUCTION schemas ONLY in pytest's private
    # temporary directories; this approval is explicitly not a production build.
    ap=approval_for(tmp_path,[prof],cfg=cfg,activation=record.registered_ms-1,namespace='PRODUCTION')
    _,sources=o.routes(ap);source=sources[0];core=source['_paper_engine']
    s=core.create_state(record,old_state.trigger,old_state.revalidation,clock[0])
    s.identity['schedule_persisted_ms']=clock[0]
    ledger=core.Ledger(source['f3']);events=[]
    ledger.append(events,record.registration_id,'SCHEDULED',{'state':s.model_dump()},clock[0])
    target=s.planned_entry_ms;clock[0]=target-80000
    def sleep(seconds):clock[0]+=max(1,round(seconds*1000))
    budget=transport.WireBudget(Path(ap.root)/'windows'/str(target),digest(ap.model_dump()),clock=lambda:clock[0],sleep=sleep)
    client=transport.PublicClient(budget,deadline=target+15000,clock=lambda:clock[0],monotonic=lambda:clock[0]/1000)
    settings={'visible_round':1,'http':200,'malformed':False,'delay':10,'partial':False};calls=[]
    def send(self,request,**kwargs):
        from urllib.parse import urlsplit
        path=urlsplit(request.url).path;before=clock[0];clock[0]+=(10 if path.endswith('/time') else settings['delay'])
        response=requests.Response();response.status_code=200 if path.endswith('/time') else settings['http'];response._content_consumed=True
        if path.endswith('/time'):data=[{'ts':str(clock[0])}]
        else:
            calls.append(before)
            n=len(calls);ts=target if settings['visible_round'] is not None and n>=settings['visible_round'] else target-HOUR
            price=record.original_result['entry_reference']['price']
            data=[[str(ts),str(price),'1','1','1','1','.01','1','0']]
        response._content=(b'broken' if settings['malformed'] and not path.endswith('/time') else json.dumps({'code':'0','data':data}).encode())
        if settings['partial'] and not path.endswith('/time'):
            def broken(chunk_size):yield b'{';raise requests.ConnectionError('SECRET')
            response.iter_content=broken
        return response
    monkeypatch.setattr(requests.Session,'send',send)
    class Market:
        now=original_market.now
        bars=original_market.bars
        def refresh(self,*args):
            p=original_market.refresh(*args)
            v={'specification':prof.certificate.specification,'source':'/api/v5/public/instruments','fetched_ms':p['checked_ms']}
            v['fingerprint']=digest(v);p['instrument_specification']=v;return p
        def opening(self,*args):return core.Market(client,tracking_limits()).opening(*args)
    market=Market()
    def run():return o.timely(ap,client=client,sleep=sleep,monotonic=lambda:clock[0]/1000,market=market)
    return SimpleNamespace(ap=ap,source=source,core=core,ledger=ledger,record=record,clock=clock,target=target,
        settings=settings,calls=calls,budget=budget,client=client,run=run,market=market)


def tracking_limits():return o.tracking.ScanLimits(max_requests=60,max_seconds=120)


@pytest.mark.parametrize('visible',[1,2,3,None])
def test_rounds_first_fifth_ninth_or_missing(flow,visible):
    f=flow;f.settings['visible_round']=visible
    r=f.run();s=f.ledger.states(f.ledger.load())[f.record.registration_id]
    if visible is None:
        assert s.status=='SCHEDULED' and not s.position and len(f.calls)==3
    else:
        expected=[1000,5000,9000][visible-1]+10
        assert s.status=='OPEN' and s.position['entry_ms']==f.target+expected
        assert len(f.calls)==visible
    before=f.budget.status()['count'];f.run()
    assert f.budget.status()['count']==before and len(f.calls)==(visible or 3)
    assert not r['execution_enabled']


@pytest.mark.parametrize('kind',['http','parse','partial','late'])
def test_http_parse_partial_late_no_fake_fill(flow,kind):
    f=flow
    if kind=='http':f.settings['http']=500
    if kind=='parse':f.settings['malformed']=True
    if kind=='partial':f.settings['partial']=True
    if kind=='late':f.settings['delay']=16000
    f.run();s=f.ledger.states(f.ledger.load())[f.record.registration_id]
    assert not s.position and s.status!='OPEN'
    assert all('SECRET' not in json.dumps(o.audit.unseal(path)) for path in f.budget.root.glob('*.result.json'))


def test_budget_reserves_before_dispatch(flow):
    f=flow
    value={'policy_hash':f.budget.policy_hash,'count':60,'quote_count':0,'last_ms':0,'blocked':False}
    o.audit.seal(f.budget.path,value)
    f.run()
    assert not f.calls and not f.ledger.states(f.ledger.load())[f.record.registration_id].position


def test_interrupted_attempt_never_replayed(flow,monkeypatch):
    f=flow;f.clock[0]=f.target+1000
    def crash(*args,**kwargs):raise KeyboardInterrupt()
    original=o.paper_timing.step;monkeypatch.setattr(o.paper_timing,'step',crash)
    with pytest.raises(KeyboardInterrupt):o._attempt(f.source,f.record.registration_id,0,f.target,f.ap,
        o.automation.AutomationConfig.model_validate(f.ap.automation),f.client,market=f.market)
    monkeypatch.setattr(o.paper_timing,'step',original)
    r=f.run()
    assert not f.calls and not f.ledger.states(f.ledger.load())[f.record.registration_id].position
    assert r['status']=='NO_SAFE_PENDING_ATTEMPTS'
    assert r['excluded'][0]['reason']=='INTERRUPTED_UNCONFIRMED_ATTEMPT'


def test_init_is_opt_in_and_no_production_activation_of_uncommitted_code(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'registry')
    profiles=r.InstrumentConfig(profiles=[profile('BTC-USDT-SWAP')])
    with pytest.raises(ValueError,match='enable'):o.initialize(tmp_path/'disabled',cfg,profiles)
    assert not (tmp_path/'disabled').exists()
    with pytest.raises(ValueError,match='Reviewed'):o.initialize(tmp_path/'production',cfg,profiles,enable=True)
    assert not (tmp_path/'production').exists()
    approval=o.initialize(tmp_path/'fixture',cfg,profiles,enable=True,namespace='FIXTURE',now=T)
    assert o.load(tmp_path/'fixture')==approval
    result=o.status(tmp_path/'fixture')
    assert result['namespace']=='FIXTURE' and all(x['report']['paper']['trades']==0 for x in result['books'])
    assert all(x['report']['paper']['net_pnl'] is None for x in result['books'])
    assert not (tmp_path/'fixture'/'books').exists()  # read-only zero-sample query


def test_existing_registration_cannot_be_silently_admitted(flow):
    f=flow
    assert 'REGISTERED_BEFORE_OBSERVATION_ACTIVATION' in o.engine(
        r.InstrumentConfig.model_validate(f.ap.instruments).profiles[0],f.source['registry'],
        f.ap.model_copy(update={'activated_ms':f.record.registered_ms+1})).admission(
            f.record,f.ledger.states(f.ledger.load())[f.record.registration_id].trigger,f.target)


def test_phase_mutex_across_processes(flow):
    import subprocess,sys
    f=flow
    code='import sys,json;import tradingagents.forward_paper_opening_observation as o;o._reviewed=lambda commit:None;print(json.dumps(o.timely(o.Approval.model_validate(json.load(sys.stdin)))))'
    with o.locked(Path(f.ap.root)/'phase'):
        proc=subprocess.run([sys.executable,'-c',code],input=f.ap.model_dump_json(),capture_output=True,text=True,timeout=30)
    assert proc.returncode==0 and json.loads(proc.stdout)['status']=='BUSY'
    assert not f.calls


def test_fill_after_boundary_fifteen_rejected_even_if_body_on_time(tmp_path):
    approval,core,s,event=scoped_state(tmp_path)
    event['observed_ms']=T+14999
    event['http_evidence'].update(response_received_ms=T+14998,completed_ms=T+14999)
    with pytest.raises(ValueError):core.fill(s,event,T+15001)
    assert not s.position


def test_quote_row_parse_error_is_non_retryable(flow):
    f=flow
    original=requests.Session.send
    def bad(self,request,**kwargs):
        response=original(self,request,**kwargs)
        if '/market/candles' in request.url:response._content=json.dumps({'code':'0','data':[None]}).encode()
        return response
    # original is the isolated fake sender installed by flow, not the network.
    import unittest.mock
    with unittest.mock.patch.object(requests.Session,'send',bad):f.run()
    final=f.ledger.states(f.ledger.load())[f.record.registration_id]
    assert not final.position and len(f.calls)==1


def test_no_core_or_legacy_protocol_bytes_modified():
    import subprocess
    names=['tradingagents/forward_paper.py','tradingagents/paper_instruments.py','tradingagents/paper_timing.py',
           'tradingagents/forward_tracking.py','tradingagents/forward_validation.py','tradingagents/dataflows/plan_lifecycle.py']
    for name in names:
        original=subprocess.check_output(['git','show','b01436cc096af4bb5f64bea9e9c39b02dfa35feb:'+name])
        assert (Path(name).read_bytes().replace(b'\r\n',b'\n')==original.replace(b'\r\n',b'\n'))
    assert p.POLICY_HASH=='e29998a373c1295791116b026a04f18c4067f5c14a1352cfad2a4b93066dbd64'


@pytest.mark.parametrize('btc_failure',['missing','http','parse'])
def test_three_asset_full_fixture_chain_failure_isolation_and_settlement(tmp_path_factory,monkeypatch,btc_failure):
    """PRODUCTION-shaped records only in isolated pytest dirs, never real samples."""
    tmp_path=tmp_path_factory.mktemp("f31-assets")
    monkeypatch.setattr(o,'_reviewed',lambda commit:None)  # only isolated mock-chain test
    from tests.test_perpetual_plan import PlanClient,DAY,CONFIG,choice,NOW
    from tests.test_observation_eligibility import CandleClient,bar as rawbar
    from tradingagents import forward_validation as f1
    clock=[NOW]
    monkeypatch.setattr(p.okx,'utc_ms',lambda:clock[0]);monkeypatch.setattr(p.deriv,'utc_ms',lambda:clock[0])
    monkeypatch.setattr(o.audit,'utc_ms',lambda:clock[0]);monkeypatch.setattr(f1,'utc_ms',lambda:clock[0])
    profiles=[profile(symbol,value) for symbol,value in [('BTC-USDT-SWAP','.01'),('ETH-USDT-SWAP','.1'),('SOL-USDT-SWAP','1')]]
    records={};inputs={};triggers={};sources=[]
    for prof in profiles:
        symbol=prof.instrument;prof.certificate.fetched_ms=NOW-1000
        asset=tmp_path/'inputs'/symbol;asset.mkdir(parents=True)
        class AssetClient(PlanClient):
            def instrument(self,requested):return prof.certificate.specification.copy()
            def candles(self,requested,interval,cutoff,count):
                rows=super().candles('BTC-USDT-SWAP',interval,cutoff,count)
                for row in rows:
                    row[6]=str(float(row[5])*float(prof.certificate.specification['ctVal']))
                    row[7]=str(float(row[6])*float(row[4]))
                return rows
            def get(self,path,params=None):
                rows=super().get(path,params);mult=float(prof.certificate.specification['ctVal'])
                for row in rows:
                    if isinstance(row,dict) and 'instId' in row:
                        row['instId']=symbol.removesuffix('-SWAP') if path==p.deriv.INDEX else symbol
                        if path==p.deriv.OI:row.update(oiCcy=str(float(row['oi'])*mult),oiUsd=str(float(row['oi'])*mult*1000))
                    if path==p.deriv.OI_HISTORY:row[2]=str(float(row[1])*mult)
                return rows
        clock[0]=NOW
        bundle=p.okx.build_bundle(symbol,DAY,CONFIG,AssetClient())
        assert bundle['status']=='VALID'
        raw={'company_of_interest':symbol,'trade_date':DAY,'market_snapshot':bundle,'position_context':'UNKNOWN',
             'research_id':symbol+'-F31-ISOLATED-FIXTURE','portfolio_choice':choice(),'llm_status':'AVAILABLE','plan_mode':True}
        result,_=p.plans.finalize_result(raw,bundle,CONFIG);assert result.plan_valid
        raw.update(research_result=result.model_dump(mode='json'),data_quality='VALID',workflow_status='COMPLETED')
        report=asset/'report';report.mkdir();(report/'complete_report.md').write_text('DETERMINISTIC FIXTURE')
        o.audit.atomic(report/'research_result.json',raw['research_result']);o.audit.atomic(report/'multitimeframe_snapshot.json',bundle)
        registry=f1.Registry(asset/'f1');clock[0]=NOW-1000;token=registry.begin(symbol,CONFIG);clock[0]=NOW+2000
        record=registry.register(report,raw,token=token,completed_ms=NOW+1000)
        entry=record.original_result['entry_reference']['price'];gap=entry-record.original_result['stop_loss']['price']
        f2root=asset/'f2';clock[0]=record.first_check_ms+1000
        p.f2.scan(registry.root,f2root,client=CandleClient(clock,[rawbar(record.first_bar_open_ms,entry,gap)]))
        clock[0]+=HOUR
        p.f2.scan(registry.root,f2root,client=CandleClient(clock,[rawbar(record.first_bar_open_ms+HOUR,entry,gap,True)]))
        triggers[symbol]=p.f2.read_condition_events(registry.root,f2root,record.registration_id)[0].model_dump()
        records[symbol]=record;inputs[symbol]=(registry.root,f2root)
        sources.append(o.automation.ArchiveSource(name=symbol.lower(),registry=str(registry.root)))
    cfg=o.automation.AutomationConfig(batch_root=str(tmp_path/'batch'),state_root=str(tmp_path/'old-state'),sources=sources)
    ap=approval_for(tmp_path,profiles,cfg=cfg,activation=NOW-2000,namespace='PRODUCTION')
    # Override ONLY F2 routing in this isolated test, since the real routes are
    # defined by R1.2's state directory, not the test asset input folders.
    real_routes=o.routes
    def isolated_routes(approval):
        config,values=real_routes(approval)
        for source in values:
            root_symbol=next(symbol for symbol,(registry,_) in inputs.items() if str(registry)==source['registry'])
            source['f2']=str(inputs[root_symbol][1])
        return config,values
    monkeypatch.setattr(o,'routes',isolated_routes)
    _,routed=o.routes(ap);selected={}
    for source in routed:
        symbol=source['instrument']
        if source['registry']!=str(inputs[symbol][0]):continue
        core=source['_paper_engine'];record=records[symbol]
        s=core.create_state(record,triggers[symbol],proof(clock[0]-1),clock[0]);s.identity['schedule_persisted_ms']=clock[0]
        ledger=core.Ledger(source['f3']);events=[];ledger.append(events,record.registration_id,'ARMED',{'state':s.model_dump()},clock[0])
        selected[symbol]=(source,core,ledger,s)
    target=next(iter(selected.values()))[3].planned_entry_ms
    assert all(v[3].planned_entry_ms==target for v in selected.values())
    clock[0]=target-80000
    def sleep(seconds):clock[0]+=max(1,round(seconds*1000))
    budget=transport.WireBudget(Path(ap.root)/'windows'/str(target),digest(ap.model_dump()),clock=lambda:clock[0],sleep=sleep)
    client=transport.PublicClient(budget,deadline=target+15000,clock=lambda:clock[0],monotonic=lambda:clock[0]/1000)
    counts={symbol:0 for symbol in o.SYMBOLS}
    def send(self,request,**kwargs):
        from urllib.parse import urlsplit,parse_qs
        url=urlsplit(request.url);clock[0]+=10
        if url.path.endswith('/time'):rows=[{'ts':str(clock[0])}]
        else:
            symbol=parse_qs(url.query)['instId'][0];counts[symbol]+=1
            visible=symbol=='SOL-USDT-SWAP' or symbol=='ETH-USDT-SWAP' and counts[symbol]>=2
            ts=target if visible else target-HOUR
            price=records[symbol].original_result['entry_reference']['price']
            rows=[[str(ts),str(price),'1','1','1','1','1','1','0']]
        response=requests.Response();response.status_code=200;response._content_consumed=True
        if not url.path.endswith('/time') and symbol=='BTC-USDT-SWAP':
            if btc_failure=='http':response.status_code=500
            if btc_failure=='parse':rows=[None]
        response._content=json.dumps({'code':'0','data':rows}).encode();return response
    monkeypatch.setattr(requests.Session,'send',send)
    class Market:
        def now(self):return clock[0]
        def refresh(self,record,*args):
            value=proof(clock[0]-1);info=next(v.certificate.specification for v in profiles if v.instrument==record.instrument)
            v={'specification':info,'source':'/api/v5/public/instruments','fetched_ms':clock[0]-1};v['fingerprint']=digest(v)
            value['instrument_specification']=v;return value
        def bars(self,symbol,start,end):
            record=records[symbol];entry=record.original_result['entry_reference']['price'];gap=entry-record.original_result['stop_loss']['price']
            bars=[]
            for ts in range(start,end,HOUR):
                high=record.original_result['take_profit'][0]['price']+gap if ts>=target+HOUR else entry+gap*.4
                close=high-gap*.1 if ts>=target+HOUR else entry+gap*.2
                bars.append(bar(ts,open_=entry,low=entry-gap*.05,high=high,close=close))
            return {'bars':bars,'fetched_at_ms':clock[0]}
        def opening(self,symbol,when):return selected[symbol][1].Market(client,tracking_limits()).opening(symbol,when)
        def funding(self,symbol,start,end):return rates(symbol,start,end)
    frozen={str(path):path.read_bytes() for registry,f2 in inputs.values() for root in (registry,f2) for path in root.rglob('*.json')}
    result=o.timely(ap,client=client,sleep=sleep,monotonic=lambda:clock[0]/1000,market=Market())
    states={symbol:v[2].states(v[2].load())[records[symbol].registration_id] for symbol,v in selected.items()}
    assert states['BTC-USDT-SWAP'].status=='SCHEDULED' and not states['BTC-USDT-SWAP'].position
    assert states['ETH-USDT-SWAP'].status==states['SOL-USDT-SWAP'].status=='OPEN'
    assert counts=={'BTC-USDT-SWAP':3 if btc_failure=='missing' else 1,'ETH-USDT-SWAP':2,'SOL-USDT-SWAP':1}
    assert states['ETH-USDT-SWAP'].position['entry_ms']>=target+5000
    assert result['http_budget']['quote_count']==(6 if btc_failure=='missing' else 4)
    client.deadline=None
    clock[0]=target+2*HOUR
    for symbol,(source,core,ledger,_) in selected.items():
        core.scan(source['registry'],source['f2'],source['f3'],market=Market())
    clock[0]=max(v.expires_ms for v in records.values())+20*HOUR
    for symbol,(source,core,ledger,_) in selected.items():
        r1=core.scan(source['registry'],source['f2'],source['f3'],market=Market())
        if symbol=='BTC-USDT-SWAP':assert ledger.states(ledger.load())[records[symbol].registration_id].status=='MISSED'
        else:assert r1['paper']['settled']==1
        before=ledger.path.read_bytes();core.scan(source['registry'],source['f2'],source['f3'],market=Market());assert ledger.path.read_bytes()==before
    assert frozen=={str(path):path.read_bytes() for registry,f2 in inputs.values() for root in (registry,f2) for path in root.rglob('*.json')}



def test_reservation_without_dispatch_is_not_counted_as_wire_send(tmp_path,monkeypatch):
    clock=[T+1000]
    budget=transport.WireBudget(tmp_path/'budget','p',clock=lambda:clock[0],sleep=lambda _:None)
    original=budget.dispatch
    def reserve(*args):
        value=original(*args);clock[0]=T+15001;return value
    monkeypatch.setattr(budget,'dispatch',reserve)
    def forbidden(*args,**kwargs):raise AssertionError('No request may leave after deadline')
    monkeypatch.setattr(requests.Session,'send',forbidden)
    client=transport.PublicClient(budget,deadline=T+15000,clock=lambda:clock[0]);client.context={'test':'fixture'}
    with pytest.raises(transport.PublicFailure):client.get('/api/v5/market/candles',{'instId':'BTC-USDT-SWAP'})
    assert client.last_record['actual_http_attempts']==0
    assert 'http_dispatched_ms' not in client.last_record
    assert budget.status()['count']==1  # consumed conservatively, but not a wire send


def test_round_cannot_dispatch_before_its_grid(flow):
    f=flow;f.clock[0]=f.target+1000
    result=o._attempt(f.source,f.record.registration_id,1,f.target,f.ap,
        o.automation.AutomationConfig.model_validate(f.ap.automation),f.client,market=f.market)
    assert result['status']=='NOT_DUE' and not f.calls
    assert not any(e['kind']=='OPENING_ATTEMPT_STARTED' for e in f.ledger.load())


def test_python_api_cannot_bypass_disabled_or_unsealed_approval(tmp_path):
    approval,core,s,event=scoped_state(tmp_path)
    with pytest.raises(ValueError,match='disabled'):o.routes(approval.model_copy(update={'config':o.Config(enabled=False)}))
    with pytest.raises(ValueError):o.routes(approval.model_copy(update={'activated_ms':approval.activated_ms+1}))
    wrong=profile('BTC-USDT-SWAP','1','1')
    with pytest.raises(ValueError,match='Profile'):o.engine(wrong,tmp_path/'registry',approval)


def test_budget_rollback_conflicts_do_not_overwrite_send_intents(tmp_path):
    now=[T]
    budget=transport.WireBudget(tmp_path,'p',clock=lambda:now[0],sleep=lambda x:now.__setitem__(0,now[0]+round(x*1000)))
    first=budget.dispatch('/api/v5/public/time',{},None,None)
    old=(tmp_path/(first['request_id']+'.send.json')).read_bytes()
    o.audit.seal(budget.path,{'policy_hash':'p','count':0,'quote_count':0,'last_ms':0,'blocked':False})
    with pytest.raises(transport.PublicFailure,match='SEQUENCE'):budget.dispatch('/api/v5/public/time',{},None,None)
    assert (tmp_path/(first['request_id']+'.send.json')).read_bytes()==old


def test_public_write_path_is_unreachable_without_network(tmp_path):
    budget=transport.WireBudget(tmp_path,'p')
    client=transport.PublicClient(budget)
    with pytest.raises(transport.PublicFailure,match='NON_PUBLIC'):client.get('/api/v5/trade/order')
    assert budget.status()['count']==0


def test_unexpected_refresh_exception_is_paused_without_fabricating_proof(flow):
    f=flow
    def invalid(*args):raise TypeError('SECRET')
    f.market.refresh=invalid
    result=f.run()
    s=f.ledger.states(f.ledger.load())[f.record.registration_id]
    assert not s.position and not any('SECRET' in json.dumps(x) for x in result['results'])
    assert any(x.get('error_type')=='TypeError' for x in result['results'])


def test_cli_errors_do_not_echo_unexpected_config_values(tmp_path,monkeypatch,capsys):
    def invalid():raise ValueError('SECRET_TOKEN_MUST_NOT_APPEAR')
    monkeypatch.setattr(o,'_cli',invalid)
    with pytest.raises(SystemExit) as error:o.main()
    assert error.value.code==2
    output=capsys.readouterr().out
    assert 'SECRET_TOKEN' not in output and json.loads(output)['status']=='BLOCKED'
