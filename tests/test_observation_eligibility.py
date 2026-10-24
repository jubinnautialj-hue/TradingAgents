"""F2.1 permission separation in isolated fixture registries; no real samples."""
import copy
from pathlib import Path
import pytest

from tests.test_forward_validation import setup as f1_setup, register as f1_register
from tests.test_perpetual_plan import research
from tradingagents import forward_tracking as tracking
from tradingagents import observation_eligibility as observation
from tradingagents.forward_validation import atomic,digest,read
from tradingagents.dataflows.plan_lifecycle import HOUR


class CandleClient:
    def __init__(self,clock,rows):self.clock=clock;self.rows=rows;self.calls=0
    def get(self,path,params=None):
        self.calls+=1
        assert path in {'/api/v5/public/time','/api/v5/market/history-candles'}
        if path.endswith('/time'):return [{'ts':str(self.clock[0])}]
        return copy.deepcopy(self.rows)


def bar(ts,entry,gap,retest=False):
    close=entry+gap*(0.2 if retest else 0.25)
    return [str(ts),str(entry),str(close+gap*.1),str(entry-gap*.05),str(close),'10','0.1','100','1']


def test_true_plan_false_risk_passes_independent_policy(f1_setup):
    r=f1_register(f1_setup)
    before=read(f1_setup[0].root/(r.registration_id+'.json'))
    a=observation.assess(r,f1_setup[4][0])
    assert a.research_completed and a.plan_valid and a.observation_eligible
    assert not a.allow_new_risk and a.original_result_allow_new_risk is False
    assert a.observation_only and not a.simulation_admitted and not a.execution_enabled
    assert before==read(f1_setup[0].root/(r.registration_id+'.json'))


@pytest.mark.parametrize('changes,reason',[
    ({'action':'WAIT'},'NOT_OPEN_CANDIDATE'),
    ({'workflow_status':'FAILED','llm_status':'LLM_UNAVAILABLE'},'RESEARCH_NOT_COMPLETED'),
    ({'plan_valid':False},'PLAN_INVALID'),
    ({'origin':'HISTORICAL_IMPORT'},'NOT_PROSPECTIVE'),
    ({'origin':'TEST_REPLAY'},'NOT_PROSPECTIVE'),
    ({'data_quality':'STALE_DATA'},'DATA_QUALITY_INVALID'),
    ({'simulation_pool_eligible':False},'F1_POOL_NOT_ELIGIBLE'),
])
def test_separation_is_not_an_unconditional_bypass(f1_setup,changes,reason):
    r=f1_register(f1_setup).model_copy(update=changes)
    a=observation.assess(r,f1_setup[4][0])
    assert not a.observation_eligible and reason in a.reasons


def test_invalid_geometry_cost_or_source_cannot_observe(f1_setup):
    r=f1_register(f1_setup)
    for mutate in ['stop','cost','source','clock']:
        raw=copy.deepcopy(r.original_result)
        if mutate=='stop':raw['stop_loss']['price']=raw['entry_reference']['price']+1
        elif mutate=='cost':raw['risk_reward']['net']+=1
        elif mutate=='source':raw['entry_reference']['source_snapshot_id']='wrong'
        else:raw['signal_timing']['hypothesis_expires_ms']+=HOUR
        a=observation.assess(r.model_copy(update={'original_result':raw}),f1_setup[4][0])
        assert not a.observation_eligible


def test_stale_at_registration_differs_from_old_but_valid_research(f1_setup):
    r=f1_register(f1_setup)
    later=r.first_check_ms+1000
    assert observation.assess(r,later).observation_eligible
    stale=copy.deepcopy(r.quality_evidence)
    stale['orderflow_snapshot.json']={'effective_view':{'status':'STALE_DATA'}}
    a=observation.assess(r.model_copy(update={'quality_evidence':stale}),later)
    assert not a.observation_eligible and 'FROZEN_EVIDENCE_INVALID_OR_STALE' in a.reasons
    assert r.original_result['allow_new_risk'] is False


def test_registration_clock_and_frozen_gap_not_waived(f1_setup):
    r=f1_register(f1_setup)
    assert not observation.assess(r,r.registered_ms-1).observation_eligible
    core=copy.deepcopy(r.quality_evidence)
    raw=core['market_snapshot'];raw['timeframes']['1h']['records'].pop(3)
    a=observation.assess(r.model_copy(update={'quality_evidence':core}),r.registered_ms+1)
    assert not a.observation_eligible and 'INVALID_FROZEN_MARKET_EVIDENCE' in a.reasons


def test_audit_idempotency_and_risk_fields_archived_separately(f1_setup,tmp_path):
    r=f1_register(f1_setup)
    store=observation.EligibilityAudit(tmp_path)
    a=observation.assess(r,r.registered_ms+1)
    e=store.record(a)
    assert store.record(observation.assess(r,r.registered_ms+100))==e
    assert e['assessment']['observation_eligible'] is True
    assert e['assessment']['allow_new_risk'] is False
    assert len(store.events(r.registration_id))==1
    assert store.policy_path.exists()
    changed=read(store.path(r.registration_id));changed[0]['assessment']['allow_new_risk']=True
    atomic(store.path(r.registration_id),changed)
    with pytest.raises(ValueError):store.events(r.registration_id)


