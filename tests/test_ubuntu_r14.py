"""R1.4 local, isolated deployment/supervision/probe contracts, no real trades."""
import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents import ubuntu_deployment as d, ubuntu_operations as o
from tradingagents import automation as a, forward_paper as p
from tests.test_automation import cfg_for
from tests.test_forward_validation import setup as f1_setup,register as register_f1
from tests.test_perpetual_plan import research
from tests.test_forward_paper import T,HOUR


@pytest.mark.parametrize('change',[{'user':'root'},{'release_dir':'/'},{'release_dir':'/opt/x%h'},
    {'release_dir':'/opt/x\nExecStart=/bin/false'},{'release_dir':'/opt/../etc'},
    {'release_dir':'/var/lib/tradingagents/r14'},{'simulation_admitted':True},
    {'allow_new_risk':True},{'execution_enabled':True},{'min_free_bytes':0},{'clock_offset_limit_ms':15000}])
def test_bad_deployment_cannot_grant_permissions_or_inject_units(change):
    with pytest.raises(ValueError):d.Deployment(**change)


def test_schema_configuration_and_parent_unchanged():
    assert a.read(d.ROOT/'docs/ubuntu_r14.json')==dataclasses.asdict(d.Deployment())
    assert a.read(d.ROOT/'docs/ubuntu_r14.schema.json')==d.schema()
    assert a.digest(p.POLICY)=='e29998a373c1295791116b026a04f18c4067f5c14a1352cfad2a4b93066dbd64'


def test_export_units_isolation_resources_and_no_enable():
    units=d.services(d.Deployment(),'a','b','c')
    assert len([k for k in units if k.endswith('.service')])==5 # four + optional quote-only probe
    for phase in d.PHASES:
        service=units[f'ta-r14-{phase}.service'];timer=units[f'ta-r14-{phase}.timer']
        for expected in ('User=ta-research','UMask=0077','KillMode=control-group','Restart=no','MemoryMax=',
                         'NoNewPrivileges=yes','ProtectSystem=strict','ReadWritePaths=/var/lib/tradingagents/r14'):
            assert expected in service
        assert f'run {phase} --bundle' in service
        assert 'Persistent=false' in timer and 'AccuracySec=1s' in timer and 'RandomizedDelaySec=0' in timer
        assert 'enable' not in service and '%h' not in service
    assert '*:58:40 UTC' in units['ta-r14-timely.timer']
    assert 'run-probe' in units['ta-r14-probe.service']
    assert '/var/lib/tradingagents/r14/logs/*.log' in units['tradingagents-r14.logrotate']
    assert 'ledger' not in units['tradingagents-r14.logrotate']


def test_prepare_manifest_new_only_and_source_tamper(tmp_path):
    folder=tmp_path/'export';result=d.prepare(folder,d.Deployment())
    assert result['status']=='PREPARED_NOT_INSTALLED'
    config,manifest=d.validate_bundle(folder,source_root=d.ROOT)
    assert not manifest['server_deployment_verified'] and not manifest['timers_enabled']
    auto=a.AutomationConfig.model_validate(a.read(folder/'automation.json'))
    assert len(auto.sources)==1 and auto.sources[0].name=='ubuntu-live'
    assert auto.schedule.research_seconds==14400
    assert 'r11_live_20261024' not in (folder/'automation.json').read_text()
    with pytest.raises(ValueError):d.prepare(folder,d.Deployment())
    (folder/'watchlist.json').write_text('{}')
    with pytest.raises(ValueError,match='fingerprint'):d.validate_bundle(folder)


def test_command_routing_keeps_native_four_channels(tmp_path):
    manifest={'native_automation_hash':'auto','native_profile_hash':'profile'}
    for phase in d.PHASES:
        command=o.command_for(phase,tmp_path,manifest)
        assert command[0]==sys.executable
        assert ('tradingagents.automation' in command)==(phase in {'research','observe'})
        assert ('--expected-profile-hash' in command)==(phase in {'paper','timely'})


def test_child_timeout_bad_output_and_classification(tmp_path):
    assert o.bounded_child([sys.executable,'-c','import time;time.sleep(5)'],.1,tmp_path)['status']=='TIMEOUT_OR_OUTPUT_BUDGET'
    assert o.bounded_child([sys.executable,'-c','print("{}")'],3,tmp_path)['status']=='CHILD_COMPLETED'
    result=o.bounded_child([sys.executable,'-c','print("{}");raise SystemExit(2)'],3,tmp_path)
    assert result['status']=='CHILD_FAILED'
    with pytest.raises(ValueError):o.bounded_child([sys.executable,'-c','print("not-json")'],3,tmp_path)


