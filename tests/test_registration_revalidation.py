"""Frozen FIXTURE admission tests; no network/model or formal archives."""
import copy
import pytest
from tests.test_forward_validation import setup as f1_setup
from tests.test_perpetual_plan import research,CONFIG,PlanClient,NOW,plan,okx,d
from tradingagents import forward_validation as f, observation_eligibility as obs, registration_revalidation as admission

@pytest.mark.parametrize('kind',['fresh','expired_no_refresh','expired_good_refresh','expired_bad_refresh','original_invalid'])
def test_matrix(f1_setup,monkeypatch,kind):
    registry,directory,state,token,clock,save=f1_setup
    config={**CONFIG,'forward_admission_policy':admission.VERSION}
    monkeypatch.setattr(f,'utc_ms',lambda:NOW-1000)
    token=registry.begin(state['company_of_interest'],config)
    monkeypatch.setattr(f,'utc_ms',lambda:clock[0])
    bundle=state['market_snapshot'];original=copy.deepcopy(bundle)
    later=NOW+1000 if kind=='fresh' else NOW+301000
    monkeypatch.setattr(okx,'utc_ms',lambda:later);monkeypatch.setattr(d,'utc_ms',lambda:later)
    if kind=='original_invalid':
        bundle['derivatives']['components']['orderbook']['status']='STALE_DATA'
        bundle['snapshot_id']=okx.fingerprint(bundle);original=copy.deepcopy(bundle)
        monkeypatch.setattr(okx,'OKXPublicClient',lambda:pytest.fail('Invalid original cannot be repaired'))
    else:monkeypatch.setattr(okx,'OKXPublicClient',lambda:PlanClient(now=later,price=1100 if kind=='expired_bad_refresh' else 1000))
    result,proof=plan.finalize_result(state,bundle,config,refresh=kind in ['fresh','expired_good_refresh','expired_bad_refresh','original_invalid'])
    assert bundle==original
    assert result.plan_valid==(kind in ['fresh','expired_good_refresh'])
    state.update(research_result=result.model_dump(mode='json'),entry_revalidation=proof,data_quality='VALID',workflow_status='COMPLETED');save()
    clock[0]=later+1000
    record=registry.register(directory,state,token=token,completed_ms=later)
    assessment=obs.assess(registry.load(record.registration_id),clock[0])
    expected=kind in ['fresh','expired_good_refresh']
    assert record.simulation_pool_eligible==expected, (record.status,{n:p['status'] for n,p in bundle['derivatives']['components'].items()})
    assert assessment.observation_eligible==expected,assessment.reasons
    assert bundle==original

@pytest.mark.parametrize('mutation',['snapshot','instrument','checked_time','fingerprint','price','missing','expired','wait'])
def test_proof_negative(f1_setup,monkeypatch,mutation):
    registry,directory,state,token,clock,save=f1_setup
    config={**CONFIG,'forward_admission_policy':admission.VERSION}
    later=NOW+301000
    monkeypatch.setattr(okx,'utc_ms',lambda:later);monkeypatch.setattr(d,'utc_ms',lambda:later)
    monkeypatch.setattr(okx,'OKXPublicClient',lambda:PlanClient(now=later))
    result,proof=plan.finalize_result(state,state['market_snapshot'],config,refresh=True)
    raw=result.model_dump(mode='json');bundle=state['market_snapshot']
    assert admission.build(bundle,raw,config,later,later+1000)
    if mutation=='snapshot':raw['snapshot_id']='0'*64
    if mutation=='instrument':raw['evidence']['entry_revalidation']['instrument']='ETH-USDT-SWAP'
    if mutation=='checked_time':raw['evidence']['entry_revalidation']['checked_at_ms']=later+10000
    if mutation=='fingerprint':raw['evidence']['entry_revalidation']['validation_id']='0'*64
    if mutation=='price':raw['entry_reference']['price']+=1
    if mutation=='missing':raw['evidence']['entry_revalidation']={}
    if mutation=='expired':later+=61000
    if mutation=='wait':raw['proposed_action']='WAIT'
    assert admission.build(bundle,raw,config,later,later+1000) is None


def test_default_and_f3_independent(f1_setup,monkeypatch):
    from tradingagents import forward_paper as paper
    state=f1_setup[2];later=NOW+301000
    monkeypatch.setattr(okx,'utc_ms',lambda:later);monkeypatch.setattr(d,'utc_ms',lambda:later)
    monkeypatch.setattr(okx,'OKXPublicClient',lambda:PlanClient(now=later))
    r,v=plan.finalize_result(state,state['market_snapshot'],CONFIG,refresh=True)
    assert admission.build(state['market_snapshot'],r.model_dump(mode='json'),CONFIG,later,later+1000) is None
    assert not paper.revalidation_ready(v,{'hypothesis_expires_ms':later+3600000},later)


