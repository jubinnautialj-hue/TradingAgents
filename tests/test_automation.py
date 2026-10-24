"""R1.2 isolated orchestration. No installed timers, real model or private API."""
import copy
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from tradingagents import automation as a,paper_timing as timing,forward_paper as p,forward_tracking as f2
from tradingagents import auto_research as r11
from tradingagents.forward_validation import Registry,atomic,read,digest
from tradingagents.automation_schedule import export,next_trigger
from tests.test_forward_validation import setup as f1_setup,register as register_f1
from tests.test_perpetual_plan import research
from tests.test_observation_eligibility import CandleClient,bar as rawbar
from tests.test_forward_paper import proof


def cfg_for(root,registry):
    return a.AutomationConfig(state_root=str(root/'state'),batch_root=str(root/'batch'),
                              sources=[a.ArchiveSource(name='live',registry=str(registry))])


def test_schema_default_and_three_symbols():
    cfg=a.AutomationConfig.model_validate(read(a.ROOT/'docs/automation_r12.json'))
    assert read(a.ROOT/'docs/automation_r12.schema.json')==a.AutomationConfig.model_json_schema()
    assert r11.Watchlist.model_validate(read(a.expand(cfg.watchlist))).enabled()==['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP']
    assert cfg.schedule.research_seconds==14400
    assert not cfg.execution_enabled and not cfg.simulation_admitted and not cfg.allow_new_risk


def test_readonly_status_and_dry_run_do_not_create_files(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'missing')
    assert not (tmp_path/'state').exists()
    value=a.status(cfg)
    assert value['counts']['f1_wait']==0
    assert a.dispatch(cfg,'research',dry_run=True)['status']=='DRY_RUN'
    assert not (tmp_path/'state').exists() and not (tmp_path/'missing').exists()


def test_paths_alias_and_overlap(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'registry')
    cfg.sources.append(a.ArchiveSource(name='alias',registry=str(tmp_path/'registry')))
    assert a.paths(cfg)[1]['alias_of']=='live'
    cfg.sources[0].registry=cfg.state_root
    with pytest.raises(ValueError):a.paths(cfg)


def test_discover_f1_identity_and_no_archive_writes(f1_setup,tmp_path):
    record=register_f1(f1_setup);cfg=cfg_for(tmp_path,f1_setup[0].root)
    before={p.name:p.read_bytes() for p in f1_setup[0].root.iterdir() if p.is_file()}
    value=a.discover(cfg,persist=True)
    assert value['records'][record.registration_id]['content_hash']==record.content_hash
    assert value['sources'][0]['status']=='READY'
    assert before=={p.name:p.read_bytes() for p in f1_setup[0].root.iterdir() if p.is_file()}


def test_duplicate_registry_quarantine_and_frozen_owner(f1_setup,tmp_path):
    r=register_f1(f1_setup);cfg=cfg_for(tmp_path,f1_setup[0].root)
    a.discover(cfg,persist=True)
    copy_root=tmp_path/'copy';shutil.copytree(f1_setup[0].root,copy_root)
    cfg.sources.insert(0,a.ArchiveSource(name='copied',registry=str(copy_root)))
    value=a.discover(cfg,persist=True)
    assert value['records'][r.registration_id]['source']=='live'
    assert value['sources'][0]['status']=='DUPLICATE_REGISTRY_QUARANTINED'


def test_missing_record_is_not_forgotten(f1_setup,tmp_path):
    r=register_f1(f1_setup);cfg=cfg_for(tmp_path,f1_setup[0].root)
    a.discover(cfg,persist=True)
    (f1_setup[0].root/(r.registration_id+'.json')).unlink()
    first=a.discover(cfg,persist=True);second=a.discover(cfg,persist=True)
    assert first['sources'][0]['status']==second['sources'][0]['status']=='MISSING_SEALED_RECORDS'
    assert r.registration_id in second['records']


def test_tamper_and_binding_change_refuse_progress(f1_setup,tmp_path):
    r=register_f1(f1_setup);cfg=cfg_for(tmp_path,f1_setup[0].root)
    a.discover(cfg,persist=True)
    path=f1_setup[0].root/(r.registration_id+'.json');raw=read(path);raw['plan_valid']=False;atomic(path,raw)
    assert a.discover(cfg)['sources'][0]['status']=='INVALID_SOURCE'
    cfg.sources[0].registry=str(tmp_path/'another')
    assert a.discover(cfg)['sources'][0]['status']=='SOURCE_BINDING_CONFLICT'