@pytest.fixture
def probe_clock(monkeypatch):
    now=[T-80000];mono=[0.]
    monkeypatch.setattr(a.r11,'utc_ms',lambda:now[0]);monkeypatch.setattr(p.okx,'utc_ms',lambda:now[0])
    def sleep(seconds):now[0]+=round(seconds*1000);mono[0]+=seconds
    return now,mono,sleep


class Quotes:
    def __init__(self,clock,delay=0):self.clock=clock;self.calls=0;self.delay=delay
    def get(self,path,params=None):
        self.calls+=1
        if path=='/api/v5/public/time':return [{'ts':str(self.clock[0])}]
        assert path=='/api/v5/market/candles'
        self.clock[0]+=self.delay
        return [[str(T),'100','100','100','100','1','1','100','0']]


def test_probe_armed_future_boundary_on_time_not_trade_and_idempotent(tmp_path,probe_clock):
    now,mono,sleep=probe_clock;path=tmp_path/'probe.json'
    plan=o.arm_probe(path,T,now=now[0])
    client=Quotes(now)
    result=o.run_probe(path,client=client,sleep=sleep,clock=lambda:now[0],monotonic=lambda:mono[0])
    assert result['status']=='ON_TIME_PUBLIC_QUOTES_NOT_TRADES' and len(result['observations'])==3
    assert client.calls==4 and result['paper_trades']==0
    assert not result['simulation_admitted'] and not result['allow_new_risk'] and not result['execution_enabled']
    calls=client.calls;assert o.run_probe(path,client=client)==result and client.calls==calls
    assert not list(tmp_path.glob('**/ledger.json'))


@pytest.mark.parametrize('kind',['missed','restart','future','tamper','clock_jump','late_quote'])
def test_probe_failures_never_backfill_or_trade(tmp_path,probe_clock,kind):
    now,mono,sleep=probe_clock;path=tmp_path/'probe.json';plan=o.arm_probe(path,T,now=now[0])
    if kind=='missed':now[0]=T+16000
    if kind=='restart':a.r11.seal(path.with_name(plan['plan_id']+'.attempt.json'),{'started_ms':now[0]})
    if kind=='future':
        with pytest.raises(ValueError):o.arm_probe(tmp_path/'bad.json',T-HOUR,now=now[0])
        return
    if kind=='tamper':
        value=a.read(path);value['payload']['target_ms']+=HOUR;a.atomic(path,value)
        with pytest.raises(ValueError):o.run_probe(path)
        return
    def jumping(seconds):sleep(seconds);now[0]+=3000
    client=Quotes(now,delay=16000 if kind=='late_quote' else 0)
    result=o.run_probe(path,client=client,sleep=jumping if kind=='clock_jump' else sleep,
                       clock=lambda:now[0],monotonic=lambda:mono[0])
    assert result['status'] in {'MISSED_NO_BACKFILL','INTERRUPTED_UNCONFIRMED','MISSED_OR_UNCONFIRMED'}
    assert result['paper_trades']==0
    if kind in {'missed','restart'}:assert client.calls==0


def test_empty_status_readonly_no_forward_performance(tmp_path):
    cfg=cfg_for(tmp_path,tmp_path/'missing');profiles=o.components()[1].load_config(o.components()[1].DEFAULT_CONFIG)
    value=o.unified_status(cfg,profiles)
    assert len(value['assets'])==3 and value['dispatch_completion_rate'] is None
    assert all(not row['paper_books'] and row['f1']['registered']==0 for row in value['assets'].values())
    assert not (tmp_path/'state').exists() and not (tmp_path/'missing').exists()


def test_wait_registration_visible_but_not_trade(f1_setup,tmp_path):
    state=f1_setup[2];state['research_result'].update(proposed_action='WAIT',plan_valid=False,plan_status='PLAN_INCOMPLETE')
    f1_setup[5]();record=register_f1(f1_setup)
    cfg=cfg_for(tmp_path,f1_setup[0].root);r13=o.components()[1]
    value=o.unified_status(cfg,r13.load_config(r13.DEFAULT_CONFIG))
    assert value['assets']['BTC-USDT-SWAP']['f1']['wait']==1
    assert not value['assets']['BTC-USDT-SWAP']['paper_books'][0]['paper']['trades']


