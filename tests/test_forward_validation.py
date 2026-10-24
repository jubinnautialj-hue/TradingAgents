"""F1 offline registration contracts; no network, orders or future tracking."""
import json
import pytest
from tests.test_perpetual_plan import research, CONFIG, choice
from tests.test_okx_research import NOW, SYMBOL
from tradingagents.dataflows import perpetual_plan
from tradingagents import forward_validation as f

@pytest.fixture
def setup(tmp_path,monkeypatch,research):
    state,bundle=research
    result,_=perpetual_plan.finalize_result(state,bundle,CONFIG)
    state.update(research_result=result.model_dump(mode='json'),data_quality='VALID',workflow_status='COMPLETED')
    directory=tmp_path/'report';directory.mkdir()
    (directory/'complete_report.md').write_text('Research',encoding='utf-8')
    (directory/'complete_report.html').write_text('<p>Research</p>',encoding='utf-8')
    def save():
        for name,value in [('research_result.json',state['research_result']),('multitimeframe_snapshot.json',bundle)]:
            (directory/name).write_text(json.dumps(value),encoding='utf-8')
    save()
    registry=f.Registry(tmp_path/'registry')
    clock=[NOW-1000];monkeypatch.setattr(f,'utc_ms',lambda:clock[0])
    token=registry.begin(SYMBOL,CONFIG)
    clock[0]=NOW+2000
    return registry,directory,state,token,clock,save

def register(setup,**kwargs):
    registry,directory,state,token,clock,save=setup
    return registry.register(directory,state,token=token,completed_ms=NOW+1000,**kwargs)

def test_valid_candidate_and_sealed_read(setup):
    r=register(setup);registry=setup[0]
    assert r.origin=='PROSPECTIVE' and r.plan_valid and r.simulation_pool_eligible
    assert not r.execution_enabled and not r.allow_new_risk and r.requires_revalidation
    assert r.first_check_ms>r.registered_ms
    assert registry.load(r.registration_id)==r
    assert list(registry.pending())==[r]

@pytest.mark.parametrize('action',['WAIT','REDUCE_LONG','REDUCE_SHORT','EXIT_CANDIDATE'])
def test_actions_preserved(setup,action):
    state=setup[2];state['portfolio_choice']=choice(action)
    state['position_context']='SHORT' if action=='REDUCE_SHORT' else 'LONG'
    result,_=perpetual_plan.finalize_result(state,state['market_snapshot'],CONFIG)
    state['research_result']=result.model_dump(mode='json');setup[5]()
    r=register(setup)
    assert r.action==action and not r.simulation_pool_eligible

@pytest.mark.parametrize('status',['STALE_DATA','DATA_UNAVAILABLE','RESEARCH_INVALID'])
def test_quality_blocks_pool(setup,status):
    setup[2]['data_quality']=status
    assert not register(setup).simulation_pool_eligible

def test_optional_stale_does_not_mutate_plan(setup):
    setup[2]['orderflow_evidence']={'status':'STALE_DATA'}
    r=register(setup)
    assert r.plan_valid and not r.simulation_pool_eligible
    assert r.quality_evidence['orderflow_evidence']['status']=='STALE_DATA'

def test_failure_not_direction(setup):
    setup[2].update(research_result=None,workflow_status='FAILED',llm_status='LLM_UNAVAILABLE')
    (setup[1]/'research_result.json').unlink()
    r=register(setup)
    assert r.action is None and not r.simulation_pool_eligible
    assert r.llm_status=='LLM_UNAVAILABLE'

def test_idempotent_and_conflicting_identity(setup):
    first=register(setup);setup[4][0]+=100000
    assert register(setup)==first
    setup[2]['research_result']['wait_case']='Changed conclusion'
    with pytest.raises(ValueError,match='sealed'):register(setup)

def test_interrupt_recover(setup,monkeypatch):
    original=f.atomic
    def interrupt(path,value):
        if len(path.stem)==64:raise OSError('Simulated interrupted final commit')
        original(path,value)
    monkeypatch.setattr(f,'atomic',interrupt)
    with pytest.raises(OSError):register(setup)
    monkeypatch.setattr(f,'atomic',original)
    assert register(setup).origin=='PROSPECTIVE'

def test_late_and_historical_isolation(setup):
    setup[4][0]+=61000
    assert register(setup).origin=='LATE_REGISTRATION'

def test_import_cannot_claim_prospective(setup):
    registry,directory,state,*_=setup
    with pytest.raises(ValueError,match='intent'):
        registry.register(directory,state,origin='PROSPECTIVE')
    r=registry.register(directory,state)
    assert r.origin=='HISTORICAL_IMPORT' and not r.simulation_pool_eligible

def test_future_availability_block(setup):
    setup[2]['market_snapshot']['fetched_at_ms']=NOW+100000
    with pytest.raises(ValueError):register(setup)

