"""F2 isolated candle/lifecycle/registry contracts. No exchange account or LLM."""
import copy
import json
import multiprocessing
from pathlib import Path

import pytest

from tradingagents import forward_tracking as f2
from tradingagents.forward_validation import digest, atomic, read
from tradingagents.dataflows.plan_lifecycle import HOUR, timing
from tests.test_forward_validation import setup as f1_setup, register as f1_register
from tests.test_perpetual_plan import research

FIRST=(1792670700000//HOUR+1)*HOUR


def candidate(direction='LONG'):
    plan={'direction':direction,'entry':{'price':100},'stop':{'price':90 if direction=='LONG' else 110},
          'target':{'price':120 if direction=='LONG' else 80}}
    return f2.FrozenCandidate(registration_id='a'*64,research_id='fixture-study',registration_hash='b'*64,
        protocol_hash='c'*64,snapshot_id='d'*64,result_hash='e'*64,instrument='BTC-USDT-SWAP',
        direction=direction,plan=plan,clock=timing(FIRST-600000,{}),
        first_bar_open_ms=FIRST,first_check_ms=FIRST+HOUR,registered_ms=FIRST-300000,namespace='FIXTURE')


def row(ts,close=103,low=98,high=105,open_=100):
    return [str(ts),str(open_),str(high),str(low),str(close),'10','0.1','10','1']


class Client:
    def __init__(self,rows,clock):
        self.rows=rows;self.clock=clock;self.calls=0;self.paths=[]
    def get(self,path,params=None):
        self.calls+=1;self.paths.append(path)
        assert path in {'/api/v5/public/time','/api/v5/market/history-candles'}
        if path.endswith('/time'):return [{'ts':str(self.clock[0])}]
        return copy.deepcopy(self.rows)


@pytest.fixture
def tracking(tmp_path,monkeypatch):
    clock=[FIRST+HOUR+1000]
    monkeypatch.setattr(f2.okx,'utc_ms',lambda:clock[0])
    limits=f2.ScanLimits()
    c=candidate();store=f2.EventStore(tmp_path,'FIXTURE')
    client=Client([row(FIRST)],clock)
    return c,store,client,clock,limits


def run(tracking):
    c,store,client,clock,limits=tracking
    return f2.process_candidate(c,store,f2.Collector(client,limits),clock[0],limits)


def test_breakout_retest_resume_and_fixture_never_exports(tracking):
    c,store,client,clock,limits=tracking
    assert run(tracking)['status']=='PENDING_RETEST'
    clock[0]=FIRST+2*HOUR+1000
    client.rows=[row(FIRST+HOUR,104,99,105)]
    assert run(tracking)['status']=='ENTRY_ELIGIBLE'
    reopened=f2.EventStore(store.root.parent,'FIXTURE')
    events=reopened.load(c)
    assert len([e for e in events if e['kind']=='BAR_OBSERVATION'])==2
    transitions=[e for e in events if e['kind']=='STATE_TRANSITION']
    assert [e['payload']['signal']['state'] for e in transitions]==['PENDING_RETEST','ENTRY_ELIGIBLE']
    assert transitions[-1]['payload']['signal']['next_tradable_ms']==FIRST+2*HOUR
    assert not transitions[-1]['payload']['f3_exportable'] and reopened.triggers(c)==[]
    assert not transitions[-1]['execution_enabled']


def test_duplicate_scan_does_not_duplicate_bar_or_transition(tracking):
    run(tracking);run(tracking);run(tracking)
    events=tracking[1].load(tracking[0])
    assert sum(e['kind']=='BAR_OBSERVATION' for e in events)==1
    assert sum(e['kind']=='STATE_TRANSITION' for e in events)==1
    assert len({e['event_id'] for e in events})==len(events)


def test_no_future_bar_checks_before_first_close(tracking):
    tracking[3][0]=FIRST+1000
    result=run(tracking)
    assert result['status']=='PENDING_BREAKOUT' and tracking[2].calls==0
    events=tracking[1].load(tracking[0])
    assert events[0]['payload']['reason']=='WAITING_FOR_NEXT_CLOSED_BAR'


def test_only_breakout_no_retest(tracking):
    run(tracking);tracking[3][0]=FIRST+2*HOUR+1000
    tracking[2].rows=[row(FIRST+HOUR,104,101,105,103)]
    assert run(tracking)['status']=='PENDING_RETEST'


@pytest.mark.parametrize('direction,rows',[
    ('LONG',[row(FIRST),row(FIRST+HOUR,89,88,104)]),
    ('SHORT',[row(FIRST,97,96,102),row(FIRST+HOUR,96,95,101)]),
])
def test_long_invalidation_and_short_confirmation(tracking,direction,rows):
    tracking=list(tracking);tracking[0]=candidate(direction)
    tracking[3][0]=FIRST+2*HOUR+1000;tracking[2].rows=rows
    status=run(tracking)['status']
    assert status==('INVALIDATED' if direction=='LONG' else 'ENTRY_ELIGIBLE')


def test_invalidated_cannot_reactivate(tracking):
    tracking[2].rows=[row(FIRST,89,88,104)]
    assert run(tracking)['status']=='INVALIDATED'
    n=len(tracking[1].load(tracking[0]));tracking[3][0]+=HOUR
    tracking[2].rows=[row(FIRST+HOUR)]
    assert run(tracking)['new_events']==0
    assert len(tracking[1].load(tracking[0]))==n


def test_expiry_is_calendar_only_not_proof_untriggered(tracking):
    tracking[3][0]=tracking[0].clock.hypothesis_expires_ms
    assert run(tracking)['status']=='EXPIRED'
    e=tracking[1].load(tracking[0])[-1]
    assert e['payload']['coverage']=='UNKNOWN_UNOBSERVED' and tracking[2].calls==0
    assert run(tracking)['new_events']==0


def test_gap_pauses_without_partial_advance_then_backfill_recovery(tracking):
    tracking[3][0]=FIRST+3*HOUR+1000
    tracking[2].rows=[row(FIRST),row(FIRST+2*HOUR)]
    assert run(tracking)['processed_bars']==0
    events=tracking[1].load(tracking[0]);assert events[-1]['kind']=='PAUSED'
    tracking[2].rows=[row(FIRST),row(FIRST+HOUR),row(FIRST+2*HOUR)]
    assert run(tracking)['status']=='ENTRY_ELIGIBLE'
    events=tracking[1].load(tracking[0])
    assert any(e['payload'].get('collection_mode')=='BACKFILL' for e in events)
    assert tracking[1].triggers(tracking[0])==[]


def test_unconfirmed_or_missing_latest_pauses(tracking):
    tracking[2].rows[0][-1]='0'
    assert run(tracking)['processed_bars']==0
    assert tracking[1].load(tracking[0])[-1]['kind']=='PAUSED'


def test_rows_sort_identical_overlap_and_conflicting_duplicate():
    a,b=row(FIRST),row(FIRST+HOUR)
    bars,quality=f2.normalize_rows([b,a,a],FIRST,FIRST+2*HOUR)
    assert [r['ts'] for r in bars]==[FIRST,FIRST+HOUR] and quality['duplicate_rows']==1
    bad=copy.deepcopy(a);bad[4]='104'
    with pytest.raises(ValueError,match='duplicate'):f2.normalize_rows([a,b,bad],FIRST,FIRST+2*HOUR)


@pytest.mark.parametrize('value',['nan','inf','-1'])
def test_bad_ohlcv_pauses(tracking,value):
    tracking[2].rows[0][4]=value
    assert run(tracking)['processed_bars']==0


def test_later_unclosed_row_cannot_create_signal(tracking):
    tracking[2].rows=[row(FIRST),row(FIRST+HOUR,104,99,105)]
    assert run(tracking)['status']=='PENDING_RETEST'
    events=tracking[1].load(tracking[0])
    assert sum(e['kind']=='BAR_OBSERVATION' for e in events)==1


def test_local_clock_rollback_and_frozen_clock_mismatch(tracking):
    with pytest.raises(ValueError,match='clock'):f2.process_candidate(tracking[0],tracking[1],None,FIRST,tracking[4])
    value=tracking[0].model_dump();value['first_bar_open_ms']+=HOUR
    with pytest.raises(ValueError):f2.FrozenCandidate.model_validate(value)


def test_atomic_interruption_then_resume_no_duplicate(tracking,monkeypatch):
    original=f2.atomic
    monkeypatch.setattr(f2,'atomic',lambda *a,**k:(_ for _ in ()).throw(OSError('Injected interruption')))
    with pytest.raises(OSError):run(tracking)
    assert tracking[1].load(tracking[0])==[]
    monkeypatch.setattr(f2,'atomic',original)
    assert run(tracking)['processed_bars']==1
    assert sum(e['kind']=='STATE_TRANSITION' for e in tracking[1].load(tracking[0]))==1


def test_event_tamper_policy_conflict_and_no_terminal_reactivation(tracking):
    run(tracking)
    wrong=f2.EventStore(tracking[1].root.parent,'FIXTURE',delivery_grace_seconds=60)
    with pytest.raises(ValueError):wrong.load(tracking[0])
    p=tracking[1].path(tracking[0].registration_id);v=read(p)
    v['events'][-1]['payload']['signal']['state']='ENTRY_ELIGIBLE';atomic(p,v)
    with pytest.raises(ValueError,match='fingerprint'):tracking[1].load(tracking[0])


def test_c1_replay_rejects_forged_transition_even_if_rehashed(tracking):
    run(tracking);p=tracking[1].path(tracking[0].registration_id);v=read(p)
    e=v['events'][-1];e['payload']['signal']['state']='ENTRY_ELIGIBLE'
    e['evidence_fingerprint']=digest(e['payload']);e['event_hash']=digest({k:x for k,x in e.items() if k!='event_hash'})
    atomic(p,v)
    with pytest.raises(ValueError,match='C.1'):tracking[1].load(tracking[0])


def test_public_pagination_and_per_scan_shared_cache():
    clock=[FIRST+2*HOUR+1000]
    class Pages(Client):
        def get(self,path,params=None):
            self.calls+=1
            return [row(FIRST+HOUR)] if self.calls==1 else [row(FIRST)]
    client=Pages([],clock);collector=f2.Collector(client,f2.ScanLimits())
    value=collector.get('BTC-USDT-SWAP',FIRST,FIRST+2*HOUR)
    assert len(value['bars'])==2 and client.calls==2
    assert collector.get('BTC-USDT-SWAP',FIRST,FIRST+2*HOUR)==value and client.calls==2


def test_f1_false_risk_permission_allows_observation_without_risk_grant(f1_setup,tmp_path,monkeypatch):
    record=f1_register(f1_setup)
    assert record.plan_valid and record.simulation_pool_eligible and not record.allow_new_risk
    monkeypatch.setattr(f2.okx,'utc_ms',lambda:f1_setup[4][0])
    assert f2.exclusion_reasons(record)==[]
    candidate_=f2.candidate_from_record(record)
    assert candidate_.observation_only and not candidate_.original_allow_new_risk and not candidate_.simulation_admitted
    monkeypatch.setattr(f2.okx,'utc_ms',lambda:f1_setup[4][0])
    client=Client([],f1_setup[4])
    summary=f2.scan(f1_setup[0].root,tmp_path/'tracking',client=client)
    assert summary['eligible_candidates']==1 and summary['observation_eligible_count']==1
    assert summary['results'][0]['operational_status']=='WAITING_FOR_CLOSED_BAR'
    assert client.paths==['/api/v5/public/time']
    assert summary['simulated_trade_candidates']==0 and summary['allow_new_risk'] is False


@pytest.mark.parametrize('changes,reason',[
    ({'action':'WAIT'},'NOT_OPEN_CANDIDATE'),
    ({'workflow_status':'FAILED','llm_status':'LLM_UNAVAILABLE'},'RESEARCH_NOT_COMPLETED'),
    ({'origin':'HISTORICAL_IMPORT'},'NOT_PROSPECTIVE'),
    ({'origin':'TEST_REPLAY'},'NOT_PROSPECTIVE'),
    ({'plan_valid':False},'PLAN_INVALID'),
    ({'data_quality':'STALE_DATA'},'DATA_QUALITY_INVALID'),
])
def test_unsafe_states_never_enter_pool(f1_setup,changes,reason):
    r=f1_register(f1_setup).model_copy(update=changes)
    assert reason in f2.exclusion_reasons(r)


def test_f1_tamper_refuses_progress_but_does_not_hide_others(f1_setup,tmp_path,monkeypatch):
    r=f1_register(f1_setup);p=f1_setup[0].root/(r.registration_id+'.json')
    value=read(p);value['allow_new_risk']=True;atomic(p,value)
    monkeypatch.setattr(f2.okx,'utc_ms',lambda:f1_setup[4][0])
    result=f2.scan(f1_setup[0].root,tmp_path/'out',client=Client([],f1_setup[4]))
    assert result['errors'][0]['status']=='F1_ARCHIVE_INVALID' and result['results']==[]


def concurrent_worker(args):
    path,raw=args
    c=f2.FrozenCandidate.model_validate(raw)
    f2.okx.utc_ms=lambda:FIRST+HOUR+1000
    store=f2.EventStore(path,'FIXTURE');limits=f2.ScanLimits()
    return f2.process_candidate(c,store,f2.Collector(Client([row(FIRST)],[FIRST+HOUR+1000]),limits),FIRST+HOUR+1000,limits)


def test_multiprocess_same_candidate_only_one_bar(tmp_path):
    c=candidate();ctx=multiprocessing.get_context('spawn')
    with ctx.Pool(2) as pool:
        results=pool.map(concurrent_worker,[(str(tmp_path),c.model_dump())]*2)
    events=f2.EventStore(tmp_path,'FIXTURE').load(c)
    assert sum(e['kind']=='BAR_OBSERVATION' for e in events)==1
    assert sum(e['kind']=='STATE_TRANSITION' for e in events)==1
    assert all(r['status'] in {'BUSY','PENDING_RETEST'} for r in results)


def test_condition_contract_is_not_fill():
    e=f2.ConditionTrigger(event_id='x',registration_id='a'*64,research_id='r',protocol_hash='p',snapshot_id='s',
        result_hash='z',direction='LONG',signal_confirmed_ms=FIRST+2*HOUR,observed_available_ms=FIRST+2*HOUR+1000,decision_available_ms=FIRST+2*HOUR+1000,
        next_tradable_ms=FIRST+2*HOUR,hypothesis_expires_ms=FIRST+6*HOUR,original_conditions={},
        confirmation_bar={'ts':FIRST+HOUR},evidence_fingerprint='f',previous_state='PENDING_RETEST',collection_mode='CONTEMPORANEOUS')
    assert not e.is_simulated_fill and not e.allow_new_risk and not e.execution_enabled and e.requires_revalidation
    with pytest.raises(ValueError):f2.ConditionTrigger.model_validate({**e.model_dump(),'next_tradable_ms':FIRST})


def test_namespace_cannot_export_fixture_to_production(tracking):
    with pytest.raises(ValueError,match='Namespace'):
        f2.process_candidate(tracking[0],f2.EventStore(tracking[1].root.parent),None,tracking[3][0],tracking[4])


def test_fetch_crossing_expiry_does_not_reactivate_historical_condition(tracking):
    c,store,client,clock,limits=tracking
    clock[0]=c.clock.hypothesis_expires_ms-1000
    class Slow(Client):
        def get(self,path,params=None):
            answer=[row(FIRST+i*HOUR) for i in range(5)]
            clock[0]=c.clock.hypothesis_expires_ms+1000
            return answer
    result=f2.process_candidate(c,store,f2.Collector(Slow([],clock),limits),c.clock.hypothesis_expires_ms-1000,limits)
    assert result['status']=='EXPIRED'
    assert all(e['kind']!='STATE_TRANSITION' for e in store.load(c))


def test_page_budget_and_nonprogress_are_bounded():
    clock=[FIRST+2*HOUR+1000]
    client=Client([row(FIRST+HOUR)],clock)
    with pytest.raises(ValueError):
        f2.Collector(client,f2.ScanLimits(max_pages=1)).get('BTC-USDT-SWAP',FIRST,FIRST+2*HOUR)
    assert client.calls==1
    client=Client([row(FIRST+2*HOUR)],clock)
    with pytest.raises(ValueError,match='progress'):
        f2.Collector(client,f2.ScanLimits()).get('BTC-USDT-SWAP',FIRST,FIRST+2*HOUR)
    assert client.calls==1


def test_logical_http_budget_is_bounded(monkeypatch):
    calls=[]
    def get(self,path,params=None):calls.append(path);return []
    monkeypatch.setattr(f2.okx.OKXPublicClient,'get',get)
    client=f2.PublicBudgetClient(f2.ScanLimits(max_requests=1))
    client.get('/api/v5/public/time')
    with pytest.raises(f2.BudgetExhausted):client.get('/api/v5/public/time')
    assert len(calls)==1


def test_scan_supervisor_hard_timeout_is_audited(tmp_path,monkeypatch,capsys):
    import subprocess,sys
    monkeypatch.setattr(sys,'argv',['forward_tracking','scan','--registry',str(tmp_path/'f1'),'--output',str(tmp_path/'f2'),'--max-seconds','5'])
    def timeout(*a,**k):raise subprocess.TimeoutExpired('fake-worker',5)
    monkeypatch.setattr(f2.subprocess,'run',timeout)
    with pytest.raises(SystemExit) as exit_:f2.main()
    assert exit_.value.code==4 and 'TIME_BUDGET_EXHAUSTED' in capsys.readouterr().out
    assert len(list((tmp_path/'f2/runs').glob('*.json')))==1


def test_f3_reader_rechecks_original_permission(f1_setup,tmp_path):
    record=f1_register(f1_setup)
    assert f2.read_condition_events(f1_setup[0].root,tmp_path/'f2',record.registration_id)==[]


def test_changed_frozen_candidate_rejected_on_resume(tracking):
    run(tracking)
    value=tracking[0].model_dump();value['plan']['entry']['price']=101
    changed=f2.FrozenCandidate.model_validate(value)
    with pytest.raises(ValueError,match='identity'):tracking[1].load(changed)


def test_private_paths_still_blocked_by_reused_client():
    client=f2.PublicBudgetClient(f2.ScanLimits())
    for path in ['/api/v5/trade/order','/api/v5/account/balance','/api/v5/asset/transfer']:
        with pytest.raises(ValueError,match='blocked'):client.get(path)


def test_future_registered_candidate_wait_does_not_consume_candles(tracking):
    c,store,client,clock,limits=tracking
    clock[0]=FIRST-1000
    result=run(tracking)
    assert result['operational_status']=='WAITING_FOR_CLOSED_BAR' and client.calls==0


def test_pause_is_distinct_from_lifecycle_wait(tracking):
    tracking[2].rows=[]
    result=run(tracking)
    assert result['status']=='PENDING_BREAKOUT' and result['operational_status']=='PAUSED'
    assert result['pause_reason']=='DATA_GAP_OR_UNAVAILABLE'