def test_model_failure_does_not_lock_observation_supervisor(tmp_path,monkeypatch):
    config=SimpleNamespace(data_dir=str(tmp_path),clock_offset_limit_ms=1000)
    monkeypatch.setattr(o,'runtime_check',lambda *args:(config,{'manifest_hash':'fixture'}, {'ready':True}))
    monkeypatch.setattr(o,'native_config',lambda *args:(cfg_for(tmp_path,tmp_path/'registry'),None))
    monkeypatch.setattr(o,'command_for',lambda phase,*args:[phase])
    monkeypatch.setattr(o,'bounded_child',lambda command,*args:{'status':'CHILD_FAILED' if command==['research'] else 'CHILD_COMPLETED'})
    assert o.run_phase(tmp_path,'research')['status']=='CHILD_FAILED'
    assert o.run_phase(tmp_path,'observe')['status']=='CHILD_COMPLETED'
    assert len(list((tmp_path/'operations').glob('*.finished.json')))==2


def test_capacity_pauses_research_without_touching_observation(tmp_path,monkeypatch):
    cfg=cfg_for(tmp_path,tmp_path/'registry');cfg.schedule.max_records_per_source=10
    (tmp_path/'registry').mkdir()
    for i in range(8):(tmp_path/'registry'/f'{i:064x}.json').write_text('{}')
    config=SimpleNamespace(data_dir=str(tmp_path),clock_offset_limit_ms=1000)
    monkeypatch.setattr(o,'runtime_check',lambda *args:(config,{'manifest_hash':'fixture'},{'ready':True}))
    monkeypatch.setattr(o,'native_config',lambda *args:(cfg,None))
    monkeypatch.setattr(o,'command_for',lambda phase,*args:[phase])
    called=[]
    monkeypatch.setattr(o,'bounded_child',lambda command,*args:called.append(command) or {'status':'CHILD_COMPLETED'})
    assert o.run_phase(tmp_path,'research')['status']=='CAPACITY_PAUSED_RESEARCH_ONLY'
    assert not called and a.paused(cfg,'research') and not a.paused(cfg,'observe')
    assert o.run_phase(tmp_path,'observe')['status']=='CHILD_COMPLETED'
    assert called==[['observe']]


def test_environment_check_never_reads_auth_content(tmp_path,monkeypatch):
    home=tmp_path/'home';auth=home/'.tradingagents/chatgpt-auth.json';auth.parent.mkdir(parents=True);auth.write_text('DO_NOT_READ')
    monkeypatch.setattr(Path,'home',lambda:home)
    original=Path.read_text
    def read(self,*args,**kwargs):
        if self==auth:raise AssertionError('Credential content access')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    value=d.host_check(d.Deployment())
    assert value['auth']['exists'] and not value['auth']['content_read']
    assert value['server_deployment_verified'] is False


def test_unsafe_runtime_does_not_create_linux_data_paths(tmp_path,monkeypatch):
    data=tmp_path/'must-not-exist'
    monkeypatch.setattr(o,'runtime_check',lambda *args:(SimpleNamespace(data_dir=str(data)),{},
                        {'ready':False,'issues':['LINUX_REQUIRED']}))
    assert o.run_phase(tmp_path,'paper')['status']=='BLOCKED_ENVIRONMENT_NO_WRITES'
    assert not data.exists()


def test_ntp_failure_does_not_invoke_children(tmp_path,monkeypatch):
    config=SimpleNamespace(data_dir=str(tmp_path),clock_offset_limit_ms=1000)
    monkeypatch.setattr(o,'runtime_check',lambda *args:(config,{'manifest_hash':'fixture'},
                        {'ready':False,'issues':['NTP_NOT_CONFIRMED']}))
    monkeypatch.setattr(o,'native_config',lambda *args:(cfg_for(tmp_path,tmp_path/'registry'),None))
    def forbidden(*args):raise AssertionError('No network or model calls while NTP invalid')
    monkeypatch.setattr(o,'bounded_child',forbidden)
    assert o.run_phase(tmp_path,'timely')['status']=='BLOCKED_ENVIRONMENT'


def test_probe_script_module_launch_needs_no_editable_install():
    import subprocess
    result=subprocess.run([sys.executable,'-m','scripts.probe_okx_llm','--help'],cwd=d.ROOT,
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0 and '--output' in result.stdout


def test_linux_lock_hash_and_installed_candidate_pins_are_consistent():
    import re,hashlib
    lock=d.ROOT/'docs/ubuntu_r14.requirements.lock';info=a.read(d.ROOT/'docs/ubuntu_r14_dependency_lock.json')
    assert hashlib.sha256(lock.read_bytes()).hexdigest()==info['lock_sha256']
    pins=dict(re.findall(r'^([A-Za-z0-9_.-]+)==([^\s;\\]+)',lock.read_text(),re.M))
    assert pins==d.dependency_candidate()['pins']
    assert not info['actual_linux_install_verified']