def test_original_moved_and_archive_tamper(setup):
    r=register(setup);registry,directory=setup[:2]
    (directory/'complete_report.md').unlink()
    assert registry.load(r.registration_id)==r
    assert not registry.verify_original(r.registration_id)['complete_report.md']
    p=registry.root/(r.registration_id+'.artifacts.json')
    a=f.read(p);a['complete_report.md']='tampered';f.atomic(p,a)
    with pytest.raises(ValueError,match='artifacts'):registry.load(r.registration_id)

def test_events_and_schema(setup):
    r=register(setup);registry=setup[0]
    a=registry.annotate(r.registration_id,'one','Audit only')
    assert registry.annotate(r.registration_id,'one','Audit only')==a
    with pytest.raises(ValueError):registry.annotate(r.registration_id,'one','Changed')
    assert registry.events(r.registration_id)==[a]
    value=r.model_dump();value['first_check_ms']=r.cutoff_ms
    with pytest.raises(ValueError):f.Registration.model_validate(value)
    value=r.model_dump();value['execution_enabled']=True
    with pytest.raises(ValueError):f.Registration.model_validate(value)

def test_pairing_does_not_depend_on_conclusion(setup):
    a=register(setup)
    setup[2]['research_result']['research_id']='different-configuration'
    setup[5]()
    registry,directory,state=setup[:3]
    b=registry.register(directory,state)
    assert a.registration_id!=b.registration_id and a.pair_id==b.pair_id


def test_invalid_plan_and_failure_categories(setup):
    setup[2]['research_result'].update(plan_valid=False,plan_status='PLAN_INCOMPLETE')
    setup[5]()
    r=register(setup)
    assert not r.simulation_pool_eligible and f.sample_category(r)=='PLAN_INVALID'

def test_missed_first_confirmation_bar_cannot_be_backfilled(setup):
    setup[4][0]=NOW+3600000
    r=register(setup)
    assert not r.simulation_pool_eligible

def test_protocol_cannot_silently_change_same_version(setup,monkeypatch,tmp_path):
    changed=f.read(f.PROTOCOL)
    changed['registration_max_delay_ms']=50000
    alternate=tmp_path/'changed_protocol.json';f.atomic(alternate,changed)
    monkeypatch.setattr(f,'PROTOCOL',alternate)
    with pytest.raises(ValueError,match='frozen'):
        f.Registry(setup[0].root)

def test_protocol_costs_preserve_d_freeze():
    changed=f.read(f.PROTOCOL);changed['costs']['slippage_bps']=0
    with pytest.raises(ValueError,match='frozen D'):
        f.ForwardProtocol.model_validate(changed)

def test_registration_content_and_last_event_tamper(setup):
    r=register(setup);registry=setup[0]
    registry.annotate(r.registration_id,'one','Audit')
    path=registry.root/(r.registration_id+'.events.json')
    e=f.read(path);e[0]['note']='Changed';f.atomic(path,e)
    with pytest.raises(ValueError,match='chain'):registry.events(r.registration_id)
    path=registry.root/(r.registration_id+'.json')
    v=f.read(path);v['action']='OPEN_SHORT_CANDIDATE';f.atomic(path,v)
    with pytest.raises(ValueError,match='fingerprint'):registry.load(r.registration_id)

def test_closed_bar_contract_no_pre_registration_observation(setup):
    r=register(setup)
    observation=f.FutureBarObservation(registration_id=r.registration_id,bar_open_ms=r.first_bar_open_ms,
        bar_close_ms=r.first_check_ms,available_ms=r.first_check_ms+1,
        recorded_ms=r.first_check_ms+2,bar_fingerprint='a'*64)
    key=observation.validate_against(r)
    assert len(key)==64 and observation.validate_against(r)==key
    bad=observation.model_copy(update={'bar_open_ms':r.cutoff_ms-3600000})
    with pytest.raises(ValueError,match='leak'):bad.validate_against(r)
    with pytest.raises(ValueError):
        f.FutureBarObservation.model_validate({**observation.model_dump(),'available_ms':r.cutoff_ms})
    assert setup[0].frozen_conditions(r.registration_id)['costs']['taker_fee_bps']==5

def test_recovery_does_not_require_original_report(setup,monkeypatch):
    original=f.atomic
    def interrupt(path,value):
        if len(path.stem)==64:raise OSError('Interrupted')
        original(path,value)
    monkeypatch.setattr(f,'atomic',interrupt)
    with pytest.raises(OSError):register(setup)
    for p in setup[1].iterdir():p.unlink()
    monkeypatch.setattr(f,'atomic',original)
    assert register(setup).research_id=='test-run'

def test_report_tamper_before_registration_blocks(setup):
    path=setup[1]/'research_result.json'
    value=json.loads(path.read_text(encoding='utf-8'));value['wait_case']='tampered'
    f.atomic(path,value)
    with pytest.raises(ValueError,match='Report/state'):register(setup)

def test_future_bar_even_with_updated_hash_blocks(setup):
    from tradingagents.dataflows.vendors.okx import fingerprint
    b=setup[2]['market_snapshot'];child=b['timeframes']['1h']
    child['records'][-1]['ts']=NOW
    child['snapshot_id']=fingerprint(child);b['snapshot_id']=fingerprint(b)
    with pytest.raises(ValueError):register(setup)


