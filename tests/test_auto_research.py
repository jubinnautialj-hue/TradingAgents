"""R1.1 fixtures are isolated in pytest temp directories, never real archives."""
import copy
import json
import subprocess
from pathlib import Path

import pytest

from tradingagents import auto_research as r
from tradingagents import research_budget as budget
from tradingagents.forward_validation import Registry, read, atomic
from tests.test_forward_validation import setup as f1_setup
from tests.test_perpetual_plan import research, choice, CONFIG
from tests.test_okx_research import INFO,NOW


def config(symbols=('BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP')):
    return r.Watchlist(instruments=[r.Instrument(instrument=s) for s in symbols])


def validator(cfg,root):
    return {'instruments':[{'instrument':s,'status':'VALID'} for s in cfg.enabled()],
            'meter':{'http_dispatches':0},'execution_enabled':False}


def finished_without_registration(header,folder,seconds):
    meter=budget.Meter(folder/'meter.json',header['grants']['llm'],header['grants']['http'],seconds)
    meter.take('llm_calls')
    r.seal(folder/'receipt.json',{'worker_status':'FINISHED','exit_code':5,'error_type':'FixtureFailure','execution_enabled':False})
    return {'returncode':5,'timed_out':False}


def test_default_watchlist_and_schema():
    assert config().enabled()==['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP']
    assert read(r.ROOT/'docs/auto_research_r11.json')==config().model_dump()
    assert read(r.ROOT/'docs/auto_research_r11.schema.json')==r.Watchlist.model_json_schema()


def test_normalize_dedupe_disable_and_add_remove():
    cfg=r.Watchlist(instruments=[{'instrument':' btc-usdt-swap '},{'instrument':'BTC-USDT-SWAP'},
                                {'instrument':'ETH-USDT-SWAP','enabled':False},{'instrument':'SOL-USDT-SWAP'}])
    assert cfg.enabled()==['BTC-USDT-SWAP','SOL-USDT-SWAP'] and len(cfg.instruments)==3
    cfg.instruments.pop();assert cfg.enabled()==['BTC-USDT-SWAP']
    cfg.instruments.append(r.Instrument(instrument='XRP-USDT-SWAP'));assert cfg.enabled()[-1]=='XRP-USDT-SWAP'
    with pytest.raises(ValueError):r.Watchlist(instruments=[{'instrument':'BTC-USDT-SWAP'},{'instrument':'btc-usdt-swap','enabled':False}])


@pytest.mark.parametrize('symbol',['BTC-USD','BTC-USDT','BTC/USDT','BTC-USD-SWAP','../BTC-USDT-SWAP',''])
def test_spot_and_invalid_instruments_rejected(symbol):
    with pytest.raises(ValueError):r.Instrument(instrument=symbol)


def test_unknown_credentials_and_execution_config_rejected():
    with pytest.raises(ValueError):r.Watchlist.model_validate({**config().model_dump(),'api_key':'fake'})
    with pytest.raises(ValueError):r.Watchlist.model_validate({**config().model_dump(),'execution_enabled':True})


def test_contract_validation_is_public_and_failure_isolated(tmp_path):
    class Client:
        def instrument(self,symbol):
            if symbol.startswith('ETH'):raise ValueError('Not live')
            return {**INFO,'instId':symbol,'instFamily':symbol.removesuffix('-SWAP'),'ctValCcy':symbol.split('-')[0]}
    value=r.validate_pool(config(),tmp_path,client=Client())
    assert [i['status'] for i in value['instruments']]==['VALID','INVALID_OR_UNAVAILABLE','VALID']


def test_serial_failure_isolation_and_same_slot_idempotency(tmp_path):
    calls=[]
    def runner(header,folder,seconds):
        calls.append(header['instrument']);return finished_without_registration(header,folder,seconds)
    first=r.run_batch(config(),tmp_path,namespace='FIXTURE',runner=runner,validator=validator)
    assert calls==config().enabled()
    assert all(row['task_status']=='FAILED_NO_REGISTRATION' for row in first['tasks'])
    second=r.run_batch(config(),tmp_path,namespace='FIXTURE',runner=runner,validator=validator)
    assert len(calls)==3 and first['schedule_slot_ms']==second['schedule_slot_ms']