def test_oi_diagnostic_safe_and_behavior(research):
    from tradingagents.dataflows.vendors import okx_derivatives as vendor
    import json
    bundle=research[1]
    class Bad(PlanClient):
        def get(self,path,params=None):
            if path==vendor.OI_HISTORY:raise ValueError('secret-token=DO_NOT_PERSIST')
            return super().get(path,params)
    evidence=vendor.collect_derivatives(Bad(),bundle['instrument'],bundle['cutoff_ms'],'4h',False,CONFIG)
    part=evidence['components']['oi_history']
    assert part['status']=='DATA_UNAVAILABLE' and part['diagnostic']['error_code']=='UNCLASSIFIED'
    assert 'DO_NOT_PERSIST' not in json.dumps(part) and 'secret-token' not in json.dumps(part)
    assert part['diagnostic']['stage']=='REQUEST'

@pytest.mark.parametrize('kind',['width','pagination','numeric'])
def test_oi_static_codes(research,kind):
    vendor=d;bundle=research[1];step=4*3600000;base=(NOW//step)*step-step
    class Rows(PlanClient):
        def get(self,path,params=None):
            if path==vendor.OI_HISTORY:
                if kind=='pagination':return [[int(params['end']),1,1,1]]
                if kind=='width':return [[base,1],[base-step,1]]
                return [[base,'PRIVATE_DO_NOT_LOG',1,1],[base-step,1,1,1]]
            return super().get(path,params)
    value=vendor.collect_derivatives(Rows(),bundle['instrument'],bundle['cutoff_ms'],'4h',False,{**CONFIG,'okx_derivatives_history_points':2})['components']['oi_history']
    assert value['status']=='DATA_UNAVAILABLE'
    diag=value['diagnostic']
    assert diag['error_code']=={'width':'ROW_WIDTH','pagination':'PAGINATION_NONPROGRESS','numeric':'UNCLASSIFIED'}[kind]
    assert 'PRIVATE_DO_NOT_LOG' not in __import__('json').dumps(value)


def test_existing_registration_is_never_upgraded(f1_setup):
    registry,directory,state,token,clock,save=f1_setup
    first=registry.register(directory,state,token=token,completed_ms=NOW+1000)
    before=(registry.root/(first.registration_id+'.json')).read_bytes()
    token2=registry.begin(state['company_of_interest'],{**CONFIG,'forward_admission_policy':admission.VERSION})
    assert registry.register(directory,state,token=token2,completed_ms=NOW+1000)==first
    assert (registry.root/(first.registration_id+'.json')).read_bytes()==before
    assert admission.KEY not in first.quality_evidence


def test_schema_and_registration_proof_tamper(f1_setup,monkeypatch):
    import json,re
    registry,directory,state,token,clock,save=f1_setup
    config={**CONFIG,'forward_admission_policy':admission.VERSION};clock[0]=NOW-1000
    token=registry.begin(state['company_of_interest'],config);later=NOW+301000
    monkeypatch.setattr(okx,'utc_ms',lambda:later);monkeypatch.setattr(d,'utc_ms',lambda:later)
    monkeypatch.setattr(okx,'OKXPublicClient',lambda:PlanClient(now=later))
    result,proof=plan.finalize_result(state,state['market_snapshot'],config,refresh=True)
    state.update(research_result=result.model_dump(mode='json'),entry_revalidation=proof);save();clock[0]=later+1000
    record=registry.register(directory,state,token=token,completed_ms=later)
    assert record.status=='AWAITING_F2'
    value=record.quality_evidence[admission.KEY]
    schema=json.loads((f.ROOT/'docs/registration_revalidation.schema.json').read_text())
    assert set(value)==set(schema['required'])==set(schema['properties'])
    for name,rule in schema['properties'].items():
        if 'const' in rule:assert value[name]==rule['const']
        if 'pattern' in rule:assert re.fullmatch(rule['pattern'],value[name])
        if rule.get('type')=='integer':assert type(value[name]) is int and value[name]>=1
    # Expiration is checked at the frozen registration, not renewed when read.
    assert obs.assess(record,record.expires_ms).observation_eligible is False
    broken=copy.deepcopy(record.quality_evidence);broken[admission.KEY]['candidate_id']='SHORT_BREAKOUT_RETEST'
    assert not obs.assess(record.model_copy(update={'quality_evidence':broken}),clock[0]).observation_eligible
    assert registry.load(record.registration_id)==record


def test_aging_exception_does_not_mask_other_sources(research):
    bundle=research[1];registered=NOW+301000
    effective=d.evidence_view(bundle['derivatives'],registered)
    assert admission.evidence_valid({'derivatives_at_registration':effective},bundle,registered)
    assert not admission.evidence_valid({'orderflow_evidence':effective},bundle,registered)
    assert not admission.evidence_valid({'orderflow_evidence':{admission.KEY:{'status':'STALE_DATA'}}},bundle,registered)
