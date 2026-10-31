"""Offline production-entry wiring, frozen data and deterministic model substitutes.
Production-shaped F1 records live ONLY under pytest FIXTURE paths, never formal samples.
"""
import copy
import json
from pathlib import Path
import pytest
from langchain_core.runnables import RunnableLambda
from typer.testing import CliRunner
from tradingagents import auto_research as native, guarded_research as guard
from tradingagents import forward_validation as f, observation_eligibility as obs
from tradingagents import registration_revalidation as admission
from tradingagents.safety_budget import Policy
from tests.test_perpetual_plan import research, CONFIG, PlanClient, PlanModel, _Client, PerpetualChoice, choice
from tests.test_graph_end_to_end import ARGS
from tests.test_okx_research import NOW, DAY, SYMBOL

V=admission.VERSION

@pytest.fixture(autouse=True)
def no_wire(monkeypatch):
    import requests, httpx
    def denied(*a,**k):pytest.fail('Real network forbidden in entry-wiring tests')
    monkeypatch.setattr(requests.Session,'send',denied)
    send=httpx.Client._send_single_request
    def mock_only(client,*a,**k):
        if not isinstance(client._transport,httpx.MockTransport):denied()
        return send(client,*a,**k)
    monkeypatch.setattr(httpx.Client,'_send_single_request',mock_only)

@pytest.mark.parametrize('bad',[None,True,1,{},[], 'unknown', 'forward-registration-revalidation-v2'])
@pytest.mark.parametrize('model',[Policy,native.Watchlist])
def test_policy_rejects_invalid_types_and_values(bad,model):
    kwargs={'forward_admission_policy':bad}
    if model is native.Watchlist:kwargs['instruments']=[{'instrument':SYMBOL}]
    with pytest.raises(ValueError):model(**kwargs)

@pytest.mark.parametrize('selected',['strict-v1',V])
def test_guarded_policy_reaches_cli_and_audit_hash(tmp_path,selected):
    policy=Policy(forward_admission_policy=selected)
    cfg=guard.watchlist(policy)
    assert cfg.forward_admission_policy==selected
    store=native.Store(tmp_path/'FIXTURE','FIXTURE')
    header=store.create(SYMBOL,NOW,cfg,{'llm':24,'http':240,'seconds':900})
    args=native.command_for(header)
    assert args[args.index('--forward-admission-policy')+1]==selected
    assert header['config_hash']==f.digest(header['config'])
    changed=cfg.model_copy(update={'forward_admission_policy':V if selected=='strict-v1' else 'strict-v1'})
    with pytest.raises(ValueError,match='different frozen configuration'):
        store.create(SYMBOL,NOW,changed,header['grants'])
    schema=json.loads((f.ROOT/'docs/d4b_safety_budget.schema.json').read_text())
    assert schema==Policy.model_json_schema()
    assert Policy().forward_admission_policy=='strict-v1'