def test_shared_model_budget_and_unknown_interruption_reservation(tmp_path):
    cfg=config();cfg.limits.llm_total=1
    calls=[]
    def crash(header,folder,seconds):calls.append(header['instrument']);raise OSError('fixture crash')
    value=r.run_batch(cfg,tmp_path,namespace='FIXTURE',runner=crash,validator=validator)
    assert len(calls)==1 and value['budget']['llm_used_or_reserved']==1
    assert any(n['reason']=='BATCH_BUDGET_EXHAUSTED' for n in value['notes'])
    r.run_batch(cfg,tmp_path,namespace='FIXTURE',runner=crash,validator=validator)
    assert len(calls)==1


def test_config_change_does_not_rerun_same_slot(tmp_path):
    r.run_batch(config(),tmp_path,namespace='FIXTURE',runner=finished_without_registration,validator=validator)
    changed=config();changed.enhancements.orderflow=True
    def forbidden(*a):raise AssertionError('Duplicate research')
    value=r.run_batch(changed,tmp_path,namespace='FIXTURE',runner=forbidden,validator=validator)
    assert all(n['reason']=='FROZEN_SLOT_CONFIG_CONFLICT' for n in value['notes'])


def test_frequency_does_not_reset_at_slot_boundary(tmp_path,monkeypatch):
    cfg=config(('BTC-USDT-SWAP',));period=cfg.limits.min_interval_seconds*1000
    clock=[period*10+period-1000];monkeypatch.setattr(r,'utc_ms',lambda:clock[0])
    r.run_batch(cfg,tmp_path,namespace='FIXTURE',runner=finished_without_registration,validator=validator)
    clock[0]+=2000
    value=r.run_batch(cfg,tmp_path,namespace='FIXTURE',runner=finished_without_registration,validator=validator)
    assert not value['tasks'] and value['notes'][0]['reason']=='MINIMUM_INTERVAL'


def test_existing_orphan_worker_prevents_new_batch(tmp_path):
    store=r.Store(tmp_path,'FIXTURE')
    with r.locked(store.root/'worker'):
        assert r.run_batch(config(),tmp_path,namespace='FIXTURE')['status']=='ORPHAN_WORKER_BUSY'


@pytest.mark.parametrize('kind',['llm_calls','http_dispatches'])
def test_meter_pre_dispatch_caps_and_tamper_detection(tmp_path,kind):
    m=budget.Meter(tmp_path/'meter.json',2,2,10)
    m.take(kind);m.take(kind)
    with pytest.raises(budget.ResearchBudgetExceeded):m.take(kind)
    assert budget.read_meter(m.path)[kind]==2
    raw=read(m.path);raw[kind]=0;atomic(m.path,raw)
    with pytest.raises(ValueError,match='fingerprint'):budget.read_meter(m.path)