def test_history_replay_not_counted_as_forward(f1_setup,tmp_path):
    registry,folder,state,token,clock,save=f1_setup
    registry.register(folder,state,origin='TEST_REPLAY')
    value=a.status(cfg_for(tmp_path,registry.root))
    assert value['counts']['excluded_origins']==1 and not value['latest_research']


def test_pause_is_independent_and_resumable(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'registry')
    a.set_pause(cfg,'research',True)
    assert a.paused(cfg,'research') and not a.paused(cfg,'observe')
    assert a.dispatch(cfg,'research')['status']=='PAUSED'
    a.set_pause(cfg,'research',False);assert not a.paused(cfg,'research')


def test_research_failure_does_not_block_observation(tmp_path,monkeypatch):
    cfg=cfg_for(tmp_path,tmp_path/'registry');a.set_pause(cfg,'research',True)
    assert a.execute_phase(cfg,'observe')['status']=='COMPLETED'
    assert not a.paused(cfg,'observe')


def test_slow_research_lock_does_not_block_f2(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'registry');root=a.expand(cfg.state_root);root.mkdir()
    with a.locked(root/'research.phase'):
        assert a.execute_phase(cfg,'observe')['status']=='COMPLETED'


def test_reserved_opening_interval():
    cfg=a.AutomationConfig(sources=[a.ArchiveSource(name='x')]);t=p.HOUR*500000
    assert a.reserved(cfg,t-80000) and a.reserved(cfg,t+15000)
    assert not a.reserved(cfg,t+30000) and not a.reserved(cfg,t-130000)


@pytest.fixture
def scheduled_case(f1_setup,tmp_path_factory,monkeypatch):
    r=register_f1(f1_setup);clock=f1_setup[4]
    root=tmp_path_factory.mktemp('r12')
    cfg=cfg_for(root,f1_setup[0].root);source=a.paths(cfg)[0]
    monkeypatch.setattr(p.okx,'utc_ms',lambda:clock[0]);monkeypatch.setattr(r11,'utc_ms',lambda:clock[0])
    entry=r.original_result['entry_reference']['price'];gap=entry-r.original_result['stop_loss']['price']
    clock[0]=r.first_check_ms+1000
    f2.scan(r.registry_root if hasattr(r,'registry_root') else f1_setup[0].root,source['f2'],client=CandleClient(clock,[rawbar(r.first_bar_open_ms,entry,gap)]))
    clock[0]+=p.HOUR
    f2.scan(f1_setup[0].root,source['f2'],client=CandleClient(clock,[rawbar(r.first_bar_open_ms+p.HOUR,entry,gap,True)]))
    trigger=f2.read_condition_events(f1_setup[0].root,source['f2'],r.registration_id)[0].model_dump()
    s=p.create_state(r,trigger,proof(clock[0]-1),clock[0]);ledger=p.Ledger(source['f3']);events=[]
    ledger.append(events,r.registration_id,'SCHEDULED',{'state':s.model_dump()},clock[0])
    s.identity['schedule_persisted_ms']=clock[0]
    ledger.append(events,r.registration_id,'ARMED',{'state':s.model_dump()},clock[0])
    class Market:
        invalid=False;late=False;drift=False;opens=0;calls=[]
        def now(self):return clock[0]+(31000 if self.drift else 0)
        def bars(self,symbol,start,end):
            rows=[]
            for ts in range(start,end,p.HOUR):
                close=entry+gap*.2
                if self.invalid:close=r.original_result['stop_loss']['price']-gap
                rows.append({'ts':ts,'open':entry,'high':entry+gap,'low':min(entry-gap*.1,close),'close':close,'confirm':'1'})
            return {'bars':rows,'fetched_at_ms':clock[0]}
        def refresh(self,*args):self.calls.append('refresh');return proof(clock[0]-1)
        def opening(self,symbol,target):
            self.calls.append('open');self.opens+=1
            if self.late:clock[0]=target+16000
            return {'reference':entry+gap*.2,'boundary_ms':target,'request_started_ms':target+100,
                    'observed_ms':clock[0],'collection_mode':'CONTEMPORANEOUS'}
    return cfg,source,r,s,ledger,clock,Market()


