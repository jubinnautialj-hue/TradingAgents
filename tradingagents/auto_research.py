"""R1.1 explicit watchlist + serial invocation of the existing OKX CLI/F1.

No scheduler, F2/F3 invocation, account API, execution or strategy changes.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import runpy
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import patch
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.vendors import okx
from tradingagents.forward_validation import Registry, atomic, digest, read
from tradingagents.research_budget import Meter, guards, read_meter

ROOT=Path(__file__).resolve().parents[1]
VERSION='auto-research-r11-v1'
CODE_HASH=digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in
                  (Path(__file__),Path(__file__).with_name('research_budget.py'))})


class Instrument(ResearchModel):
    instrument: str
    enabled: bool = True

    @field_validator('instrument')
    @classmethod
    def normalized(cls,value):
        return okx.validate_symbol(value.strip().upper())


class Enhancements(ResearchModel):
    orderflow: bool = False
    market_checks: bool = False
    price_depth: bool = False
    events: bool = True
    flow_minutes: int = Field(default=1,ge=1,le=60)
    flow_max_pages: int = Field(default=10,ge=1,le=100)
    depth_seconds: int = Field(default=0,ge=0,le=10)
    liquidation_seconds: int = Field(default=0,ge=0,le=10)


class Limits(ResearchModel):
    min_interval_seconds: int = Field(default=14400,ge=3600,le=604800)
    max_symbols: int = Field(default=3,ge=1,le=20)
    task_seconds: int = Field(default=900,ge=10,le=1800)
    batch_seconds: int = Field(default=2700,ge=10,le=7200)
    llm_per_task: int = Field(default=24,ge=1,le=100)
    llm_total: int = Field(default=72,ge=1,le=500)
    http_per_task: int = Field(default=240,ge=5,le=1000)
    http_total: int = Field(default=720,ge=5,le=5000)


class Watchlist(ResearchModel):
    version: Literal['auto-research-r11-v1'] = VERSION
    instruments: list[Instrument] = Field(min_length=1,max_length=20)
    forward_admission_policy: Literal["strict-v1", "forward-registration-revalidation-v1"] = "strict-v1"
    enhancements: Enhancements = Field(default_factory=Enhancements)
    limits: Limits = Field(default_factory=Limits)
    execution_enabled: Literal[False] = False

    @model_validator(mode='after')
    def unique(self):
        result={}
        for item in self.instruments:
            if item.instrument in result and result[item.instrument].enabled!=item.enabled:
                raise ValueError('Conflicting duplicate instrument enable flags')
            result.setdefault(item.instrument,item)
        self.instruments=list(result.values())
        return self

    def enabled(self):return [i.instrument for i in self.instruments if i.enabled]


def utc_ms():return time.time_ns()//1_000_000


def seal(path,value):atomic(path,{'payload':value,'sha256':digest(value)})


def unseal(path):
    obj=read(path)
    if obj.get('sha256')!=digest(obj['payload']):raise ValueError('R11 artifact fingerprint mismatch')
    return obj['payload']


def task_id(symbol,slot):return digest({'version':VERSION,'instrument':symbol,'schedule_slot_ms':slot})


class Store:
    def __init__(self,root,namespace='PRODUCTION'):
        if namespace not in {'PRODUCTION','FIXTURE'}:raise ValueError('Invalid batch namespace')
        self.root=Path(root)/namespace.lower();self.root.mkdir(parents=True,exist_ok=True)
        self.namespace=namespace
        (self.root/'tasks').mkdir(exist_ok=True)

    def directory(self,tid):
        if len(tid)!=64 or any(c not in '0123456789abcdef' for c in tid):raise ValueError('Invalid task id')
        return self.root/'tasks'/tid

    def events(self,tid):
        file=self.directory(tid)/'events.json'
        if not file.exists():return []
        events=read(file)
        for i,event in enumerate(events):
            if event['previous_hash']!=(events[i-1]['hash'] if i else None) or digest({k:v for k,v in event.items() if k!='hash'})!=event['hash']:
                raise ValueError('Task audit chain corrupted')
        return events

    def emit(self,tid,status,details=None):
        events=self.events(tid);payload={'status':status,'details':details or {}}
        if events and events[-1]['payload']==payload:return
        event={'recorded_ms':utc_ms(),'task_id':tid,'payload':payload,'previous_hash':events[-1]['hash'] if events else None,'execution_enabled':False}
        event['hash']=digest(event);atomic(self.directory(tid)/'events.json',[*events,event])

    def create(self,symbol,slot,config,grants):
        tid=task_id(symbol,slot);folder=self.directory(tid);folder.mkdir(exist_ok=True)
        header={'version':VERSION,'task_id':tid,'instrument':symbol,'schedule_slot_ms':slot,
                'namespace':self.namespace,'config':config.model_dump(),'config_hash':digest(config.model_dump()),
                'code_hash':CODE_HASH,'grants':grants,'created_ms':utc_ms(),
                'results_dir':str((self.root/'research').resolve()),'execution_enabled':False}
        header['git_commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        header['git_branch']=subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip()
        if (folder/'task.json').exists():
            previous=unseal(folder/'task.json')
            if previous['config_hash']!=header['config_hash'] or previous['code_hash']!=CODE_HASH:raise ValueError('Existing slot task has a different frozen configuration/version')
            return previous
        seal(folder/'task.json',header);self.emit(tid,'QUEUED');return header

    def tasks(self):
        paths=list((self.root/'tasks').glob('*/task.json'))
        if len(paths)>5000:raise ValueError('Task archive cap reached; explicit archival required')
        headers=[unseal(p) for p in paths]
        for h in headers:
            if h['namespace']!=self.namespace or Path(h['results_dir']).resolve()!=(self.root/'research').resolve():
                raise ValueError('Cross-namespace task archive')
            if h['task_id']!=task_id(h['instrument'],h['schedule_slot_ms']):raise ValueError('Task identity mismatch')
        return headers

    def state(self,header):
        events=self.events(header['task_id'])
        return events[-1]['payload']['status'] if events else 'QUEUED'


def command_for(header):
    cfg=Watchlist.model_validate(header['config']);e=cfg.enhancements
    args=['okx-research','--instrument',header['instrument'],'--interval','4h',
          '--multi-timeframe','--trade-plan','--taker-fee-bps','5','--forward-register',
          '--forward-admission-policy',cfg.forward_admission_policy,
          '--flow-minutes',str(e.flow_minutes),'--flow-max-pages',str(e.flow_max_pages),
          '--depth-seconds',str(e.depth_seconds),'--liquidation-seconds',str(e.liquidation_seconds)]
    args.extend('--'+name.replace('_','-') for name in ('orderflow','market_checks','price_depth','events') if getattr(e,name))
    return args


def registration_summary(record,folder):
    if record.execution_enabled or record.allow_new_risk:raise ValueError('Unexpected research execution/risk permission')
    return {'registration_id':record.registration_id,'research_id':record.research_id,
            'instrument':record.instrument,'origin':record.origin,'cutoff_ms':record.cutoff_ms,
            'completed_ms':record.completed_ms,'registered_ms':record.registered_ms,
            'workflow_status':record.workflow_status,'llm_status':record.llm_status,
            'action':record.action,'data_quality':record.data_quality,'plan_valid':record.plan_valid,
            'forward_admission_policy':record.config.get('forward_admission_policy','strict-v1'),
            'allow_new_risk':record.allow_new_risk,'execution_enabled':False,
            'failure_kind':record.failure.get('kind'),'snapshot_id':record.snapshot_id,
            'protocol_hash':record.protocol_hash,'report_html':str(Path(folder)/'complete_report.html'),
            'report_json':str(Path(folder)/'research_result.json'),
            'models':{k:record.config.get(k) for k in ('llm_provider','quick_think_llm','deep_think_llm')}}


def worker(folder,*,invoker=None):
    """Real invocation is the ORIGINAL CLI, with only worker-local observation hooks."""
    folder=Path(folder);header=unseal(folder/'task.json')
    if header['code_hash']!=CODE_HASH or header['task_id']!=folder.name:raise ValueError('Worker version/task mismatch')
    if header.get('config_hash')!=digest(header['config']):raise ValueError('Worker configuration fingerprint mismatch')
    cfg=Watchlist.model_validate(header['config'])
    if header['instrument'] not in cfg.enabled() or header['execution_enabled'] is not False:raise ValueError('Unconfigured instrument')
    grant=header['grants']
    if not (0<grant['llm']<=min(cfg.limits.llm_per_task,cfg.limits.llm_total)
            and 0<grant['http']<=min(cfg.limits.http_per_task,cfg.limits.http_total)
            and 0<grant['seconds']<=cfg.limits.task_seconds):raise ValueError('Worker budget outside frozen config')
    if header['namespace']=='FIXTURE' and invoker is None:raise ValueError('Fixture cannot invoke real model CLI')
    root=folder.parents[1];progress={};receipt=folder/'receipt.json'
    if root.name!=header['namespace'].lower() or Path(header['results_dir']).resolve()!=(root/'research').resolve():
        raise ValueError('Worker output/namespace mismatch')
    with locked(root/'worker',wait=False) as held:
        if not held:return {'worker_status':'BUSY','execution_enabled':False}
        meter=Meter(folder/'meter.json',grant['llm'],grant['http'],grant['seconds'])
        def update(**values):
            progress.update(values);seal(receipt,{**progress,'execution_enabled':False})
        begin=Registry.begin;register=Registry.register
        def begin_hook(registry,symbol,config):
            if symbol!=header['instrument']:raise ValueError('Worker instrument mismatch')
            if config.get('forward_admission_policy','strict-v1')!=cfg.forward_admission_policy:
                raise ValueError('Worker admission policy conflict')
            token=begin(registry,symbol,config)
            update(registry_root=str(registry.root.resolve()),intent_token=token,worker_status='RESEARCH_RUNNING')
            return token
        def register_hook(registry,directory,state,**kwargs):
            update(report_directory=str(Path(directory).resolve()),worker_status='REGISTRATION_PENDING')
            if header['namespace']=='FIXTURE':kwargs.update(token=None,origin='TEST_REPLAY')
            result=register(registry,directory,state,**kwargs)
            update(registration=registration_summary(result,directory),worker_status='REGISTERED')
            return result
        update(worker_status='STARTED',started_ms=utc_ms(),config_hash=header['config_hash'],
               forward_admission_policy=cfg.forward_admission_policy)
        exit_code=0
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{'TRADINGAGENTS_RESULTS_DIR':header['results_dir']}))
            # Some shared modules import defaults before the worker starts.
            # Rebuild through the existing factory AFTER setting only the local
            # result directory; the original CLI still builds its own config.
            from tradingagents import default_config
            stack.enter_context(patch.object(default_config,'DEFAULT_CONFIG',default_config.build_default_config()))
            stack.enter_context(patch.object(Registry,'begin',begin_hook))
            stack.enter_context(patch.object(Registry,'register',register_hook))
            stack.enter_context(guards(meter))
            try:
                if invoker is not None:invoker(command_for(header))
                else:
                    # Import CLI only AFTER its original environment preferences
                    # and meter are in place. No default_config/source mutation.
                    with patch.object(sys,'argv',['cli.main',*command_for(header)]):runpy.run_module('cli.main',run_name='__main__')
            except SystemExit as exc:exit_code=exc.code if isinstance(exc.code,int) else 1
            except Exception as exc:
                if invoker is not None:raise  # Isolated test harness; never production exception text
                from tradingagents.llm_clients.reliability import classify_failure
                failure=classify_failure(exc)
                exit_code=5;update(error_type=type(exc).__name__,failure_kind=failure.kind if failure else None)
        update(worker_status='FINISHED',exit_code=exit_code,finished_ms=utc_ms(),meter=meter.value)
        return progress


def run_worker(header,folder,seconds):
    command=[sys.executable,'-m','tradingagents.auto_research','_worker','--task',str(folder)]
    # No raw stdout/stderr logs: receipt + verified F1 archives are authoritative.
    process=subprocess.Popen(command,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:return {'returncode':process.wait(timeout=seconds),'timed_out':False}
    except subprocess.TimeoutExpired:
        process.kill();process.wait()
        return {'returncode':None,'timed_out':True}


def recover(store,header):
    """Only replay the existing F1 registration journal, NEVER the model."""
    folder=store.directory(header['task_id']);path=folder/'receipt.json'
    if not path.exists():return None
    value=unseal(path)
    expected=Path(header['results_dir']).resolve()/'forward_validation'/'f1'
    if value.get('registry_root') and Path(value['registry_root']).resolve()!=expected:
        raise ValueError('Receipt registry outside isolated research root')
    if value.get('registration'):
        reg=value['registration'];record=Registry(value['registry_root']).load(reg['registration_id'])
        if record.instrument!=header['instrument']:raise ValueError('Registration/task mismatch')
        value['registration']=registration_summary(record,value['report_directory']);return value
    token=value.get('intent_token');registry_root=value.get('registry_root')
    if token and registry_root and value.get('report_directory'):
        journal=Path(registry_root)/(token+'.journal.json')
        if journal.exists() and header['namespace']=='PRODUCTION':
            frozen=read(journal);registry=Registry(registry_root)
            record=registry.register(value['report_directory'],frozen['state'],token=token,completed_ms=frozen['completed_ms'])
            value['registration']=registration_summary(record,value['report_directory'])
            value['recovered_registration_only']=True;seal(path,value);return value
    return value


def charge(store,header):
    path=store.directory(header['task_id'])/'meter.json'
    receipt=store.directory(header['task_id'])/'receipt.json'
    finished=receipt.exists() and unseal(receipt).get('worker_status')=='FINISHED'
    if finished and path.exists():
        meter=read_meter(path)
        if meter['llm_calls']>header['grants']['llm'] or meter['http_dispatches']>header['grants']['http']:
            raise ValueError('Meter exceeds granted budget')
        return meter['llm_calls'],meter['http_dispatches']
    # Unknown interrupted work reserves its ENTIRE granted budget, not zero.
    if store.state(header)!='QUEUED':return header['grants']['llm'],header['grants']['http']
    return 0,0


def validate_pool(cfg,root,*,coverage=False,client=None):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    meter=Meter(root/('validation_'+uuid4().hex+'.meter.json'),0,cfg.limits.http_total,min(600,cfg.limits.batch_seconds))
    client=client or okx.OKXPublicClient();results=[]
    with guards(meter):
        for symbol in cfg.enabled()[:cfg.limits.max_symbols]:
            try:
                info=client.instrument(symbol)
                okx.validate_instrument(info,symbol)
                item={'instrument':symbol,'status':'VALID','fetched_at_ms':utc_ms(),
                      'specification':{k:info.get(k) for k in ('instId','instType','settleCcy','ctType','ctVal','ctMult','tickSz','lotSz','minSz','state')}}
                if coverage:
                    from copy import deepcopy
                    from tradingagents.default_config import DEFAULT_CONFIG
                    config=deepcopy(DEFAULT_CONFIG)
                    config.update(market_mode=okx.MODE,okx_interval='4h',okx_multi_timeframe=True,okx_derivatives_enabled=False)
                    bundle=okx.build_bundle(symbol,datetime.now(UTC).strftime('%Y-%m-%d'),config,client)
                    item['data_quality']=bundle['status'];item['cutoff_ms']=bundle.get('cutoff_ms')
                    item['timeframes']={key:{'status':child['status'],'bars':len(child.get('records',[])),
                                             'latest_close_ms':child.get('latest_close_ms')} for key,child in bundle.get('timeframes',{}).items()}
                    item['snapshot_id']=bundle.get('snapshot_id')
                results.append(item)
            except Exception as exc:results.append({'instrument':symbol,'status':'INVALID_OR_UNAVAILABLE','error_type':type(exc).__name__})
    return {'instruments':results,'meter':meter.value,'coverage_probe_only':coverage,'execution_enabled':False}


def validate_supervised(cfg,root,*,coverage=False):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    config_path=root/('validation_'+uuid4().hex+'.config.json');atomic(config_path,cfg.model_dump())
    command=[sys.executable,'-m','tradingagents.auto_research','_validate','--config',str(config_path),'--output',str(root)]
    if coverage:command.append('--coverage')
    try:
        result=subprocess.run(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                              text=True,timeout=min(600,cfg.limits.batch_seconds),check=False)
    except subprocess.TimeoutExpired:
        return {'instruments':[],'meter':{'http_dispatches':cfg.limits.http_total},'status':'VALIDATION_TIMEOUT','execution_enabled':False}
    try:
        value=json.loads(result.stdout)
        if result.returncode or 'meter' not in value:raise ValueError('No verified validation result')
        return value
    except (ValueError,KeyError):
        return {'instruments':[],'meter':{'http_dispatches':cfg.limits.http_total},'status':'VALIDATION_UNAVAILABLE','execution_enabled':False}


def summary(store,headers):
    rows=[]
    for header in headers:
        receipt_path=store.directory(header['task_id'])/'receipt.json'
        receipt=unseal(receipt_path) if receipt_path.exists() else {}
        if receipt.get('registration'):
            receipt=recover(store,header)  # Re-verify sealed F1 archive, no model call.
        meter_path=store.directory(header['task_id'])/'meter.json'
        meter=read_meter(meter_path) if meter_path.exists() else None
        registration=receipt.get('registration')
        research_status=('NOT_RESEARCHED' if not registration else
                         'REPLAY_ONLY' if registration['origin']!='PROSPECTIVE' else
                         'BUDGET_EXHAUSTED' if (registration.get('failure_kind') or '').startswith('R11_') else
                         'MODEL_FAILED' if registration['llm_status']=='LLM_UNAVAILABLE' else
                         'DATA_OR_WORKFLOW_FAILED' if registration['workflow_status']!='COMPLETED' else
                         'WAIT' if registration['action']=='WAIT' else
                         'VALID_CANDIDATE' if registration['plan_valid'] else 'PLAN_INVALID')
        rows.append({'instrument':header['instrument'],'task_id':header['task_id'],'schedule_slot_ms':header['schedule_slot_ms'],
                     'task_status':store.state(header),'research_status':research_status,'registration':registration,
                     'worker_error_type':receipt.get('error_type'),'meter':meter,'config_hash':header['config_hash']})
    return {'version':VERSION,'namespace':store.namespace,'execution_enabled':False,'tasks':rows,
            'counts':{status:sum(r['research_status']==status for r in rows) for status in sorted({r['research_status'] for r in rows})},
            'limitations':['Scheduling slot is NOT market cutoff; actual cutoff comes from F1/report',
                           'No F2/F3 invocation; ETH/SOL not admitted into BTC-only F3',
                           'Target holding horizon is descriptive; original 6-bar confirmation and 12-bar maximum paper holding unchanged',
                           'Logical model calls / HTTP dispatch caps do not establish a monetary billing cap',
                           'Unknown interrupted model work is not automatically retried']}


def save_summary(store,value):
    path=store.root/'runs';path.mkdir(exist_ok=True)
    name=digest(value);atomic(path/(name+'.json'),value)
    body=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    table='<table border="1"><tr><th>Instrument</th><th>Task</th><th>Research</th><th>Action</th><th>Report</th></tr>'
    for row in value['tasks']:
        reg=row['registration'] or {}
        table+='<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in (row['instrument'],row['task_status'],row['research_status'],reg.get('action'),reg.get('report_html')) )+'</tr>'
    (path/(name+'.html')).write_text('<!doctype html><meta charset="utf-8"><title>R1.1 batch research</title><h1>Research only — execution disabled</h1>'+table+'</table><details><summary>Audit JSON</summary><pre>'+html.escape(body)+'</pre></details>',encoding='utf-8')
    return str(path/(name+'.json'))


def run_batch(cfg,root,*,namespace='PRODUCTION',runner=None,validator=None):
    store=Store(root,namespace);start=time.monotonic();slot=utc_ms()//(cfg.limits.min_interval_seconds*1000)*(cfg.limits.min_interval_seconds*1000)
    with locked(store.root/'controller',wait=False) as held:
        if not held:return {'status':'BUSY','execution_enabled':False}
        with locked(store.root/'worker',wait=False) as worker_free:
            if not worker_free:return {'status':'ORPHAN_WORKER_BUSY','execution_enabled':False}
        previous=store.tasks()
        for h in previous:
            if store.state(h) in {'DISPATCHED','TIMEOUT','INTERRUPTED_UNKNOWN'}:
                value=recover(store,h)
                store.emit(h['task_id'],'REGISTERED' if value and value.get('registration') else 'INTERRUPTED_UNKNOWN')
        headers=[];notes=[];shared_block=None
        existing={h['task_id']:h for h in previous}
        scheduled=[h for h in previous if h['schedule_slot_ms']==slot]
        for h in scheduled:
            receipt_path=store.directory(h['task_id'])/'receipt.json'
            if receipt_path.exists():
                old=unseal(receipt_path)
                kind=(old.get('registration') or {}).get('failure_kind') or old.get('failure_kind')
                if kind in {'QUOTA_EXHAUSTED','AUTHENTICATION_OR_PERMISSION'}:shared_block=kind
        llm_used=sum(charge(store,h)[0] for h in scheduled);http_used=sum(charge(store,h)[1] for h in scheduled)
        validations=store.root/'validations';validations.mkdir(exist_ok=True)
        for path in validations.glob(str(slot)+'_*.json'):
            http_used+=unseal(path)['meter']['http_dispatches']
        validation=None
        for symbol in cfg.enabled()[:cfg.limits.max_symbols]:
            if shared_block:
                notes.append({'instrument':symbol,'reason':'SHARED_MODEL_RESTRICTION','failure_kind':shared_block});continue
            tid=task_id(symbol,slot)
            queued=None
            if tid in existing:
                h=existing[tid]
                if h['config_hash']!=digest(cfg.model_dump()) or h['code_hash']!=CODE_HASH:
                    headers.append(h);notes.append({'instrument':symbol,'reason':'FROZEN_SLOT_CONFIG_CONFLICT'});continue
                if store.state(h)=='QUEUED' and not (store.directory(tid)/'meter.json').exists():queued=h
                else:headers.append(h);continue
            recent=[h for h in previous if h['instrument']==symbol and h['task_id']!=tid and utc_ms()-h['created_ms']<cfg.limits.min_interval_seconds*1000]
            if recent:notes.append({'instrument':symbol,'reason':'MINIMUM_INTERVAL','previous_task_id':recent[-1]['task_id']});continue
            remaining=cfg.limits.batch_seconds-(time.monotonic()-start)
            if remaining<10 or llm_used>=cfg.limits.llm_total or http_used>=cfg.limits.http_total:
                notes.append({'instrument':symbol,'reason':'BATCH_BUDGET_EXHAUSTED'});continue
            if validation is None:
                cache=validations/(str(slot)+'_'+digest(cfg.model_dump())+'.json')
                if cache.exists():validation=unseal(cache)
                elif cfg.limits.http_total-http_used<5:
                    notes.append({'instrument':symbol,'reason':'CONTRACT_VALIDATION_BUDGET_EXHAUSTED'});continue
                else:
                    remaining_cfg=cfg.model_copy(deep=True);remaining_cfg.limits.http_total=cfg.limits.http_total-http_used
                    remaining_cfg.limits.batch_seconds=min(cfg.limits.batch_seconds,int(remaining))
                    # Reserve before spawning the public validation process too.
                    # A controller crash cannot make previous HTTP use vanish.
                    seal(cache,{'instruments':[],'meter':{'http_dispatches':remaining_cfg.limits.http_total},
                                'status':'VALIDATION_RESERVED_UNKNOWN','execution_enabled':False})
                    validation=(validator or validate_supervised)(remaining_cfg,store.root)
                    seal(cache,validation);http_used+=validation['meter']['http_dispatches']
            valid=next((i for i in validation['instruments'] if i['instrument']==symbol),None)
            if not valid or valid['status']!='VALID':
                notes.append({'instrument':symbol,'reason':'CONTRACT_INVALID_OR_UNAVAILABLE'});continue
            remaining=cfg.limits.batch_seconds-(time.monotonic()-start)
            if remaining<10 or cfg.limits.http_total-http_used<1:
                notes.append({'instrument':symbol,'reason':'BATCH_BUDGET_EXHAUSTED'});continue
            grants={'llm':min(cfg.limits.llm_per_task,cfg.limits.llm_total-llm_used),
                    'http':min(cfg.limits.http_per_task,cfg.limits.http_total-http_used),
                    'seconds':min(cfg.limits.task_seconds,remaining)}
            if queued:
                grants={k:min(grants[k],queued['grants'][k]) for k in grants}
                # Never silently change its frozen worker grants. If the current
                # remaining budget is smaller, leave the unsent task queued.
                if grants!=queued['grants']:
                    headers.append(queued);notes.append({'instrument':symbol,'reason':'QUEUED_GRANT_RESERVATION_UNAVAILABLE'});continue
                h=queued
            else:h=store.create(symbol,slot,cfg,grants)
            headers.append(h)
            store.emit(tid,'DISPATCHED',{'grants':grants})
            try:
                outcome=(runner or run_worker)(h,store.directory(tid),grants['seconds'])
                value=recover(store,h)
                status=('REGISTERED' if value and value.get('registration') else
                        'TIMEOUT' if outcome.get('timed_out') else 'FAILED_NO_REGISTRATION')
                store.emit(tid,status,{'returncode':outcome.get('returncode')})
            except Exception as exc:store.emit(tid,'INTERRUPTED_UNKNOWN',{'error_type':type(exc).__name__})
            spent_llm,spent_http=charge(store,h);llm_used+=spent_llm;http_used+=spent_http
            receipt_path=store.directory(tid)/'receipt.json'
            if receipt_path.exists():
                receipt=unseal(receipt_path)
                kind=(receipt.get('registration') or {}).get('failure_kind') or receipt.get('failure_kind')
                if kind in {'QUOTA_EXHAUSTED','AUTHENTICATION_OR_PERMISSION'}:shared_block=kind
        result=summary(store,headers)
        result.update(status='COMPLETED',schedule_slot_ms=slot,notes=notes,validation=validation,
                      budget={'llm_used_or_reserved':llm_used,'http_used_or_reserved':http_used,'elapsed_seconds':time.monotonic()-start})
        result['summary_json']=save_summary(store,result)
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run','validate','status','_worker','_validate'])
    parser.add_argument('--config',type=Path,default=ROOT/'docs/auto_research_r11.json')
    parser.add_argument('--output',type=Path,default=Path.home()/'.tradingagents/logs/auto_research/r11')
    parser.add_argument('--coverage',action='store_true')
    parser.add_argument('--task',type=Path)
    args=parser.parse_args()
    try:
        if args.command=='_worker':
            if args.task is None:raise ValueError('Task required')
            worker(args.task);return
        cfg=Watchlist.model_validate(read(args.config))
        if args.command=='run':value=run_batch(cfg,args.output)
        elif args.command in {'validate','_validate'}:
            value=(validate_supervised if args.command=='validate' else validate_pool)(cfg,args.output,coverage=args.coverage)
            seal(args.output/('public_validation_'+str(utc_ms())+'.json'),value)
        else:
            store=Store(args.output);value=summary(store,store.tasks())
        print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
    except (ValueError,OSError,KeyError) as exc:
        print(json.dumps({'status':'R11_UNAVAILABLE','error_type':type(exc).__name__,'execution_enabled':False}));raise SystemExit(2)


if __name__=='__main__':main()
