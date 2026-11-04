"""TEST_FIXTURE only: native accounting and actual SDK MockTransport, no network."""
import copy
import hashlib
import json
import multiprocessing as mp
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tradingagents import market_online_probe as p, guarded_research as g
from tradingagents.forward_validation import digest, read
from tradingagents.safety_budget import Ledger, Policy, SafetyStop, implementation_hash
from tests.test_guarded_research import FixtureModel
from tests.test_market_safe_adapter import events, model_for
from tests.test_market_tool_contract import pinned


def policy(**overrides):
    return Policy(run_purpose=p.PURPOSE, research_tokens=100000, daily_tokens=100000,
                  optional_source_policy='official-macro-403-v1',
                  campaign_tokens=100000, headroom_tokens=100000, research_calls=1,
                  daily_calls=1, campaign_calls=1, research_http=240, daily_http=240,
                  campaign_http=240, **overrides)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', lambda *a, **k: pytest.fail('REAL_NETWORK_FORBIDDEN'))
    monkeypatch.setenv('TRADINGAGENTS_LLM_PROVIDER', 'chatgpt')
    monkeypatch.setenv('TRADINGAGENTS_QUICK_THINK_LLM', 'gpt-6.1-sol')
    monkeypatch.setenv('TRADINGAGENTS_DEEP_THINK_LLM', 'gpt-6.1-sol')
    monkeypatch.delenv('TA_SINGLE_AUDIT_FILE', raising=False)


@pytest.fixture
def gate(tmp_path):
    ledger = Ledger(tmp_path / 'TEST_FIXTURE_LEDGER')
    ledger.initialize(policy())
    config = tmp_path / 'TEST_FIXTURE_CONFIG'; config.mkdir()
    raw = json.dumps(policy().model_dump(), indent=2) + '\n'
    (config / 'policy.json').write_text(raw)
    at = ledger.clock()
    scope = dict(version=p.VERSION, purpose=p.PURPOSE, approved=True, authorization_id='TEST_FIXTURE_SINGLE',
                 attempts_authorized=1, no_retry=True, ledger_root=str(ledger.root),
                 code_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                 module_sha256=hashlib.sha256(Path(p.__file__).read_bytes()).hexdigest(),
                 implementation_hash=implementation_hash(), policy_sha256=hashlib.sha256((config/'policy.json').read_bytes()).hexdigest(),
                 policy_config_hash=digest(ledger.load()['policy']), config_hash=digest(p._config(ledger, policy())),
                 not_before_ms=at-1000, latest_claim_ms=at-1000+60000)
    approval = config / 'approval.json'; approval.write_text(json.dumps(scope))
    return ledger, approval


def bound(gate):
    ledger, approval = gate
    p.claim_once(ledger, approval)
    rid = ledger.start_research(); p.bind_run(ledger, rid, approval)
    return ledger, approval, rid


def run_model(gate, model=None, *, count=1):
    ledger, approval, rid = bound(gate)
    def fixture(config):
        for _ in range(count): (model or FixtureModel()).invoke('PRIVATE_PROMPT')
        return {'tool_count': 1}
    return ledger, p.worker(ledger, rid, approval, invoker=fixture)


def test_exact_minimum_headroom_passes_native_accounting(gate):
    ledger, result = run_model(gate)
    state = ledger.status()
    assert result['status'] == 'PASS'
    assert state['tokens'] == 100 and state['llm_calls'] == state['http'] == 1
    assert next(iter(state['runs'].values()))['outcome'] == 'REVIEW_ONLY'
    assert not state['paused'] and result['purpose'] == p.PURPOSE
    assert read(p.folder(ledger) / 'meter.json')['reported_total_tokens'] == 100


def test_second_model_call_refused_before_http(gate):
    with pytest.raises(SafetyStop, match='TOKEN_HEADROOM|CALL_BUDGET'):
        run_model(gate, count=2)
    state = gate[0].status()
    assert state['llm_calls'] == state['http'] == 1 and state['paused']
    assert read(p.folder(gate[0]) / 'receipt.json')['status'] == 'FAILED'


@pytest.mark.parametrize('field,value', [('research_tokens', 99999), ('headroom_tokens', 99999),
                                       ('campaign_calls', 2), ('research_http', 239)])