def test_langchain_guard_counts_actual_logical_calls(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    m=budget.Meter(tmp_path/'meter.json',1,2,10)
    with budget.guards(m):
        model=FakeListChatModel(responses=['fixture'])
        assert model.invoke('x').content=='fixture'
        with pytest.raises(budget.ResearchBudgetExceeded):model.invoke('y')
    assert budget.read_meter(m.path)['llm_calls']==1


@pytest.mark.parametrize('module_name',['httpx','httpx2'])
def test_http_transport_dispatch_budget_includes_repeated_requests(tmp_path,module_name):
    import importlib
    module=importlib.import_module(module_name)
    m=budget.Meter(tmp_path/'meter.json',1,1,10)
    transport=module.MockTransport(lambda request:module.Response(200,json={'fixture':True}))
    with budget.guards(m),module.Client(transport=transport) as client:
        assert client.get('https://model.example/test').status_code==200
        with pytest.raises(budget.ResearchBudgetExceeded):client.get('https://model.example/test')
    assert budget.read_meter(m.path)['http_dispatches']==1


@pytest.mark.parametrize('method,url',[('POST','https://www.okx.com/api/v5/trade/order'),
    ('GET','https://www.okx.com/api/v5/account/balance'),('DELETE','https://fapi.binance.com/fapi/v1/order')])
def test_private_exchange_transport_blocked(method,url):
    with pytest.raises(ValueError):budget.public_exchange_only(method,url)


def test_task_worker_timeout_kills_own_process(monkeypatch,tmp_path):
    killed=[]
    class Process:
        def __init__(self,*a,**k):self.n=0
        def wait(self,timeout=None):
            self.n+=1
            if self.n==1:raise subprocess.TimeoutExpired('fixture',timeout)
            return -9
        def kill(self):killed.append(True)
    monkeypatch.setattr(r.subprocess,'Popen',Process)
    assert r.run_worker({},tmp_path,1)['timed_out'] and killed


def test_validation_timeout_reserves_http_and_blocks_models(tmp_path,monkeypatch):
    def timeout(*a,**k):raise subprocess.TimeoutExpired('fixture',1)
    monkeypatch.setattr(r.subprocess,'run',timeout)
    value=r.validate_supervised(config(),tmp_path)
    assert value['status']=='VALIDATION_TIMEOUT' and value['meter']['http_dispatches']==config().limits.http_total


@pytest.mark.parametrize('mode',['candidate','wait','failure'])
def test_worker_reuses_f1_registration_and_isolates_fixtures(f1_setup,tmp_path_factory,monkeypatch,mode):
    from tradingagents.dataflows import perpetual_plan
    # Native F1 keeps its original Windows path behavior; short test roots avoid
    # MAX_PATH from pytest's long descriptive test-name directories.
    cfg=config(('BTC-USDT-SWAP',));store=r.Store(tmp_path_factory.mktemp('r11'),'FIXTURE')
    header=store.create('BTC-USDT-SWAP',NOW,cfg,{'llm':2,'http':5,'seconds':20})
    state=copy.deepcopy(f1_setup[2]);bundle=state['market_snapshot']
    if mode=='wait':
        state['portfolio_choice']=choice('WAIT')
        result,_=perpetual_plan.finalize_result(state,bundle,CONFIG)
        state['research_result']=result.model_dump(mode='json')
    if mode=='failure':
        state.update(research_result=None,workflow_status='FAILED',llm_status='LLM_UNAVAILABLE',llm_failure={'kind':'MODEL_SERVICE_ERROR'})
    def invoke(argv):
        assert '--forward-register' in argv and '--trade-plan' in argv and '--events' in argv
        registry=Registry(Path(header['results_dir'])/'forward_validation/f1')
        token=registry.begin(header['instrument'],CONFIG)
        folder=Path(header['results_dir'])/'reports'/'fixture';folder.mkdir(parents=True)
        (folder/'complete_report.md').write_text('Research fixture',encoding='utf-8')
        (folder/'complete_report.html').write_text('Research fixture',encoding='utf-8')
        atomic(folder/'multitimeframe_snapshot.json',bundle)
        if state.get('research_result'):atomic(folder/'research_result.json',state['research_result'])
        registry.register(folder,state,token=token,completed_ms=NOW+1000)
    value=r.worker(store.directory(header['task_id']),invoker=invoke)
    reg=value['registration']
    assert reg['origin']=='TEST_REPLAY' and reg['allow_new_risk'] is False
    assert reg['workflow_status']==('FAILED' if mode=='failure' else 'COMPLETED')
    recovered=r.recover(store,header)
    assert recovered['registration']['registration_id']==reg['registration_id']
    assert r.summary(store,[header])['tasks'][0]['research_status']=='REPLAY_ONLY'


def test_registration_journal_recovery_never_reinvokes_model(f1_setup,tmp_path,monkeypatch):
    registry,folder,state,token,clock,save=f1_setup
    # Build a production-shape test header pointing only at the pytest registry.
    store=r.Store(tmp_path/'batch','PRODUCTION');cfg=config(('BTC-USDT-SWAP',))
    h=store.create('BTC-USDT-SWAP',NOW,cfg,{'llm':2,'http':5,'seconds':20})
    h['results_dir']=str(registry.root.parent.parent)
    # Existing fixture registry path is .../registry, not the production layout;
    # move the captured F1 journal into an isolated standard path for recovery.
    source=Registry(tmp_path/'recovered'/'forward_validation/f1')
    monkeypatch.setattr(__import__('tradingagents.forward_validation',fromlist=['utc_ms']),'utc_ms',lambda:NOW)
    t=source.begin('BTC-USDT-SWAP',CONFIG)
    original=__import__('tradingagents.forward_validation',fromlist=['atomic']).atomic
    def interrupt(path,value):
        if len(path.stem)==64:raise OSError('fixture crash at final seal')
        original(path,value)
    monkeypatch.setattr('tradingagents.forward_validation.atomic',interrupt)
    with pytest.raises(OSError):source.register(folder,state,token=t,completed_ms=NOW)
    monkeypatch.setattr('tradingagents.forward_validation.atomic',original)
    h['results_dir']=str(tmp_path/'recovered')
    r.seal(store.directory(h['task_id'])/'receipt.json',{'registry_root':str(source.root),'intent_token':t,'report_directory':str(folder),'worker_status':'REGISTRATION_PENDING'})
    value=r.recover(store,h)
    assert value['registration']['origin']=='PROSPECTIVE' and value['recovered_registration_only']


def test_original_cli_and_f3_not_reimplemented():
    header={'instrument':'ETH-USDT-SWAP','config':config().model_dump()}
    args=r.command_for(header)
    assert args[0]=='okx-research' and '--forward-register' in args
    assert not any('forward_paper' in x or 'forward_tracking' in x for x in args)


def test_unsubmitted_queued_task_can_resume_without_duplicate_dispatch(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'utc_ms',lambda:NOW)
    cfg=config(('BTC-USDT-SWAP',));store=r.Store(tmp_path,'FIXTURE')
    slot=NOW//(cfg.limits.min_interval_seconds*1000)*(cfg.limits.min_interval_seconds*1000)
    store.create('BTC-USDT-SWAP',slot,cfg,{'llm':2,'http':5,'seconds':20})
    calls=[]
    def runner(h,f,s):calls.append(h['task_id']);return finished_without_registration(h,f,s)
    r.run_batch(cfg,tmp_path,namespace='FIXTURE',runner=runner,validator=validator)
    r.run_batch(cfg,tmp_path,namespace='FIXTURE',runner=runner,validator=validator)
    assert len(calls)==1


def test_interrupted_validation_reservation_is_not_reset(tmp_path):
    def crash(*a):raise OSError('fixture controller interruption')
    with pytest.raises(OSError):r.run_batch(config(),tmp_path,namespace='FIXTURE',validator=crash)
    def forbidden(*a):raise AssertionError('Prior HTTP reservation must not disappear')
    result=r.run_batch(config(),tmp_path,namespace='FIXTURE',runner=forbidden,validator=forbidden)
    assert not result['tasks'] and result['budget']['http_used_or_reserved']==config().limits.http_total


def test_same_slot_quota_failure_is_not_retried_on_restart(tmp_path):
    calls=[]
    def exhausted(h,f,s):
        calls.append(h['instrument']);finished_without_registration(h,f,s)
        r.seal(f/'receipt.json',{'worker_status':'FINISHED','failure_kind':'QUOTA_EXHAUSTED','execution_enabled':False})
        return {'returncode':5,'timed_out':False}
    r.run_batch(config(),tmp_path,namespace='FIXTURE',runner=exhausted,validator=validator)
    result=r.run_batch(config(),tmp_path,namespace='FIXTURE',runner=exhausted,validator=validator)
    assert len(calls)==1 and all(n['reason']=='SHARED_MODEL_RESTRICTION' for n in result['notes'])
