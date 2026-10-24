"""R1.2 bounded orchestration. Original R11/F1/F2/F3 implementations unchanged."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents.dataflows.files import locked
from tradingagents.forward_validation import Registry, ForwardProtocol, PROTOCOL, atomic, digest, read
from tradingagents import auto_research as r11, forward_tracking as f2, forward_paper as f3
from tradingagents import observation_eligibility as observation

ROOT=Path(__file__).resolve().parents[1]
VERSION='automation-r12-v1'
PHASES=('research','observe','paper','timely')


class ArchiveSource(ResearchModel):
    name: str = Field(pattern=r'^[a-z0-9-]{1,32}$')
    registry: str | None = None  # None means the canonical R11 registry
    optional: bool = True


class Schedule(ResearchModel):
    research_seconds: Literal[3600,7200,10800,14400,21600,28800,43200,86400] = 14400
    observe_seconds: Literal[60,120,180,300] = 60
    paper_seconds: Literal[60,300,600,900,1800] = 300
    observe_timeout: int = Field(default=50,ge=10,le=55)
    paper_timeout: int = Field(default=50,ge=10,le=55)
    timely_timeout: int = Field(default=120,ge=90,le=180)
    refresh_lead_ms: Literal[40000] = 40000
    warm_lead_ms: Literal[80000] = 80000
    paper_reserved_lead_ms: Literal[120000] = 120000
    paper_reserved_tail_ms: Literal[20000] = 20000
    logical_gets_per_source: int = Field(default=12,ge=2,le=40)
    timely_http_cap: int = Field(default=60,ge=12,le=100)
    max_records_per_source: int = Field(default=1000,ge=1,le=2000)
    max_due: int = Field(default=3,ge=1,le=5)


class AutomationConfig(ResearchModel):
    version: Literal['automation-r12-v1'] = VERSION
    watchlist: str = 'docs/auto_research_r11.json'
    batch_root: str = '~/.tradingagents/logs/auto_research/r11'
    state_root: str = '~/.tradingagents/logs/automation/r12'
    sources: list[ArchiveSource] = Field(min_length=1,max_length=4)
    schedule: Schedule = Field(default_factory=Schedule)
    execution_enabled: Literal[False] = False
    simulation_admitted: Literal[False] = False
    allow_new_risk: Literal[False] = False

    @model_validator(mode='after')
    def unique(self):
        if len({s.name for s in self.sources})!=len(self.sources):raise ValueError('Duplicate archive source name')
        return self


def expand(value):
    path=Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else ROOT/path).resolve()


def paths(cfg):
    state=expand(cfg.state_root);batch=expand(cfg.batch_root)
    if state==batch or state in batch.parents or batch in state.parents:raise ValueError('R12 state and R11 batch must be isolated')
    result=[];seen={}
    for source in cfg.sources:
        registry=expand(source.registry) if source.registry else batch/'production/research/forward_validation/f1'
        if registry==state or registry in state.parents or state in registry.parents:raise ValueError('R12 state overlaps sealed F1 registry')
        key=os.path.normcase(str(registry.resolve()))
        alias=seen.get(key);seen.setdefault(key,source.name)
        result.append({'name':source.name,'registry':str(registry),'optional':source.optional,'alias_of':alias,
                       'f2':str(state/'f2'/source.name),'f3':str(state/'f3'/source.name)})
    return result


def readonly_registry(path):
    """Use native verification without Registry.__init__ creating lock/protocol files."""
    path=Path(path)
    expected=ForwardProtocol.model_validate(read(PROTOCOL)).model_dump()
    if read(path/'protocol-forward-f1-v1.json')!=expected:raise ValueError('F1 protocol mismatch')
    obj=object.__new__(Registry);obj.root=path
    return obj


def readonly_ledger(path):
    obj=object.__new__(f3.Ledger)
    obj.root=Path(path)/'production';obj.path=obj.root/'ledger.json';obj.namespace='PRODUCTION'
    return obj


def readonly_tracking(path,record):
    file=Path(path)/'production'/(record.registration_id+'.events.json')
    if not file.exists():return []
    saved=read(file)
    if saved.get('version')!=f2.VERSION:raise f2.LegacyTrackingChain('Legacy chain paused; no migration')
    grace=saved['policy']['delivery_grace_seconds']
    f2.ScanLimits(delivery_grace_seconds=grace)
    obj=object.__new__(f2.EventStore);obj.root=file.parent;obj.namespace='PRODUCTION'
    obj.policy={'version':f2.VERSION,'delivery_grace_seconds':grace,'observation_policy_hash':observation.POLICY_HASH}
    # Static qualification first, then the unchanged native existing-chain loader.
    candidate=f2.candidate_from_record(record,existing_expiry=True)
    return obj.load(candidate)


def discover(cfg,*,persist=False):
    """Read-only source index. Quarantine a whole duplicate/invalid source because
    native scans operate on whole registries; never copy a filtered fake registry.
    """
    state=expand(cfg.state_root);prior={}
    old=state/'index.json'
    if old.exists():prior=r11.unseal(old)
    result={'version':VERSION,'sources':[],'records':{},'errors':[], 'execution_enabled':False}
    conflicts=set()
    for source in paths(cfg):
        item={**source,'status':'READY','registrations':[],'binding_hash':digest({k:source[k] for k in ('name','registry','f2','f3')})}
        result['sources'].append(item)
        if source['alias_of']:
            item['status']='DUPLICATE_PATH_ALIAS';continue
        binding=state/'bindings'/(source['name']+'.json')
        if binding.exists() and r11.unseal(binding)['binding_hash']!=item['binding_hash']:
            item['status']='SOURCE_BINDING_CONFLICT';continue
        root=Path(source['registry'])
        if not root.is_dir():
            item['status']=('INVALID_SOURCE_PATH' if root.exists() else 'MISSING_BOUND_SOURCE' if binding.exists() else
                            'NOT_YET_CREATED' if source['optional'] else 'MISSING_REQUIRED_SOURCE')
            continue
        try:
            registry=readonly_registry(root)
            candidates=sorted(p for p in root.glob('*.json') if re.fullmatch(r'[0-9a-f]{64}',p.stem))
            if len(candidates)>cfg.schedule.max_records_per_source:raise ValueError('Registry index cap exceeded')
            for path in candidates:
                record=registry.load(path.stem);registry.events(path.stem)
                item['registrations'].append(record.registration_id)
                row={'registration_id':record.registration_id,'source':source['name'],'content_hash':record.content_hash,
                     'research_id':record.research_id,'origin':record.origin,'instrument':record.instrument,
                     'workflow_status':record.workflow_status,'action':record.action,'data_quality':record.data_quality,
                     'plan_valid':record.plan_valid,'registered_ms':record.registered_ms,'cutoff_ms':record.cutoff_ms,
                     'report_directory':record.report_directory,'protocol_hash':record.protocol_hash}
                owner=prior.get('records',{}).get(record.registration_id)
                if owner and (owner['source']!=source['name'] or owner['content_hash']!=record.content_hash):
                    result['records'].setdefault(record.registration_id,owner)
                    if owner['content_hash']!=record.content_hash:
                        conflicts.update([owner['source'],source['name']]);item['status']='REGISTRATION_ID_CONFLICT'
                    else:item['status']='DUPLICATE_REGISTRY_QUARANTINED'
                    continue
                if record.registration_id in result['records']:
                    previous=result['records'][record.registration_id]
                    if previous['content_hash']!=record.content_hash:
                        item['status']='REGISTRATION_ID_CONFLICT'
                        for earlier in result['sources']:
                            if earlier['name']==previous['source']:earlier['status']='REGISTRATION_ID_CONFLICT'
                    elif previous['source']!=source['name']:item['status']='DUPLICATE_REGISTRY_QUARANTINED'
                else:result['records'][record.registration_id]=row
            old_ids={rid for rid,r in prior.get('records',{}).items() if r['source']==source['name']}
            missing=old_ids-set(item['registrations'])
            if missing:item.update(status='MISSING_SEALED_RECORDS',missing_registration_ids=sorted(missing))
        except (ValueError,OSError,KeyError,TypeError) as exc:
            item.update(status='INVALID_SOURCE',error_type=type(exc).__name__)
        if persist and item['status']=='READY':
            binding.parent.mkdir(parents=True,exist_ok=True)
            if not binding.exists():r11.seal(binding,{'binding_hash':item['binding_hash'],'source':source})
    for item in result['sources']:
        if item['name'] in conflicts:item['status']='REGISTRATION_ID_CONFLICT'
        if item['status'] not in {'READY','NOT_YET_CREATED','DUPLICATE_PATH_ALIAS'}:
            result['errors'].append({'source':item['name'],'status':item['status']})
    # Retain missing identities in the index so a second scan cannot silently
    # forget a previous disappearance and resume the source.
    for rid,row in prior.get('records',{}).items():result['records'].setdefault(rid,row)
    if persist:
        state.mkdir(parents=True,exist_ok=True)
        with locked(state/'index'):
            if old.exists():
                for rid,row in r11.unseal(old).get('records',{}).items():result['records'].setdefault(rid,row)
            r11.seal(old,result)
    return result


def paused(cfg,phase):
    file=expand(cfg.state_root)/'control.json'
    if not file.exists():return False
    value=r11.unseal(file)
    return value.get('all',False) or value.get(phase,False)


def set_pause(cfg,phase,value):
    root=expand(cfg.state_root);root.mkdir(parents=True,exist_ok=True)
    with locked(root/'control'):
        file=root/'control.json';old=r11.unseal(file) if file.exists() else {}
        old[phase]=value;r11.seal(file,old)
    return {'status':'PAUSED' if value else 'RESUMED','phase':phase,'execution_enabled':False}


def reserved(cfg,now):
    hour=f3.HOUR;previous=now//hour*hour;upcoming=previous+hour
    return (now-previous<=cfg.schedule.paper_reserved_tail_ms or upcoming-now<=cfg.schedule.paper_reserved_lead_ms)


def audit(cfg,job,kind,payload):
    if not re.fullmatch(r'(?:[0-9a-f]{32}|[0-9a-f]{64})',job):raise ValueError('Invalid scheduler job id')
    folder=expand(cfg.state_root)/'jobs'/job;folder.mkdir(parents=True,exist_ok=True)
    file=folder/'events.json'
    with locked(folder/'audit'):
        events=read(file) if file.exists() else []
        for i,e in enumerate(events):
            if e['hash']!=digest({k:v for k,v in e.items() if k!='hash'}) or e['previous_hash']!=(events[i-1]['hash'] if i else None):raise ValueError('R12 audit corrupted')
        event={'kind':kind,'recorded_ms':r11.utc_ms(),'payload':payload,'previous_hash':events[-1]['hash'] if events else None,'execution_enabled':False}
        event['hash']=digest(event);atomic(file,[*events,event])


def limits_for(cfg,seconds,sources):
    return f2.ScanLimits(max_seconds=max(5,min(600,int(seconds))),max_requests=cfg.schedule.logical_gets_per_source,
                         max_records=cfg.schedule.max_records_per_source,max_candidates=20,max_pages=3)


def execute_phase(cfg,phase):
    if paused(cfg,phase):return {'status':'PAUSED','phase':phase,'execution_enabled':False}
    root=expand(cfg.state_root);root.mkdir(parents=True,exist_ok=True)
    lock_name='paper' if phase in {'paper','timely'} else phase
    # Timely can wait briefly for a prior bounded routine scan, but routine work
    # never waits for timely and cannot enter the reserved opening interval.
    deadline=time.monotonic()+cfg.schedule.timely_timeout
    while True:
        with locked(root/(lock_name+'.phase'),wait=False) as held:
            if held:
                if phase=='research':
                    watch=r11.Watchlist.model_validate(read(expand(cfg.watchlist)))
                    if watch.limits.min_interval_seconds!=cfg.schedule.research_seconds:raise ValueError('R11/R12 research frequency mismatch')
                    value=r11.run_batch(watch,expand(cfg.batch_root))
                    for row in value.get('tasks',[]):
                        kind=(row.get('registration') or {}).get('failure_kind')
                        if not kind and row.get('task_id'):
                            receipt=expand(cfg.batch_root)/'production/tasks'/row['task_id']/'receipt.json'
                            if receipt.exists():kind=r11.unseal(receipt).get('failure_kind')
                        if kind in {'QUOTA_EXHAUSTED','AUTHENTICATION_OR_PERMISSION'}:
                            # Automation must not keep spending at each new slot
                            # against an explicitly blocked provider account.
                            set_pause(cfg,'research',True)
                            value['service_restriction']={'kind':kind,'requires_manual_resume':True}
                            break
                    return value
                if phase=='paper' and reserved(cfg,r11.utc_ms()):return {'status':'TIMELY_RESERVED','execution_enabled':False}
                index=discover(cfg,persist=True)
                ready=[s for s in index['sources'] if s['status']=='READY']
                if phase=='timely':
                    from tradingagents.paper_timing import warm_window
                    return warm_window(cfg,ready)
                seconds=cfg.schedule.observe_timeout if phase=='observe' else cfg.schedule.paper_timeout
                results=[];started=time.monotonic()
                for source in ready:
                    remaining=seconds-(time.monotonic()-started)
                    if remaining<5:
                        results.append({'source':source['name'],'status':'PHASE_TIME_BUDGET'});continue
                    try:
                        native_limits=limits_for(cfg,min(remaining,seconds/max(1,len(ready))),len(ready))
                        if phase=='observe':value=f2.scan(source['registry'],source['f2'],native_limits)
                        elif not Path(source['f2']).is_dir():value={'status':'WAITING_FOR_F2'}
                        else:value=f3.scan(source['registry'],source['f2'],source['f3'],native_limits)
                        # Persist only known status/counters and native report refs,
                        # never provider request/response text or sensitive errors.
                        results.append({'source':source['name'],'status':value.get('status','COMPLETED'),
                                        'errors':len(value.get('errors',[])),'logical_get_calls':value.get('logical_get_calls'),
                                        'research':value.get('research'),'paper':value.get('paper'),
                                        'eligible_candidates':value.get('eligible_candidates')})
                    except (ValueError,OSError,KeyError,f2.BudgetExhausted,f2.okx.VendorUnavailableError) as exc:
                        results.append({'source':source['name'],'status':'SOURCE_UNAVAILABLE','error_type':type(exc).__name__})
                return {'status':'COMPLETED','phase':phase,'results':results,'index_errors':index['errors'],'execution_enabled':False}
        if phase!='timely' or time.monotonic()>=deadline or paused(cfg,phase):
            return {'status':'BUSY_OR_PAUSED','phase':phase,'execution_enabled':False}
        time.sleep(.25)


def kill_owned(process):
    if process.poll() is not None:return
    if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
    else:os.killpg(process.pid,signal.SIGKILL)
    try:process.wait(timeout=5)
    except subprocess.TimeoutExpired:process.kill();process.wait()


def dispatch(cfg,phase,*,dry_run=False,wait=True):
    if phase not in PHASES:raise ValueError('Unknown phase')
    now=r11.utc_ms();slot=now//(cfg.schedule.research_seconds*1000)*(cfg.schedule.research_seconds*1000)
    period,offset={'research':(cfg.schedule.research_seconds,20),'observe':(cfg.schedule.observe_seconds,2),
                   'paper':(cfg.schedule.paper_seconds,20),'timely':(3600,3520)}[phase]
    grid=now//(period*1000)*(period*1000)+offset*1000
    if grid>now:grid-=period*1000
    job=digest({'phase':phase,'slot':slot}) if phase=='research' else uuid4().hex
    root=expand(cfg.state_root)
    if dry_run:return {'status':'DRY_RUN','job_id':job,'phase':phase,'paths':paths(cfg),'will_invoke_model':phase=='research','execution_enabled':False}
    if paused(cfg,phase):return {'status':'PAUSED','phase':phase,'execution_enabled':False}
    folder=root/'jobs'/job;folder.mkdir(parents=True,exist_ok=True)
    with locked(folder/'launch',wait=False) as held:
        if not held:return {'status':'BUSY','execution_enabled':False}
        if (folder/'dispatch.json').exists():return {'status':'ALREADY_DISPATCHED','job_id':job,'execution_enabled':False}
        # Never silently reset a dispatched/unknown research job within its slot.
        r11.seal(folder/'config.json',cfg.model_dump())
        config_path=folder/'effective_config.json';atomic(config_path,cfg.model_dump())
        if phase=='research':
            watch=r11.Watchlist.model_validate(read(expand(cfg.watchlist)))
            watch_path=folder/'watchlist.json';atomic(watch_path,watch.model_dump())
            effective=cfg.model_copy(update={'watchlist':str(watch_path)})
            atomic(config_path,effective.model_dump());timeout=watch.limits.batch_seconds+30
        else:timeout=getattr(cfg.schedule,{'observe':'observe_timeout','paper':'paper_timeout','timely':'timely_timeout'}[phase])+10
        command=[sys.executable,'-m','tradingagents.automation','_worker','--phase',phase,'--config',str(config_path),'--job',job]
        audit(cfg,job,'DISPATCHED',{'phase':phase,'scheduled_ms':now,'configured_grid_ms':grid,'configured_grid_lag_ms':now-grid,
                                  'grid_scope':'Configured UTC cadence reference; manual calls are not proof of OS trigger lateness',
                                  'config_hash':digest(cfg.model_dump()),'timeout_seconds':timeout})
        r11.seal(folder/'dispatch.json',{'phase':phase,'scheduled_ms':now,'timeout_seconds':timeout,'execution_enabled':False})
        output=folder/'stdout.tmp'
        with output.open('w',encoding='utf-8') as out:
            process=subprocess.Popen(command,cwd=ROOT,stdout=out,stderr=subprocess.DEVNULL,start_new_session=os.name!='nt')
        started=r11.utc_ms();audit(cfg,job,'STARTED',{'phase':phase,'started_ms':started,'dispatch_lag_ms':started-now,'pid':process.pid})
    handle={'process':process,'folder':folder,'job':job,'timeout':timeout,'start':time.monotonic(),'phase':phase}
    return finish(cfg,handle) if wait else handle


def finish(cfg,handle):
    process=handle['process'];timed_out=False
    try:code=process.wait(timeout=max(.01,handle['timeout']-(time.monotonic()-handle['start'])))
    except subprocess.TimeoutExpired:
        kill_owned(process);code=None;timed_out=True
    folder=handle['folder'];value={};output=folder/'stdout.tmp'
    if output.exists():
        if output.stat().st_size<=2*1024*1024:
            try:value=json.loads(output.read_text(encoding='utf-8'))
            except (ValueError,UnicodeError):value={'status':'INVALID_WORKER_OUTPUT'}
        else:value={'status':'WORKER_OUTPUT_TOO_LARGE'}
        output.unlink()  # Only our own temporary stdout, never research artifacts.
    if not value and (folder/'worker_result.json').exists():value=r11.unseal(folder/'worker_result.json')
    invalid=value.get('status') in {'INVALID_WORKER_OUTPUT','WORKER_OUTPUT_TOO_LARGE','R12_UNAVAILABLE','WORKER_COMPONENT_ERROR'}
    result={'status':'TIMEOUT' if timed_out else ('COMPLETED' if code==0 and not invalid else 'WORKER_FAILED'),
            'job_id':handle['job'],'phase':handle['phase'],'returncode':code,'result':value,'execution_enabled':False}
    r11.seal(folder/'result.json',result)
    audit(cfg,handle['job'],result['status'],{'phase':handle['phase'],'returncode':code,'finished_ms':r11.utc_ms()})
    return result


def status(cfg):
    """Strictly read-only: no directory creation, locks, network or model calls."""
    index=discover(cfg);root=expand(cfg.state_root)
    counts={'f1_completed':0,'f1_research_failed':0,'f1_wait':0,'f1_candidate':0,'excluded_origins':0,
            'f2_pending':0,'f2_triggered':0,'f2_expired':0,'f2_invalidated':0,'f2_paused':0,
            'f3_scheduled':0,'f3_open':0,'f3_settled':0,'f3_missed':0,'data_quality_issues':0,
            'f3_exit_pending':0,'f3_unresolved':0,'f3_cancelled':0,'f3_conditions_awaiting_review':0,
            'f3_unsupported_instrument':0,'f2_unobserved_expired':0,'f2_admin_excluded':0,'model_failures':0}
    latest={};issues=[]
    for source in index['sources']:
        if source['status']!='READY':continue
        registry=readonly_registry(source['registry'])
        triggered=[]
        for rid in source['registrations']:
            record=registry.load(rid)
            if record.origin!='PROSPECTIVE':counts['excluded_origins']+=1;continue
            counts['f1_completed']+=record.workflow_status=='COMPLETED'
            counts['f1_research_failed']+=record.workflow_status!='COMPLETED'
            counts['model_failures']+=record.llm_status=='LLM_UNAVAILABLE'
            counts['f1_wait']+=record.workflow_status=='COMPLETED' and record.action=='WAIT'
            counts['f1_candidate']+=record.workflow_status=='COMPLETED' and record.plan_valid
            counts['data_quality_issues']+=record.data_quality!='VALID'
            if record.instrument not in latest or latest[record.instrument]['registered_ms']<record.registered_ms:
                latest[record.instrument]={**index['records'][rid],'outcome':'RESEARCH_FAILED' if record.workflow_status!='COMPLETED' else record.action}
            if record.simulation_pool_eligible:
                if registry.tracking_status(rid)!='QUEUED':counts['f2_admin_excluded']+=1;continue
                try:
                    events=readonly_tracking(source['f2'],record)
                    state=f2.state_of(events)
                    if not events and record.expires_ms is not None and record.expires_ms<=r11.utc_ms():
                        counts['f2_unobserved_expired']+=1;continue
                    label={'ENTRY_ELIGIBLE':'f2_triggered','EXPIRED':'f2_expired','INVALIDATED':'f2_invalidated'}.get(state,'f2_pending')
                    counts[label]+=1;counts['f2_paused']+=bool(events and events[-1]['kind']=='PAUSED')
                    if state=='ENTRY_ELIGIBLE':triggered.append((rid,record.instrument))
                except (ValueError,OSError,KeyError) as exc:issues.append({'source':source['name'],'stage':'F2','error_type':type(exc).__name__})
        try:
            ledger=readonly_ledger(source['f3']);events=ledger.load()
            states=ledger.states(events)
            for state in states.values():
                key={'SCHEDULED':'f3_scheduled','OPEN':'f3_open','SETTLED':'f3_settled','MISSED':'f3_missed',
                     'EXIT_PENDING':'f3_exit_pending','UNRESOLVED':'f3_unresolved','CANCELLED':'f3_cancelled'}.get(state.status)
                if key:counts[key]+=1
            for rid,instrument in triggered:
                if instrument!='BTC-USDT-SWAP':counts['f3_unsupported_instrument']+=1
                elif rid not in states:counts['f3_conditions_awaiting_review']+=1
        except (ValueError,OSError,KeyError) as exc:issues.append({'source':source['name'],'stage':'F3','error_type':type(exc).__name__})
    jobs=[]
    if (root/'jobs').is_dir():
        for file in sorted((root/'jobs').glob('*/events.json'),key=lambda p:(p.stat().st_mtime_ns,p.parent.name))[-200:]:
            events=read(file)
            for i,event in enumerate(events):
                if event['hash']!=digest({k:v for k,v in event.items() if k!='hash'}) or event['previous_hash']!=(events[i-1]['hash'] if i else None):raise ValueError('Scheduler audit hash mismatch')
            if events:jobs.append({'job_id':file.parent.name,'last_event':events[-1],
                                  'completion_state':'INTERRUPTED_OR_RUNNING_UNKNOWN' if events[-1]['kind'] in {'STARTED','WORKER_READY','DISPATCHED'} else events[-1]['kind']})
    monitor=r11_monitor(cfg,index)
    return {'version':VERSION,'status':'READ_ONLY','counts':counts,'latest_research':latest,'index':index,'r11_tasks':monitor,
            'statistics_complete':not (index['errors'] or issues or monitor['errors']),
            'issues':issues,'recent_jobs':jobs,'paused':{phase:paused(cfg,phase) for phase in PHASES},
            'automatic_research_retries':0,'execution_enabled':False,'simulation_admitted':False,'allow_new_risk':False}


def r11_monitor(cfg,index):
    roots={expand(cfg.batch_root)/'production'}
    for source in index['sources']:
        registry=Path(source['registry'])
        if registry.parts[-3:]==('research','forward_validation','f1'):roots.add(registry.parents[2])
    rows=[];errors=[]
    verified_ids={rid for source in index['sources'] if source['status']=='READY' for rid in source['registrations']}
    for root in sorted(roots,key=lambda path:os.path.normcase(str(path))):
        if not (root/'tasks').is_dir():continue
        store=object.__new__(r11.Store);store.root=root;store.namespace='PRODUCTION'
        try:
            for header in sorted(store.tasks(),key=lambda h:(h['created_ms'],h['task_id'])):
                folder=store.directory(header['task_id']);receipt=r11.unseal(folder/'receipt.json') if (folder/'receipt.json').exists() else {}
                reg=receipt.get('registration');events=store.events(header['task_id'])
                meter=r11.read_meter(folder/'meter.json') if (folder/'meter.json').exists() else None
                rows.append({'task_id':header['task_id'],'instrument':header['instrument'],'task_status':store.state(header),
                             'started_ms':receipt.get('started_ms'),'finished_ms':receipt.get('finished_ms'),
                             'registration_verified':bool(reg and reg['registration_id'] in verified_ids),
                             'registration_id':reg.get('registration_id') if reg else None,
                             'origin':reg.get('origin') if reg else None,'workflow_status':reg.get('workflow_status') if reg else None,
                             'sample_scope':'FORWARD' if reg and reg['origin']=='PROSPECTIVE' else 'NOT_A_VERIFIED_FORWARD_SAMPLE',
                             'outcome':'RESEARCH_FAILED' if (reg and reg['workflow_status']!='COMPLETED') or
                                 (not reg and (receipt.get('worker_status')=='FINISHED' or store.state(header) in {'TIMEOUT','INTERRUPTED_UNKNOWN','FAILED_NO_REGISTRATION'})) else (reg or {}).get('action'),
                             'timeouts':sum(e['payload']['status']=='TIMEOUT' for e in events),
                             'recoveries':int(receipt.get('recovered_registration_only',False)),'usage':meter})
        except (ValueError,OSError,KeyError) as exc:errors.append({'stage':'R11_TASK_ARCHIVE','error_type':type(exc).__name__})
    return {'rows':rows,'errors':errors,'f1_registration_verified':sum(r['registration_verified'] for r in rows),
            'f1_pending':sum(not r['registration_verified'] and r['task_status'] in {'QUEUED','DISPATCHED'} for r in rows),
            'f1_registration_missing_or_failed':sum(not r['registration_verified'] and r['task_status'] not in {'QUEUED','DISPATCHED'} for r in rows),
            'recorded_llm_calls':sum((r['usage'] or {}).get('llm_calls',0) for r in rows),
            'scope':'Previously accumulated R11 usage, NOT necessarily calls made by this R12 run'}


def cycle(cfg,include_research=False):
    """One bounded manual coordination run, not a daemon."""
    research=dispatch(cfg,'research',wait=False) if include_research else None
    active=isinstance(research,dict) and 'process' in research
    results=[] if research is None or active else [research]
    next_observe=next_paper=next_timely=0.
    try:
        while True:
            now=time.monotonic()
            for phase,due,interval in [('observe',next_observe,cfg.schedule.observe_seconds),('paper',next_paper,cfg.schedule.paper_seconds),('timely',next_timely,60)]:
                if now>=due:
                    results.append(dispatch(cfg,phase))
                    if phase=='observe':next_observe=time.monotonic()+interval
                    elif phase=='paper':next_paper=time.monotonic()+interval
                    else:next_timely=time.monotonic()+interval
            if not active or research['process'].poll() is not None:break
            if time.monotonic()-research['start']>research['timeout']:break
            time.sleep(.5)
        if active:
            results.append(finish(cfg,research));results.append(dispatch(cfg,'observe'));results.append(dispatch(cfg,'paper'))
    finally:
        if active and research['process'].poll() is None:kill_owned(research['process'])
    return {'status':'CYCLE_COMPLETE','jobs':results,'execution_enabled':False}


def worker_entry(cfg,phase,job):
    audit(cfg,job,'WORKER_READY',{'phase':phase,'actual_worker_started_ms':r11.utc_ms()})
    try:result=execute_phase(cfg,phase)
    except Exception as exc:
        result={'status':'WORKER_COMPONENT_ERROR','phase':phase,'error_type':type(exc).__name__,'execution_enabled':False}
    # Survives controller death: child completion is recorded independently.
    folder=expand(cfg.state_root)/'jobs'/job
    r11.seal(folder/'worker_result.json',result)
    audit(cfg,job,'WORKER_FINISHED',{'phase':phase,'native_status':result.get('status'),'finished_ms':r11.utc_ms()})
    return result


def report(cfg):
    value=status(cfg);folder=expand(cfg.state_root)/'reports';folder.mkdir(parents=True,exist_ok=True)
    name=digest(value);atomic(folder/(name+'.json'),value)
    body=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    (folder/(name+'.html')).write_text('<!doctype html><meta charset="utf-8"><title>R1.2 research and paper monitoring</title><h1>R1.2 — execution disabled</h1><pre>'+html.escape(body)+'</pre>',encoding='utf-8')
    return {'status':'REPORT_SAVED','json':str(folder/(name+'.json')),'html':str(folder/(name+'.html')),'execution_enabled':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=[*PHASES,'cycle','status','report','dry-run','pause','resume','export','probe','_probe','_worker'])
    parser.add_argument('--config',type=Path,default=ROOT/'docs/automation_r12.json')
    parser.add_argument('--phase',choices=[*PHASES,'all'],default='all')
    parser.add_argument('--job')
    parser.add_argument('--include-research',action='store_true')
    parser.add_argument('--expected-config-hash')
    parser.add_argument('--export-dir',type=Path)
    args=parser.parse_args()
    try:
        cfg=AutomationConfig.model_validate(read(args.config));paths(cfg)
        if args.expected_config_hash and args.expected_config_hash!=digest(cfg.model_dump()):raise ValueError('Schedule config changed; export/review again')
        if args.command=='_worker':
            if args.phase not in PHASES or not args.job:raise ValueError('Invalid worker')
            result=worker_entry(cfg,args.phase,args.job)
        elif args.command=='status':result=status(cfg)
        elif args.command=='report':result=report(cfg)
        elif args.command=='dry-run':result={'jobs':[dispatch(cfg,p,dry_run=True) for p in PHASES],'execution_enabled':False}
        elif args.command in {'pause','resume'}:result=set_pause(cfg,args.phase,args.command=='pause')
        elif args.command=='cycle':result=cycle(cfg,args.include_research)
        elif args.command in {'export','probe','_probe'}:
            from tradingagents.automation_schedule import export,public_probe
            if args.command=='export':result=export(cfg,args.config,args.export_dir)
            elif args.command=='_probe':result=public_probe(cfg)
            else:
                process=subprocess.Popen([sys.executable,'-m','tradingagents.automation','_probe','--config',str(args.config.resolve())],
                                         stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=os.name!='nt')
                try:
                    output,_=process.communicate(timeout=45);result=json.loads(output)
                except subprocess.TimeoutExpired:
                    kill_owned(process);result={'status':'PUBLIC_PROBE_TIMEOUT','execution_enabled':False}
        else:result=dispatch(cfg,args.command)
        print(json.dumps(result,ensure_ascii=True,indent=2,allow_nan=False))
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps({'status':'R12_UNAVAILABLE','error_type':type(exc).__name__,'execution_enabled':False}));raise SystemExit(2)


if __name__=='__main__':main()