def test_probe_policy_cannot_drift(field, value):
    raw = policy().model_dump(); raw[field] = value
    with pytest.raises(ValueError): Policy.model_validate(raw)


def test_default_research_and_both_directions_reject(tmp_path, gate):
    assert Policy().run_purpose == 'research-v1'
    with pytest.raises(SafetyStop, match='PURPOSE_MISMATCH'): g.worker(gate[0], 'unused', invoker=lambda: None)
    with pytest.raises(SafetyStop, match='PURPOSE_MISMATCH'): g.launch(gate[0])
    with pytest.raises(SafetyStop, match='PURPOSE_MISMATCH'): g.watchlist(policy())
    ordinary = Ledger(tmp_path/'ordinary'); ordinary.initialize(Policy())
    with pytest.raises(SafetyStop, match='PURPOSE_MISMATCH'):
        g.launch(ordinary, purpose=p.PURPOSE, approval=gate[1])
    with pytest.raises(SafetyStop, match='PURPOSE_MISMATCH'):
        p.claim_once(ordinary, gate[1])
    assert not ordinary.status()['runs'] and not gate[0].status()['runs']


@pytest.mark.parametrize('key,value', [('approved',False), ('purpose','research-v1'), ('attempts_authorized',2),
    ('no_retry',False), ('policy_sha256','bad'), ('config_hash','bad'), ('implementation_hash','bad'),
    ('code_commit','bad'), ('module_sha256','bad'), ('ledger_root','foreign'), ('latest_claim_ms',0)])
def test_approval_failure_never_claims_or_dispatches(gate, key, value):
    ledger, approval = gate; scope=read(approval); scope[key]=value; approval.write_text(json.dumps(scope))
    with pytest.raises(SafetyStop): p.claim_once(ledger, approval)
    assert not (p.folder(ledger)/'claim.json').exists() and not ledger.status()['runs']


def claim_child(root, approval, output):
    try: p.claim_once(Ledger(root), approval); output.put('WON')
    except (SafetyStop, FileExistsError): output.put('REFUSED')


def test_multiprocess_unique_persistent_claim(gate):
    ctx=mp.get_context('spawn'); output=ctx.Queue()
    children=[ctx.Process(target=claim_child,args=(str(gate[0].root),str(gate[1]),output)) for _ in range(2)]
    for child in children: child.start()
    for child in children: child.join(60); assert child.exitcode==0
    assert sorted(output.get(timeout=5) for _ in children)==['REFUSED','WON']
    before=(p.folder(gate[0])/'claim.json').read_bytes()
    with pytest.raises(FileExistsError): p.claim_once(*gate)
    assert (p.folder(gate[0])/'claim.json').read_bytes()==before


def test_thread_competition(gate):
    def claim(_):
        try: p.claim_once(*gate); return True
        except (SafetyStop, FileExistsError): return False
    with ThreadPoolExecutor(max_workers=6) as pool: assert sum(pool.map(claim,range(6)))==1


def test_binding_before_network_and_worker_restart_refusal(gate):
    ledger, approval, rid=bound(gate)
    def fixture(config):
        assert read(p.folder(ledger)/'binding.json')['run_id']==rid
        assert read(p.folder(ledger)/'receipt.json')['status']=='UNFINALIZED'
        FixtureModel().invoke('PRIVATE')
        return {'tool_count':0}
    result=p.worker(ledger,rid,approval,invoker=fixture)
    assert result['status']=='INCONCLUSIVE_NO_TOOL_CALL'
    with pytest.raises(SafetyStop,match='RESEARCH_NOT_ACTIVE'): p.worker(ledger,rid,approval,invoker=fixture)
    with pytest.raises(SafetyStop,match='PROBE_ALREADY_BOUND'): p.verify_claim(ledger,approval,for_launch=True)
    assert ledger.status()['llm_calls']==1


@pytest.mark.parametrize('model',[FixtureModel(total=None),FixtureModel(partial=True),FixtureModel(total=101)])
def test_unknown_usage_never_zero_or_resumed(gate,model):
    with pytest.raises((SafetyStop,LLMError)):
        run_model(gate,model)
    ledger=gate[0];s=ledger.status()
    assert s['paused'] and s['total_usage']=='UNKNOWN'
    assert read(p.folder(ledger)/'receipt.json')['status']=='USAGE_UNKNOWN'
    with pytest.raises(SafetyStop): ledger.resume()