def test_timing_warm_refresh_open_and_no_duplicate(scheduled_case):
    cfg,source,r,s,ledger,clock,market=scheduled_case
    target=s.planned_entry_ms;clock[0]=target-80000
    def sleep(seconds):clock[0]+=max(1,int(seconds*1000))
    # A long-running research lane lock does not block this targeted market lane.
    a.expand(cfg.state_root).mkdir(parents=True,exist_ok=True)
    with a.locked(a.expand(cfg.state_root)/'research.phase'):
        result=timing.warm_window(cfg,[source],market=market,sleep=sleep)
    states=ledger.states(ledger.load());opened=states[r.registration_id]
    assert opened.status=='OPEN' and market.calls==['refresh','open']
    assert opened.position['entry_ms']==target+1000 and not opened.simulation_admitted and not opened.allow_new_risk
    assert result['results'][-1]['price_observed_ms']==target+1000
    assert timing.step(cfg,source,r.registration_id,'open',market,scheduled_for_ms=target+1000)['status']=='NOT_SCHEDULED'
    assert market.opens==1


@pytest.mark.parametrize('case',['missed','expired','invalid','late','stale','drift'])
def test_untimely_invalid_or_stale_never_fills(scheduled_case,case):
    cfg,source,r,s,ledger,clock,market=scheduled_case;target=s.planned_entry_ms
    clock[0]=target-40000
    if case!='stale':timing.step(cfg,source,r.registration_id,'refresh',market,scheduled_for_ms=clock[0])
    clock[0]=target+1000
    if case=='missed':clock[0]=target+16000
    if case=='expired':clock[0]=r.expires_ms
    if case=='invalid':market.invalid=True
    if case=='late':market.late=True
    if case=='drift':market.drift=True
    result=timing.step(cfg,source,r.registration_id,'open',market,scheduled_for_ms=target+1000)
    final=ledger.states(ledger.load())[r.registration_id]
    assert final.status!='OPEN' and not final.position
    if case=='missed':assert final.status=='MISSED'
    if case in {'expired','invalid'}:assert final.status=='CANCELLED'


def test_f2_evidence_tamper_prevents_fill(scheduled_case):
    cfg,source,r,s,ledger,clock,market=scheduled_case
    path=Path(source['f2'])/'production'/(r.registration_id+'.events.json')
    value=read(path);value['events'][0]['evidence_fingerprint']='tampered';atomic(path,value)
    clock[0]=s.planned_entry_ms+1000
    result=timing.step(cfg,source,r.registration_id,'open',market,scheduled_for_ms=clock[0])
    assert result['status']=='PAUSED' and market.opens==0


def test_no_new_eth_sol_paper_admission(scheduled_case):
    cfg,source,r,s,ledger,clock,market=scheduled_case
    for instrument in ('ETH-USDT-SWAP','SOL-USDT-SWAP'):
        modified=r.model_copy(update={'instrument':instrument})
        assert 'PAPER_FUNDING_AUDIT_BTC_ONLY' in p.admission(modified,s.trigger,clock[0])


def test_old_f2_chain_not_migrated(scheduled_case):
    cfg,source,r,s,ledger,clock,market=scheduled_case
    path=Path(source['f2'])/'production'/(r.registration_id+'.events.json')
    value=read(path);value['version']='forward-f2-v1';atomic(path,value);before=path.read_bytes()
    with pytest.raises(f2.LegacyTrackingChain):a.readonly_tracking(source['f2'],r)
    assert path.read_bytes()==before


def test_empty_timely_does_not_query_market(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'missing');source=a.paths(cfg)[0]
    class Never:
        def now(self):raise AssertionError('No scheduled candidates')
    assert timing.warm_window(cfg,[source],market=Never())['status']=='NO_PENDING_OPEN_IN_WINDOW'


def test_export_only_templates_and_time_boundaries(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'missing');file=tmp_path/'cfg.json';atomic(file,cfg.model_dump())
    result=export(cfg,file,tmp_path/'templates')
    assert result['installed'] is False and len(result['files'])==12
    for path in (tmp_path/'templates').glob('*.xml'):
        text=path.read_text(encoding='utf-16');assert 'IgnoreNew' in text and 'StartWhenAvailable>false' in text
    timer=(tmp_path/'templates/r12-timely.timer').read_text(encoding='utf-8')
    assert 'AccuracySec=1s' in timer and 'RandomizedDelaySec=0' in timer and 'Persistent=false' in timer
    assert next_trigger(100000,60,2)==122000