def test_expired_new_enrollment_not_allowed(f1_setup,tmp_path,monkeypatch):
    r=f1_register(f1_setup)
    a=observation.assess(r,r.expires_ms)
    assert a.research_observation_qualified and not a.observation_eligible
    assert a.window_status=='EXPIRED'
    clock=[r.expires_ms];monkeypatch.setattr(tracking.okx,'utc_ms',lambda:clock[0])
    summary=tracking.scan(f1_setup[0].root,tmp_path/'out',client=CandleClient(clock,[]))
    assert summary['results']==[] and summary['observation_eligible_count']==0


def test_mock_canonical_f1_to_f2_resume_and_condition_only(f1_setup,tmp_path,monkeypatch):
    """Canonical F1 output shape, entirely mocked/test-local; not a real sample."""
    r=f1_register(f1_setup);output=tmp_path/'test_only_f2'
    clock=f1_setup[4];monkeypatch.setattr(tracking.okx,'utc_ms',lambda:clock[0])
    before=read(f1_setup[0].root/(r.registration_id+'.json'))
    initial=tracking.scan(f1_setup[0].root,output,client=CandleClient(clock,[]))
    assert initial['observation_eligible_count']==1
    assert initial['results'][0]['operational_status']=='WAITING_FOR_CLOSED_BAR'
    entry=r.original_result['entry_reference']['price'];gap=entry-r.original_result['stop_loss']['price']
    clock[0]=r.first_check_ms+1000
    first=tracking.scan(f1_setup[0].root,output,client=CandleClient(clock,[bar(r.first_bar_open_ms,entry,gap)]))
    assert first['results'][0]['status']=='PENDING_RETEST'
    clock[0]+=HOUR
    second=tracking.scan(f1_setup[0].root,output,client=CandleClient(clock,[bar(r.first_bar_open_ms+HOUR,entry,gap,True)]))
    assert second['results'][0]['status']=='ENTRY_ELIGIBLE'
    conditions=tracking.read_condition_events(f1_setup[0].root,output,r.registration_id)
    assert len(conditions)==1
    condition=conditions[0]
    assert condition.version=='forward-observation-condition-v1'
    assert condition.observation_only and condition.requires_separate_simulation_admission
    assert not condition.simulation_admitted and not condition.allow_new_risk and not condition.is_simulated_fill
    repeat=tracking.scan(f1_setup[0].root,output,client=CandleClient(clock,[]))
    assert repeat['results'][0]['status']=='ENTRY_ELIGIBLE'
    assert len(tracking.read_condition_events(f1_setup[0].root,output,r.registration_id))==1
    assert before==read(f1_setup[0].root/(r.registration_id+'.json'))
    # No model service is called after admission; original research remains immutable.
    clock[0]=r.expires_ms
    expiry=tracking.scan(f1_setup[0].root,output,client=CandleClient(clock,[]))
    assert expiry['results'][0]['status']=='EXPIRED'
    assert tracking.read_condition_events(f1_setup[0].root,output,r.registration_id)==[]


def test_legacy_chain_is_paused_and_not_rewritten(f1_setup,tmp_path,monkeypatch):
    r=f1_register(f1_setup);output=tmp_path/'out'
    monkeypatch.setattr(tracking.okx,'utc_ms',lambda:f1_setup[4][0])
    path=tracking.EventStore(output).path(r.registration_id)
    legacy={'version':'forward-f2-v1','namespace':'PRODUCTION','policy':{'version':'forward-f2-v1'},'events':[]}
    atomic(path,legacy);before=path.read_bytes()
    summary=tracking.scan(f1_setup[0].root,output,client=CandleClient(f1_setup[4],[]))
    assert summary['errors'][0]['status']=='LEGACY_TRACKING_CHAIN_PAUSED'
    assert path.read_bytes()==before
    assert len(observation.EligibilityAudit(output).events(r.registration_id))==1


def test_assessment_schema_cannot_grant_execution_or_simulation(f1_setup):
    r=f1_register(f1_setup);a=observation.assess(r,r.registered_ms+1)
    for field in ['execution_enabled','simulation_admitted']:
        with pytest.raises(ValueError):observation.ObservationAssessment.model_validate({**a.model_dump(),field:True})
    with pytest.raises(ValueError):
        observation.ObservationAssessment.model_validate({**a.model_dump(),'plan_valid':False})


def test_missing_market_cutoff_failure_remains_ineligible_without_exception(f1_setup):
    r=f1_register(f1_setup);raw=copy.deepcopy(r.original_result)
    raw.update(research_cutoff_utc=None,signal_timing=None,plan_valid=False,proposed_action='WAIT',
               plan_status='RESEARCH_INVALID',data_quality='DATA_UNAVAILABLE')
    broken=r.model_copy(update={'original_result':raw,'cutoff_ms':None,'first_bar_open_ms':None,
                                'first_check_ms':None,'expires_ms':None,'plan_valid':False,'data_quality':'DATA_UNAVAILABLE'})
    a=observation.assess(broken,r.registered_ms+1)
    assert not a.observation_eligible and 'DATA_QUALITY_INVALID' in a.reasons


def test_supplied_positive_flag_cannot_override_frozen_invalid_plan(f1_setup,monkeypatch):
    r=f1_register(f1_setup);now=r.registered_ms+1
    monkeypatch.setattr(tracking.okx,'utc_ms',lambda:now)
    positive=observation.assess(r,now)
    invalid=r.model_copy(update={'plan_valid':False})
    with pytest.raises(ValueError,match='recomputed'):
        tracking.candidate_from_record(invalid,positive)