from tradingagents.llm_clients.reliability import LLMUnavailable as LLMError


def test_zero_call_success_is_not_faked(gate):
    ledger,approval,rid=bound(gate)
    with pytest.raises(SafetyStop,match='PROBE_RECONCILIATION'):
        p.worker(ledger,rid,approval,invoker=lambda _: {'tool_count':1})
    assert ledger.status()['paused'] and read(p.folder(ledger)/'receipt.json')['status']=='FAILED'


def test_crash_recovery_preserves_pending_unknown_and_claim(gate):
    ledger,approval,rid=bound(gate)
    cid=ledger.begin_call(rid);wire=ledger.begin_http(rid,cid,'MODEL');ledger.finish_http(wire,status=200)
    claim=(p.folder(ledger)/'claim.json').read_bytes()
    assert read(p.folder(ledger)/'receipt.json')['status']=='UNFINALIZED'
    ledger.recover_interrupted();p.finalize_interrupted(ledger,rid)
    s=ledger.status()
    assert s['runs'][rid]['status']=='FAILED' and s['calls'][cid]['status']=='PENDING'
    assert s['total_usage']=='UNKNOWN' and s['paused']
    assert read(p.folder(ledger)/'receipt.json')['status']=='USAGE_UNKNOWN'
    assert (p.folder(ledger)/'claim.json').read_bytes()==claim


def test_production_invoker_is_rejected_before_scope_or_network(tmp_path):
    ledger=Ledger(tmp_path/'TEST_FIXTURE_PRODUCTION_SHAPE')
    ledger.initialize(policy(enabled=True,accepted_post_response_exposure=True),approved=True,namespace='PRODUCTION')
    with pytest.raises(SafetyStop,match='FIXTURE_CANNOT_REGISTER_PRODUCTION'):
        p.worker(ledger,'unused',None,invoker=lambda _: pytest.fail('UNSAFE'))


@pytest.mark.parametrize('count',[0,1,6,7])
def test_actual_market_and_sdk_one_response_no_tool_or_followup(gate,pinned,monkeypatch,count):
    rows,_=events(count);requests=[];model,client=model_for(rows,requests)
    monkeypatch.setattr('tradingagents.dataflows.okx_research.preflight',lambda state:
                        {'market_snapshot':copy.deepcopy(pinned),'data_quality':'VALID','allow_new_risk':True})
    monkeypatch.setattr('tradingagents.llm_clients.factory.create_tier_client',lambda *a,**k:NS(get_llm=lambda:model))
    monkeypatch.setattr('tradingagents.forward_validation.Registry.begin',lambda *a,**k:pytest.fail('F1_FORBIDDEN'))
    monkeypatch.setattr('tradingagents.graph.trading_graph.TradingAgentsGraph.__init__',lambda *a,**k:pytest.fail('GRAPH_FORBIDDEN'))
    monkeypatch.setattr('langgraph.prebuilt.ToolNode.invoke',lambda *a,**k:pytest.fail('TOOL_EXECUTION_FORBIDDEN'))
    ledger,approval,rid=bound(gate)
    try: result=p.worker(ledger,rid,approval,invoker=p._market_turn)
    finally: client.close()
    assert result['status']==('PASS' if count else 'INCONCLUSIVE_NO_TOOL_CALL') and result['tool_count']==count
    assert len(requests)==ledger.status()['llm_calls']==ledger.status()['http']==1
    text=(p.folder(ledger)/'audit.jsonl').read_text()
    assert ('MARKET_SAFE_ADMISSION' in text) is bool(count)
    assert 'MARKET_EVENT_DIAGNOSTIC' in text
    for secret in ['2026-11-03','PRIVATE_PROMPT','Authorization','look_back_days": 30']:
        assert secret not in text


def test_approval_config_drift_rejected_before_claim(gate,monkeypatch):
    monkeypatch.setenv('TRADINGAGENTS_OUTPUT_LANGUAGE','DIFFERENT')
    with pytest.raises(SafetyStop,match='PROBE_CONFIG_DRIFT'):p.claim_once(*gate)