def test_same_research_slot_dispatch_never_spawns_twice(tmp_path,monkeypatch):
    cfg=cfg_for(tmp_path,tmp_path/'missing')
    calls=[]
    class Process:
        pid=123456
        def __init__(self,*args,**kwargs):calls.append(args)
        def wait(self,timeout=None):return 0
    monkeypatch.setattr(a.subprocess,'Popen',Process)
    first=a.dispatch(cfg,'research');second=a.dispatch(cfg,'research')
    assert len(calls)==1 and second['status']=='ALREADY_DISPATCHED'


def test_native_phases_receive_same_registry_paths(f1_setup,tmp_path,monkeypatch):
    register_f1(f1_setup);cfg=cfg_for(tmp_path,f1_setup[0].root);calls=[]
    def observe(registry,output,limits):
        calls.append((registry,output));Path(output).mkdir(parents=True);return {'status':'COMPLETED','errors':[]}
    def paper(registry,observation,output,limits):
        assert registry==calls[0][0] and observation==calls[0][1];return {'status':'COMPLETED','research':{},'paper':{}}
    monkeypatch.setattr(a.f2,'scan',observe);monkeypatch.setattr(a.f3,'scan',paper);monkeypatch.setattr(a,'reserved',lambda *x:False)
    assert a.execute_phase(cfg,'observe')['results'][0]['status']=='COMPLETED'
    assert a.execute_phase(cfg,'paper')['results'][0]['status']=='COMPLETED'


def test_late_revalidation_cannot_authorize_entry(scheduled_case):
    cfg,source,r,s,ledger,clock,market=scheduled_case
    clock[0]=s.planned_entry_ms-40000
    def late(*args):
        clock[0]=s.planned_entry_ms+1;return proof(clock[0]-1)
    market.refresh=late
    assert timing.step(cfg,source,r.registration_id,'refresh',market,scheduled_for_ms=clock[0])['status']=='PAUSED'
    assert timing.step(cfg,source,r.registration_id,'open',market,scheduled_for_ms=s.planned_entry_ms+1000)['status']=='PAUSED'
    assert not ledger.states(ledger.load())[r.registration_id].position


def test_pause_during_quote_prevents_fill(scheduled_case):
    cfg,source,r,s,ledger,clock,market=scheduled_case
    clock[0]=s.planned_entry_ms-40000
    timing.step(cfg,source,r.registration_id,'refresh',market,scheduled_for_ms=clock[0])
    clock[0]=s.planned_entry_ms+1000
    original=market.opening
    def opening(*args):a.set_pause(cfg,'timely',True);return original(*args)
    market.opening=opening
    assert timing.step(cfg,source,r.registration_id,'open',market,scheduled_for_ms=clock[0])['status']=='PAUSED'
    assert not ledger.states(ledger.load())[r.registration_id].position


def test_network_failure_keeps_scheduled_state(scheduled_case):
    cfg,source,r,s,ledger,clock,market=scheduled_case
    clock[0]=s.planned_entry_ms+1000
    def fail(*args):raise f2.okx.VendorUnavailableError('fixture offline')
    market.now=fail
    assert timing.step(cfg,source,r.registration_id,'open',market,scheduled_for_ms=clock[0])['status']=='PAUSED'
    assert ledger.states(ledger.load())[r.registration_id].status=='SCHEDULED'


def test_worker_completion_survives_controller_absence(tmp_path,monkeypatch):
    cfg=cfg_for(tmp_path,tmp_path/'missing');job='a'*32
    monkeypatch.setattr(a,'execute_phase',lambda *args:{'status':'COMPLETED','execution_enabled':False})
    value=a.worker_entry(cfg,'observe',job)
    assert r11.unseal(a.expand(cfg.state_root)/'jobs'/job/'worker_result.json')==value
    assert read(a.expand(cfg.state_root)/'jobs'/job/'events.json')[-1]['kind']=='WORKER_FINISHED'


def test_worker_exception_is_classified_without_raw_text(tmp_path,monkeypatch):
    cfg=cfg_for(tmp_path,tmp_path/'missing')
    def fail(*args):raise ValueError('sensitive body must not be persisted')
    monkeypatch.setattr(a,'execute_phase',fail)
    value=a.worker_entry(cfg,'observe','b'*32)
    assert value['status']=='WORKER_COMPONENT_ERROR' and 'sensitive' not in json.dumps(value)