def test_native_mock_workflow_registers_wait_and_default_off(research,monkeypatch,tmp_path):
    from typer.testing import CliRunner
    from cli import main
    from tests.test_perpetual_plan import PlanClient, PlanModel, _Client
    from tests.test_graph_end_to_end import ARGS
    from tests.test_okx_research import DAY
    from tradingagents.graph import trading_graph
    from tradingagents.dataflows.vendors import okx
    from tradingagents.memory import okx_simulation
    import copy
    monkeypatch.setattr(okx,'OKXPublicClient',lambda:PlanClient())
    monkeypatch.setattr(trading_graph,'create_tier_client',lambda *a,**k:_Client(PlanModel(structured=True)))
    for key in ['curr_date','end_date','start_date']:monkeypatch.setitem(ARGS,key,DAY)
    cfg={**copy.deepcopy(main.DEFAULT_CONFIG),**CONFIG,'results_dir':str(tmp_path),'memory_log_path':str(tmp_path/'memory.md')}
    monkeypatch.setattr(main,'DEFAULT_CONFIG',cfg)
    monkeypatch.setattr(f,'utc_ms',lambda:NOW)
    monkeypatch.setattr(okx_simulation,'utc_ms',lambda:NOW)
    command=['okx-research','--trade-plan','--date',DAY,'--taker-fee-bps','1']
    runner=CliRunner()
    old=runner.invoke(main.app,command)
    assert old.exit_code==0,old.output
    assert not (tmp_path/'forward_validation').exists()
    result=runner.invoke(main.app,command+['--forward-register'])
    assert result.exit_code==0,result.output
    assert 'workflow: COMPLETED' in result.output and 'F1 registration:' in result.output
    records=list(f.Registry(tmp_path/'forward_validation/f1').registrations())
    assert len(records)==1 and records[0].action=='WAIT'
    assert records[0].origin=='PROSPECTIVE' and not records[0].simulation_pool_eligible
    def interrupted(*args,**kwargs):raise OSError('Simulated disk failure')
    monkeypatch.setattr(f.Registry,'register',interrupted)
    interrupted_run=runner.invoke(main.app,command+['--forward-register'])
    assert interrupted_run.exit_code==4
    assert 'Report preserved:' in interrupted_run.output and 'intent token=' in interrupted_run.output


def test_audited_status_update_cannot_enable_invalid_research(setup):
    r=register(setup);registry=setup[0]
    assert registry.tracking_status(r.registration_id)=='QUEUED'
    registry.update_tracking_status(r.registration_id,'defer','DEFERRED','Manual deferred tracking')
    assert list(registry.pending())==[] and registry.load(r.registration_id)==r
    registry.update_tracking_status(r.registration_id,'resume','QUEUED','F2 not started')
    assert len(list(registry.pending()))==1
    registry.update_tracking_status(r.registration_id,'cancel','CANCELLED','No tracking')
    assert len(registry.events(r.registration_id))==3
    setup[2]['research_result']['research_id']='invalid-new-id'
    setup[2]['research_result'].update(plan_valid=False,plan_status='PLAN_INCOMPLETE')
    setup[5]()
    bad=registry.register(setup[1],setup[2])
    with pytest.raises(ValueError,match='cannot be enabled'):
        registry.update_tracking_status(bad.registration_id,'try','QUEUED','Must fail')


def test_no_market_cutoff_failure_is_archived_without_fabricated_time(setup):
    setup[2].update(research_result=None,market_snapshot={'symbol':SYMBOL,'status':'DATA_UNAVAILABLE'},
                    data_quality='DATA_UNAVAILABLE',workflow_status='BLOCKED_DATA')
    (setup[1]/'research_result.json').unlink();(setup[1]/'multitimeframe_snapshot.json').unlink()
    r=register(setup)
    assert r.cutoff_ms is None and r.data_available_ms is None and r.first_check_ms is None
    assert r.pair_id is None and not r.simulation_pool_eligible
    assert f.sample_category(r)=='DATA_QUALITY_INSUFFICIENT'

def test_data_only_cannot_create_formal_research_sample():
    from typer.testing import CliRunner
    from cli.main import app
    result=CliRunner().invoke(app,['okx-research','--data-only','--forward-register'])
    assert result.exit_code==1 and 'not research samples' in result.output


def test_single_timeframe_without_plan_still_seals_input(setup):
    from tests.test_okx_research import CONFIG as single_config,DAY,FakeClient
    from tradingagents.dataflows.vendors import okx
    state=setup[2]
    snapshot=okx.build_bundle(SYMBOL,DAY,single_config,FakeClient())
    state.update(market_snapshot=snapshot,research_result=None,allow_new_risk=True)
    (setup[1]/'research_result.json').unlink();(setup[1]/'multitimeframe_snapshot.json').unlink()
    r=register(setup)
    assert r.quality_evidence['market_snapshot']==snapshot
    assert r.snapshot_id==snapshot['snapshot_id'] and r.allow_new_risk and not r.simulation_pool_eligible