def install_scripted_cli(monkeypatch,tmp_path,*,aged=True,action='OPEN_LONG_CANDIDATE'):
    """Use REAL graph/CLI/report/Registry with the existing scripted model.
    Neither policy nor proofs are injected into research/F1 dictionaries.
    """
    from cli import main
    from tradingagents.okx_traded_comparison import safe_config  # Import native subclasses before replacing the transport factory.
    from tradingagents.forward_paper import revalidation_ready
    from tradingagents.graph import trading_graph
    from tradingagents.dataflows.vendors import okx,okx_derivatives as d
    from tradingagents.memory import okx_simulation
    clock=[NOW]
    class TimedModel(PlanModel):
        model_name: str = "gpt-6.1-sol"

        def _generate(self,messages,stop=None,run_manager=None,**kwargs):
            import langchain_openai.chat_models.base as sdk
            import httpx
            with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200))) as client:
                client.post("https://api.openai.com/v1/responses",json={"fixture":True}).raise_for_status()
            value=super()._generate(messages,stop=stop,run_manager=run_manager,**kwargs)
            raw={"input_tokens":80,"output_tokens":20,"total_tokens":100,
                 "input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}
            msg=value.generations[0].message
            msg.usage_metadata=sdk._create_usage_metadata_responses(raw)
            msg.response_metadata={"id":f"resp_FIXTURE_{len(self.calls)}","status":"completed"}
            return value

        def with_structured_output(self,schema,**kwargs):
            if schema is PerpetualChoice:
                def complete(_):
                    self._count()
                    clock[0]=NOW+301000 if aged else NOW+1000
                    return PerpetualChoice.model_validate(choice(self.action))
                return RunnableLambda(complete)
            return super().with_structured_output(schema,**kwargs)
    model=TimedModel(structured=True,action=action)
    monkeypatch.setattr(trading_graph,'create_tier_client',lambda *a,**k:_Client(model))
    monkeypatch.setattr(okx,'OKXPublicClient',lambda:PlanClient(now=clock[0]))
    monkeypatch.setattr(okx,'utc_ms',lambda:clock[0])
    monkeypatch.setattr(d,'utc_ms',lambda:clock[0])
    monkeypatch.setattr(f,'utc_ms',lambda:NOW-1000 if clock[0]==NOW else clock[0]+1000)
    monkeypatch.setattr(okx_simulation,'utc_ms',lambda:clock[0]+500)
    for key in ['curr_date','end_date','start_date']:monkeypatch.setitem(ARGS,key,DAY)
    cfg={**copy.deepcopy(main.DEFAULT_CONFIG),**CONFIG,'results_dir':str(tmp_path/'FIXTURE'),
         'memory_log_path':str(tmp_path/'FIXTURE-memory.md')}
    cfg.pop('forward_admission_policy',None)
    monkeypatch.setattr(main,'DEFAULT_CONFIG',cfg)
    return main,clock,model

@pytest.mark.parametrize('selected,aged,action,eligible',[
    (None,False,'OPEN_LONG_CANDIDATE',True),
    (None,True,'OPEN_LONG_CANDIDATE',False),
    (V,True,'OPEN_LONG_CANDIDATE',True),
    (V,False,'OPEN_LONG_CANDIDATE',True),
    (V,True,'WAIT',False),
])
def test_real_cli_graph_report_f1_f21(research,monkeypatch,tmp_path,selected,aged,action,eligible):
    main,clock,model=install_scripted_cli(monkeypatch,tmp_path,aged=aged,action=action)
    cmd=['okx-research','--date',DAY,'--trade-plan','--taker-fee-bps','1','--forward-register']
    if selected:cmd+=['--forward-admission-policy',selected]
    outcome=CliRunner().invoke(main.app,cmd)
    assert outcome.exit_code==0,(outcome.output,''.join(__import__('traceback').format_exception(outcome.exception)))
    registry=f.Registry(tmp_path/'FIXTURE/forward_validation/f1')
    records=list(registry.registrations());assert len(records)==1
    rec=records[0];assert rec.origin=='PROSPECTIVE' # production shape ONLY in pytest FIXTURE
    assert rec.config['forward_admission_policy']==(selected or 'strict-v1')
    assert rec.status==('AWAITING_F2' if eligible else 'ARCHIVED_ONLY')
    assert obs.assess(registry.load(rec.registration_id),clock[0]+1000).observation_eligible==eligible
    assert model.calls
    if selected and eligible:
        assert admission.verify(rec)
        proof=rec.quality_evidence[admission.KEY]
        assert proof['version']==V and proof['snapshot_id']==rec.snapshot_id
        assert not obs.assess(rec,rec.expires_ms).observation_eligible
        from tradingagents.forward_paper import revalidation_ready
        assert not revalidation_ready(proof,{'hypothesis_expires_ms':rec.expires_ms},clock[0])
        if aged:
            assert 'STALE_DATA' in json.dumps(rec.quality_evidence)
            assert rec.original_result['evidence']['entry_revalidation']['checked_at_ms']==clock[0]
    else:assert admission.KEY not in rec.quality_evidence
    assert not rec.execution_enabled and not rec.allow_new_risk


def test_auto_worker_actual_cli_policy_transport(research,monkeypatch,tmp_path,tmp_path_factory):
    main,clock,model=install_scripted_cli(monkeypatch,tmp_path)
    # Native production-shaped worker under an isolated FIXTURE root; no global
    # production Ledger/approval/claim is initialized. All data/models are substitutes.
    cfg=guard.watchlist(Policy(forward_admission_policy=V))
    cfg.enhancements.events=False
    store=native.Store(tmp_path_factory.mktemp('FIXTURE-entry'),'PRODUCTION')
    header=store.create(SYMBOL,NOW,cfg,{'llm':24,'http':240,'seconds':900})
    seen=[]
    def invoke(args):
        monkeypatch.setitem(main.DEFAULT_CONFIG,'results_dir',header['results_dir'])
        outcome=CliRunner().invoke(main.app,args+['--date',DAY])
        seen.append(outcome)
        assert outcome.exit_code==0,(outcome.output,''.join(__import__('traceback').format_exception(outcome.exception)))
    value=native.worker(store.directory(header['task_id']),invoker=invoke)
    assert len(seen)==1 and value['worker_status']=='FINISHED'
    assert value['forward_admission_policy']==V and value['config_hash']==header['config_hash']
    reg=value['registration'];assert reg['forward_admission_policy']==V
    registry=f.Registry(Path(header['results_dir'])/'forward_validation/f1')
    record=registry.load(reg['registration_id'])
    assert record.status=='AWAITING_F2' and admission.verify(record)
    assert obs.assess(record,clock[0]+1000).observation_eligible
    assert native.recover(store,header)['registration']==reg

@pytest.mark.parametrize('bad',['other','true','1','STRICT-V1'])
def test_cli_invalid_enum_rejects_before_research(bad):
    from cli.main import app
    r=CliRunner().invoke(app,['okx-research','--forward-admission-policy',bad])
    assert r.exit_code==2


def test_cli_configuration_conflict_rejects_before_models(monkeypatch):
    from cli import main
    monkeypatch.setitem(main.DEFAULT_CONFIG,'forward_admission_policy','strict-v1')
    r=CliRunner().invoke(main.app,['okx-research','--forward-register','--trade-plan','--forward-admission-policy',V])
    assert r.exit_code!=0 and 'Conflicting forward admission policy' in r.output

@pytest.mark.parametrize('extra',[[],['--forward-register'],['--trade-plan'],['--data-only','--trade-plan','--forward-register']])
def test_new_policy_requires_registration_research(extra):
    from cli.main import app
    r=CliRunner().invoke(app,['okx-research','--forward-admission-policy',V,*extra])
    assert r.exit_code!=0


def test_task_configuration_drift_stops_before_invocation(tmp_path):
    cfg=guard.watchlist(Policy(forward_admission_policy=V));store=native.Store(tmp_path/'FIXTURE','FIXTURE')
    h=store.create(SYMBOL,NOW,cfg,{'llm':24,'http':240,'seconds':900})
    h['config']['forward_admission_policy']='strict-v1'
    native.seal(store.directory(h['task_id'])/'task.json',h)
    with pytest.raises(ValueError,match='configuration fingerprint'):
        native.worker(store.directory(h['task_id']),invoker=lambda _:pytest.fail('Drift must not run'))


def test_worker_cli_policy_conflict_stops_before_f1_intent(tmp_path):
    cfg=guard.watchlist(Policy(forward_admission_policy=V));store=native.Store(tmp_path/'FIXTURE','FIXTURE')
    h=store.create(SYMBOL,NOW,cfg,{'llm':24,'http':240,'seconds':900})
    registry=f.Registry(Path(h['results_dir'])/'forward_validation/f1')
    with pytest.raises(ValueError,match='admission policy conflict'):
        native.worker(store.directory(h['task_id']),invoker=lambda _:registry.begin(SYMBOL,{'forward_admission_policy':'strict-v1'}))
    assert not list(registry.root.glob('*.intent.json'))



def test_guarded_worker_actual_cli_uses_selected_policy(research,monkeypatch,tmp_path,tmp_path_factory):
    from tradingagents.safety_budget import Ledger
    main,clock,model=install_scripted_cli(monkeypatch,tmp_path)
    # The real global guard + native worker + real CLI; namespaces remain FIXTURE.
    # The paired production-shaped native test above proves PROSPECTIVE proof sealing.
    ledger=Ledger(tmp_path_factory.mktemp('FIXTURE-guard'),clock=lambda:NOW+2000)
    ledger.initialize(Policy(forward_admission_policy=V))
    rid=ledger.start_research()
    monkeypatch.setattr(native,'utc_ms',lambda:NOW+2000)
    monkeypatch.setattr(native,'validate_pool',lambda cfg,root:{'instruments':[{'instrument':SYMBOL,'status':'VALID'}],
        'meter':{'http_dispatches':0},'execution_enabled':False})
    watchlist=guard.watchlist
    def fixture_watchlist(policy):
        cfg=watchlist(policy)
        cfg.enhancements.events=False  # No RSS in this deterministic fixture.
        return cfg
    monkeypatch.setattr(guard,'watchlist',fixture_watchlist)
    def invoke(args):
        monkeypatch.setitem(main.DEFAULT_CONFIG,'results_dir',str(ledger.root/'batch/fixture/research'))
        r=CliRunner().invoke(main.app,args+['--date',DAY])
        assert r.exit_code==0,(r.output,r.exception)
    guard.worker(ledger,rid,invoker=invoke)
    status=ledger.status()
    assert status['runs'][rid]['status']=='COMPLETED' and not status['paused']
    assert status['tokens']>0 and all(c['status']=='COMPLETE' for c in status['calls'].values())
    store=native.Store(ledger.root/'batch','FIXTURE');header=store.tasks()[0]
    receipt=native.unseal(store.directory(header['task_id'])/'receipt.json')
    assert receipt['forward_admission_policy']==V
    reg=receipt['registration'];assert reg['origin']=='TEST_REPLAY'
    registry=f.Registry(ledger.root/'batch/fixture/research/forward_validation/f1')
    record=registry.load(reg['registration_id'])
    # Native FIXTURE intentionally drops the prospective intent/token. The
    # selected policy remains in header/receipt, not a forged registration proof.
    assert record.origin=='TEST_REPLAY' and admission.KEY not in record.quality_evidence
    assert not record.simulation_pool_eligible  # Fixture cannot claim formal admission.


def test_cli_runtime_policy_drift_rejects_before_sealing(research,monkeypatch,tmp_path):
    main,clock,model=install_scripted_cli(monkeypatch,tmp_path)
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    propagate=TradingAgentsGraph.propagate
    def drift(graph,*args,**kwargs):
        result=propagate(graph,*args,**kwargs)
        graph.config['forward_admission_policy']='strict-v1'
        return result
    monkeypatch.setattr(TradingAgentsGraph,'propagate',drift)
    r=CliRunner().invoke(main.app,['okx-research','--date',DAY,'--trade-plan','--taker-fee-bps','1',
        '--forward-register','--forward-admission-policy',V])
    assert r.exit_code!=0 and 'policy changed during research' in r.output
    registry=f.Registry(tmp_path/'FIXTURE/forward_validation/f1')
    assert not list(registry.registrations())
    assert not list((tmp_path/'FIXTURE').glob('reports/*/research_result.json'))