def test_no_claim_or_wrong_binding_never_invokes(gate):
    ledger,approval=gate
    rid=ledger.start_research()
    with pytest.raises(FileNotFoundError):
        p.worker(ledger,rid,approval,invoker=lambda _:pytest.fail('NETWORK_BEFORE_CLAIM'))
    assert not ledger.status()['calls'] and not ledger.status()['wires']


def test_changed_binding_never_invokes(gate):
    ledger,approval,rid=bound(gate)
    target=p.folder(ledger)/'binding.json';value=read(target);value['run_id']='foreign';target.write_text(json.dumps(value))
    with pytest.raises(SafetyStop,match='PROBE_BINDING'):
        p.worker(ledger,rid,approval,invoker=lambda _:pytest.fail('NETWORK_BEFORE_BINDING'))
    assert not ledger.status()['calls'] and not ledger.status()['wires']


def test_full_audit_refused_before_any_network(gate):
    from tradingagents.dataflows.audit_file import append_audit
    ledger,approval,rid=bound(gate);audit=p.folder(ledger)/'audit.jsonl'
    append_audit(audit,b'x'*(4194304-2)+b'\n',4194304)
    before=audit.read_bytes()
    with pytest.raises(RuntimeError,match='CAPACITY'):
        p.worker(ledger,rid,approval,invoker=lambda _:pytest.fail('NETWORK_WITHOUT_AUDIT'))
    assert audit.read_bytes()==before and ledger.status()['llm_calls']==ledger.status()['http']==0
    assert ledger.status()['paused'] and read(p.folder(ledger)/'receipt.json')['status']=='FAILED'


@pytest.mark.skipif(os.name=='nt',reason='POSIX audit mode acceptance required on Linux')
def test_bad_audit_mode_refused_before_any_network(gate):
    from tradingagents.dataflows.audit_file import append_audit
    ledger,approval,rid=bound(gate);audit=p.folder(ledger)/'audit.jsonl'
    append_audit(audit,b'{"TEST_FIXTURE":true}\n',4194304);audit.chmod(0o644)
    with pytest.raises(RuntimeError,match='FILE_PERMISSIONS'):
        p.worker(ledger,rid,approval,invoker=lambda _:pytest.fail('NETWORK_WITH_BAD_PERMISSIONS'))
    assert ledger.status()['llm_calls']==ledger.status()['http']==0


@pytest.mark.parametrize('clock_shift',[-5000,72*3600*1000+1])
def test_claim_rejects_before_T0_or_expired_window(gate,clock_shift):
    ledger,approval=gate;at=ledger.load()['events'][0]['at_ms']+clock_shift
    value=read(approval);value.update(not_before_ms=at-1000,latest_claim_ms=at+59000);approval.write_text(json.dumps(value))
    ledger.clock=lambda:at
    with pytest.raises(SafetyStop,match='PROBE_CLAIM_WINDOW'):p.claim_once(ledger,approval)


def test_cli_scope_mismatch_is_fail_closed(gate,monkeypatch,capsys):
    monkeypatch.setattr('sys.argv',['guarded_research','run','--ledger',str(gate[0].root)])
    with pytest.raises(SystemExit) as exc:g.main()
    assert exc.value.code==2 and 'PURPOSE_MISMATCH' in capsys.readouterr().out
    assert not gate[0].status()['runs']


def test_missing_production_fixture_callback_refuses_real_model(gate):
    ledger,approval,rid=bound(gate)
    with pytest.raises(SafetyStop,match='FIXTURE_CANNOT_CALL_MODEL'):p.worker(ledger,rid,approval)
    assert not ledger.status()['calls']