def test_concurrent_research_dispatch_starts_one_process(tmp_path,monkeypatch):
    cfg=cfg_for(tmp_path,tmp_path/'missing');calls=[];results=[]
    class Process:
        pid=123456
        def __init__(self,*args,**kwargs):calls.append(True);time.sleep(.05)
        def wait(self,timeout=None):return 0
    monkeypatch.setattr(a.subprocess,'Popen',Process)
    threads=[threading.Thread(target=lambda:results.append(a.dispatch(cfg,'research'))) for _ in range(2)]
    for thread in threads:thread.start()
    for thread in threads:thread.join()
    assert len(calls)==1 and len(results)==2


def test_administratively_deferred_f1_is_not_counted_pending(f1_setup,tmp_path):
    r=register_f1(f1_setup);registry=f1_setup[0]
    registry.update_tracking_status(r.registration_id,'defer','DEFERRED','fixture administrative hold')
    value=a.status(cfg_for(tmp_path,registry.root))
    assert value['counts']['f2_admin_excluded']==1 and value['counts']['f2_pending']==0


def test_research_delegates_three_symbols_and_original_budgets(tmp_path,monkeypatch):
    cfg=cfg_for(tmp_path,tmp_path/'missing');calls=[]
    def batch(watch,root):
        calls.append((watch.enabled(),watch.limits.model_dump(),root))
        return {'status':'COMPLETED','tasks':[],'execution_enabled':False}
    monkeypatch.setattr(a.r11,'run_batch',batch)
    assert a.execute_phase(cfg,'research')['status']=='COMPLETED'
    assert calls[0][0]==['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP']
    assert calls[0][1]['llm_total']==72 and calls[0][1]['task_seconds']==900
    assert calls[0][2]==a.expand(cfg.batch_root)


def test_manual_cycle_observes_while_research_is_inflight(tmp_path,monkeypatch):
    cfg=cfg_for(tmp_path,tmp_path/'missing');calls=[]
    class Process:
        n=0
        def poll(self):self.n+=1;return 0 if self.n>2 else None
    handle={'process':Process(),'start':time.monotonic(),'timeout':10}
    def dispatch(cfg,phase,wait=True,**kwargs):
        calls.append(phase)
        return handle if phase=='research' else {'phase':phase,'status':'COMPLETED'}
    def finish(cfg,h):calls.append('research_finished');return {'phase':'research','status':'COMPLETED'}
    monkeypatch.setattr(a,'dispatch',dispatch);monkeypatch.setattr(a,'finish',finish);monkeypatch.setattr(a.time,'sleep',lambda x:None)
    assert a.cycle(cfg,True)['status']=='CYCLE_COMPLETE'
    assert calls.index('observe')<calls.index('research_finished')
    assert calls.index('timely')<calls.index('research_finished')


def test_configurable_research_cadence_is_exported(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'missing');cfg.schedule.research_seconds=21600
    path=tmp_path/'cfg.json';atomic(path,cfg.model_dump());export(cfg,path,tmp_path/'templates')
    assert '00,06,12,18:00:20 UTC' in (tmp_path/'templates/r12-research.timer').read_text(encoding='utf-8')
    assert 'PT21600S' in (tmp_path/'templates/r12-research.xml').read_text(encoding='utf-16')


def test_file_is_not_misreported_as_future_registry(tmp_path):
    path=tmp_path/'not-a-registry';path.write_text('not a directory',encoding='utf-8')
    value=a.status(cfg_for(tmp_path,path))
    assert value['index']['sources'][0]['status']=='INVALID_SOURCE_PATH'
    assert value['statistics_complete'] is False


@pytest.mark.parametrize('kind',['QUOTA_EXHAUSTED','AUTHENTICATION_OR_PERMISSION'])
def test_explicit_model_restriction_pauses_only_research(tmp_path,monkeypatch,kind):
    cfg=cfg_for(tmp_path,tmp_path/'missing')
    monkeypatch.setattr(a.r11,'run_batch',lambda *args:{'status':'COMPLETED','tasks':[{'registration':{'failure_kind':kind}}]})
    value=a.execute_phase(cfg,'research')
    assert value['service_restriction']['requires_manual_resume']
    assert a.paused(cfg,'research') and not a.paused(cfg,'observe') and not a.paused(cfg,'timely')
    assert a.dispatch(cfg,'research')['status']=='PAUSED'