@pytest.mark.parametrize('fault',['unmatched','stream','unknown','threshold'])
def test_real_sdk_failure_and_soft_budget_semantics(gate,pinned,monkeypatch,fault):
    rows,_=events(1)
    if fault=='unmatched':
        extra=copy.deepcopy(rows[0]);extra['output_index']=9;extra['item'].update(id='fc_extra',call_id='call_extra')
        rows.insert(-1,extra)
    elif fault=='stream':rows.pop()
    elif fault=='unknown':rows[-1]['response']['usage']=None
    else:rows[-1]['response']['usage'].update(input_tokens=100050,output_tokens=20,total_tokens=100070)
    requests=[];model,client=model_for(rows,requests)
    monkeypatch.setattr('tradingagents.dataflows.okx_research.preflight',lambda state:
                        {'market_snapshot':copy.deepcopy(pinned),'data_quality':'VALID','allow_new_risk':True})
    monkeypatch.setattr('tradingagents.llm_clients.factory.create_tier_client',lambda *a,**k:NS(get_llm=lambda:model))
    ledger,approval,rid=bound(gate)
    try:
        with pytest.raises((SafetyStop,LLMError)):p.worker(ledger,rid,approval,invoker=p._market_turn)
    finally:client.close()
    state=ledger.status();result=read(p.folder(ledger)/'receipt.json')
    assert len(requests)==state['llm_calls']==state['http']==1
    assert state['paused'] and state['runs'][rid]['status']=='FAILED'
    assert result['status']==('USAGE_UNKNOWN' if fault in {'stream','unknown'} else 'FAILED')
    if fault=='threshold':assert state['tokens']==100070 and 'TOKEN_THRESHOLD_REACHED' in state['paused']
    if fault=='unmatched':assert state['tokens']==100 and 'UNMATCHED_EVENT' in (p.folder(ledger)/'audit.jsonl').read_text()


def test_data_blocked_never_creates_model(gate,monkeypatch):
    monkeypatch.setattr('tradingagents.dataflows.okx_research.preflight',lambda state:
                        {'data_quality':'STALE_DATA','allow_new_risk':False})
    monkeypatch.setattr('tradingagents.llm_clients.factory.create_tier_client',lambda *a,**k:pytest.fail('MODEL_FOR_STALE_DATA'))
    ledger,approval,rid=bound(gate)
    with pytest.raises(SafetyStop,match='PROBE_DATA_BLOCKED'):p.worker(ledger,rid,approval,invoker=p._market_turn)
    assert ledger.status()['llm_calls']==ledger.status()['http']==0


def test_unknown_or_unapproved_probe_without_claim_never_creates_binding(gate):
    ledger,approval=gate
    with pytest.raises(FileNotFoundError):p.verify_claim(ledger,approval,for_launch=True)
    assert not (p.folder(ledger)/'binding.json').exists()


def test_final_receipt_failure_pauses_instead_of_claiming_pass(gate,monkeypatch):
    original=p.atomic;failed=[]
    def write(path,value):
        if path.name=='receipt.json' and value.get('status')=='PASS' and not failed:
            failed.append(True);raise OSError('TEST_FIXTURE_RECEIPT_FAILURE')
        return original(path,value)
    monkeypatch.setattr(p,'atomic',write)
    with pytest.raises(OSError):run_model(gate)
    s=gate[0].status()
    assert s['tokens']==100 and s['llm_calls']==s['http']==1
    assert 'PROBE_FINALIZATION_FAILURE' in s['paused']
    assert read(p.folder(gate[0])/'receipt.json')['status']=='FAILED'
    with pytest.raises(SafetyStop):gate[0].resume()


def test_public_http_is_guarded_after_binding_and_macro_403_counted(gate):
    import httpx
    ledger,approval,rid=bound(gate)
    def fixture(config):
        assert read(p.folder(ledger)/'binding.json')['run_id']==rid
        with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(403,request=req))) as client:
            assert client.get('https://www.bls.gov/feed/cpi.rss').status_code==403
        FixtureModel().invoke('PRIVATE_PROMPT')
        return {'tool_count':1}
    result=p.worker(ledger,rid,approval,invoker=fixture)
    state=ledger.status()
    assert result['status']=='PASS' and state['http']==2 and state['llm_calls']==1
    assert any(e['kind']=='OPTIONAL_SOURCE_UNAVAILABLE' for e in ledger.load()['events'])


def test_concurrent_workers_cannot_make_second_model_request(gate):
    ledger,approval,rid=bound(gate)
    def fixture(config):FixtureModel().invoke('PRIVATE');return {'tool_count':1}
    def worker(_):
        try:return p.worker(ledger,rid,approval,invoker=fixture)['status']
        except (SafetyStop,FileExistsError):return 'REFUSED'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,range(2)))
    assert sorted(results)==['PASS','REFUSED']
    assert ledger.status()['llm_calls']==ledger.status()['http']==1
