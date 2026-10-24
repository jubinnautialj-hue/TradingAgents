"""R1.4 bounded operational wrapper; native research/observation/paper unchanged."""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.metadata as metadata
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from tradingagents import ubuntu_deployment as deployment


def components():
    from tradingagents import automation, paper_instruments
    return automation, paper_instruments


def native_config(bundle):
    a,r13=components()
    config=a.AutomationConfig.model_validate(a.read(Path(bundle)/'automation.json'))
    profiles=r13.load_config(Path(bundle)/'instruments.json')
    a.paths(config)
    return config,profiles


def runtime_check(bundle,phase=None):
    config,manifest=deployment.validate_bundle(bundle,source_root=deployment.ROOT)
    result=deployment.host_check(config,phase=phase)
    issues=result['issues']
    if Path(bundle).resolve()!=Path(config.config_dir).resolve():issues.append('BUNDLE_NOT_AT_APPROVED_PATH')
    if deployment.ROOT.resolve()!=Path(config.release_dir).resolve():issues.append('WRONG_RELEASE_PATH')
    if Path.home().resolve()!=Path(config.data_dir+'/home').resolve():issues.append('WRONG_ISOLATED_HOME')
    if Path(sys.prefix).resolve()!=Path(config.release_dir+'/.venv').resolve():issues.append('WRONG_VIRTUAL_ENVIRONMENT')
    if (deployment.ROOT/'.env').exists():issues.append('UNREVIEWED_RELEASE_DOTENV')
    if not (os.environ.get('GIT_CONFIG_COUNT')=='1' and os.environ.get('GIT_CONFIG_KEY_0')=='safe.directory'
            and os.environ.get('GIT_CONFIG_VALUE_0')==config.release_dir):
        issues.append('SCOPED_GIT_SAFE_DIRECTORY_REQUIRED')
    for raw in (config.release_dir,config.config_dir):
        path=Path(raw)
        if not path.is_dir():issues.append('MISSING_READ_ONLY_DIRECTORY');continue
        if hasattr(os,'geteuid') and (path.stat().st_uid!=0 or path.stat().st_mode & 0o022):issues.append('CODE_OR_CONFIG_NOT_ROOT_CONTROLLED')
    if sys.platform=='linux':
        protected=[deployment.ROOT/name for name in manifest['source_sha256']]
        protected += [Path(bundle)/name for name in manifest['files']]+[Path(bundle)/'manifest.json']
        if any(path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode & 0o022 for path in protected):
            issues.append('CODE_OR_BUNDLE_FILE_NOT_ROOT_CONTROLLED')
    data=Path(config.data_dir)
    if not data.is_dir():issues.append('MISSING_DATA_DIRECTORY')
    elif hasattr(os,'geteuid') and (data.stat().st_uid!=os.geteuid() or data.stat().st_mode & 0o077):issues.append('DATA_NOT_PRIVATE_OR_WRONG_OWNER')
    pins=json.loads((Path(bundle)/'dependency-candidate.json').read_text(encoding='utf-8'))['pins']
    mismatch=[]
    for name,version in pins.items():
        try: actual=metadata.version(name)
        except metadata.PackageNotFoundError: actual=None
        if actual!=version:mismatch.append(name)
    if mismatch:issues.append('DEPENDENCY_PIN_MISMATCH')
    result.update(dependency_mismatches=mismatch,manifest_hash=manifest['manifest_hash'],ready=not issues)
    return config,manifest,result


def command_for(phase,bundle,manifest):
    if phase not in deployment.PHASES:raise ValueError('Unknown phase')
    path=Path(bundle)
    base=[sys.executable,'-m']
    if phase in {'research','observe'}:
        return base+['tradingagents.automation',phase,'--config',str(path/'automation.json'),
                     '--expected-config-hash',manifest['native_automation_hash']]
    return base+['tradingagents.paper_instruments',phase,'--config',str(path/'automation.json'),
                 '--instruments',str(path/'instruments.json'),'--expected-profile-hash',manifest['native_profile_hash'],
                 '--expected-config-hash',manifest['native_automation_hash']]


def capacity_guard(cfg):
    """Pause NEW research before native caps; never evict sealed archives/books."""
    a,_=components();alerts=[]
    for source in a.paths(cfg):
        registry=Path(source['registry']);count=0
        if registry.is_dir():
            for path in registry.glob('*.json'):
                if len(path.stem)==64 and all(c in '0123456789abcdef' for c in path.stem):count+=1
                if count>=cfg.schedule.max_records_per_source:break
        if count>=cfg.schedule.max_records_per_source*.8:
            alerts.append({'source':source['name'],'reason':'F1_INDEX_CAPACITY_NEAR_LIMIT',
                           'records':count,'hard_limit':cfg.schedule.max_records_per_source})
    state=a.expand(cfg.state_root)
    if state.is_dir():
        for index,path in enumerate(state.glob('f3/**/ledger.json')):
            if index>=100:alerts.append({'reason':'BOOK_ENUMERATION_CAP'});break
            if path.stat().st_size>=.8*32*1024*1024:alerts.append({'reason':'PAPER_BOOK_CAPACITY_NEAR_LIMIT'})
    return alerts


def bounded_child(command,timeout,capture_directory):
    """Capture bounded JSON only; stderr discarded. Kill only owned process tree."""
    a,_=components()
    capture_directory=Path(capture_directory);capture_directory.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();process=None
    with tempfile.TemporaryFile(dir=capture_directory) as output:
        try:
            process=subprocess.Popen(command,cwd=deployment.ROOT,stdout=output,stderr=subprocess.DEVNULL,
                                     start_new_session=os.name!='nt')
            while process.poll() is None:
                if time.monotonic()-started>timeout or output.tell()>2*1024*1024:
                    a.kill_owned(process)
                    return {'status':'TIMEOUT_OR_OUTPUT_BUDGET','execution_enabled':False}
                time.sleep(.05)
            if output.tell()>2*1024*1024:raise ValueError('Child output resource cap')
            output.seek(0);value=json.loads(output.read().decode('utf-8'))
            if not isinstance(value,dict):raise ValueError('Expected structured child result')
            return {'status':'CHILD_COMPLETED' if process.returncode==0 else 'CHILD_FAILED',
                    'returncode':process.returncode,'result':value,'elapsed_seconds':time.monotonic()-started,
                    'execution_enabled':False}
        finally:
            if process is not None and process.poll() is None:a.kill_owned(process)


def clock_observation(client,limit_ms=1000):
    a,_=components()
    start=a.r11.utc_ms();server=int(client.get('/api/v5/public/time')[0]['ts']);received=a.r11.utc_ms()
    offset=server-(start+received)/2
    if received<start or received-start>2000 or abs(offset)>limit_ms:raise ValueError('Clock offset or uncertainty too large')
    return {'request_started_ms':start,'received_ms':received,'server_ms':server,
            'offset_midpoint_ms':offset,'rtt_ms':received-start,'source':'/api/v5/public/time'}


def run_phase(bundle,phase):
    a,r13=components()
    config,manifest,health=runtime_check(bundle,phase)
    unsafe={'LINUX_REQUIRED','NON_ROOT_REQUIRED','WRONG_SERVICE_USER','WRONG_RELEASE_PATH',
            'BUNDLE_NOT_AT_APPROVED_PATH','WRONG_ISOLATED_HOME','MISSING_DATA_DIRECTORY','DATA_NOT_PRIVATE_OR_WRONG_OWNER'}
    if not health['ready'] and unsafe.intersection(health.get('issues',[])):
        return {'status':'BLOCKED_ENVIRONMENT_NO_WRITES','issues':health['issues'],'execution_enabled':False}
    cfg,_=native_config(bundle)
    root=Path(config.data_dir)/'operations';root.mkdir(parents=True,exist_ok=True)
    identifier=str(a.r11.utc_ms())+'_'+uuid4().hex
    started={'version':deployment.VERSION,'phase':phase,'run_id':identifier,'started_ms':a.r11.utc_ms(),
             'manifest_hash':manifest['manifest_hash'],'invoked_by_systemd':bool(os.environ.get('INVOCATION_ID')),
             'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
    period,offset={'research':(14400000,20000),'observe':(60000,2000),'paper':(300000,20000),'timely':(3600000,3520000)}[phase]
    reference=started['started_ms']//period*period+offset
    if reference>started['started_ms']:reference-=period
    started.update(cadence_reference_ms=reference,dispatch_lag_from_cadence_ms=started['started_ms']-reference,
                   dispatch_reference_scope='CADENCE_GRID_NOT_AUTHENTICATED_SYSTEMD_TRIGGER_TIMESTAMP')
    # R12 locks still protect native state. This outer phase lock prevents two
    # supervisors, while keeping model and timely channels independent.
    with a.locked(root/(phase+'.supervisor'),wait=False) as held:
        if not held:return {'status':'SUPERVISOR_BUSY','execution_enabled':False}
        a.r11.seal(root/(identifier+'.started.json'),started)
        value={'status':'BLOCKED_ENVIRONMENT','health':health}
        begin=time.monotonic();old_handler=None
        try:
            if health['ready']:
                alerts=capacity_guard(cfg) if phase=='research' else []
                if alerts:
                    a.set_pause(cfg,'research',True)
                    value={'status':'CAPACITY_PAUSED_RESEARCH_ONLY','alerts':alerts}
                    raise CapacityPause()
                if phase=='timely':
                    limits=a.f2.ScanLimits(max_requests=1,max_seconds=10)
                    value['preflight_clock']=clock_observation(a.f2.PublicBudgetClient(limits),config.clock_offset_limit_ms)
                timeouts={'research':2730,'observe':80,'paper':80,'timely':135}
                # Convert SIGTERM to a bounded unwind so the owned child dies
                # and an interrupted audit record can be committed when possible.
                def terminate(*unused):raise InterruptedError('Service stopping')
                old_handler=signal.signal(signal.SIGTERM,terminate)
                child=bounded_child(command_for(phase,bundle,manifest),timeouts[phase],root/'capture')
                value={**value,**child}
                value.pop('health',None)
        except BaseException as exc:
            if not isinstance(exc,CapacityPause):
                value={'status':'INTERRUPTED' if isinstance(exc,(InterruptedError,KeyboardInterrupt)) else 'COMPONENT_UNAVAILABLE',
                       'error_type':type(exc).__name__}
        finally:
            if old_handler is not None:signal.signal(signal.SIGTERM,old_handler)
            value.update(**started,finished_ms=a.r11.utc_ms(),elapsed_seconds=time.monotonic()-begin)
            if sys.platform=='linux':
                import resource
                usage=resource.getrusage(resource.RUSAGE_CHILDREN)
                value['child_resources']={'cpu_seconds':usage.ru_utime+usage.ru_stime,'peak_child_rss_kib':usage.ru_maxrss,
                                          'scope':'CHILD_HIGH_WATER_NOT_SUM_OF_SIMULTANEOUS_RSS'}
            a.r11.seal(root/(identifier+'.finished.json'),value)
    # Only bounded operational facts reach service logs. Native details remain
    # in the private audit directory, never tracebacks/headers/credentials.
    return {'status':value['status'],'phase':phase,'run_id':identifier,
            'elapsed_seconds':value['elapsed_seconds'],'execution_enabled':False}


class CapacityPause(Exception):
    """Internal bounded branch, not a model error or failed research sample."""


def arm_probe(plan_path,target_ms,symbols=None,now=None):
    a,r13=components();now=a.r11.utc_ms() if now is None else now
    symbols=symbols or ['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP']
    if len(set(symbols))!=len(symbols) or not 1<=len(symbols)<=3:raise ValueError('Probe instrument budget')
    for symbol in symbols:a.f2.okx.validate_symbol(symbol)
    if target_ms%a.f3.HOUR or not now<target_ms<=now+a.f3.HOUR:raise ValueError('Arm only a strictly future hour within one hour')
    plan={'version':'r14-public-timing-probe-v1','target_ms':target_ms,'armed_ms':now,'instruments':symbols,
          'expected_dispatch_ms':target_ms-80000,'scope':'PROBE_ONLY_NOT_F1_F2_F3',
          'paper_trades':0,'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
    plan['plan_id']=a.digest(plan)
    path=Path(plan_path);path.parent.mkdir(parents=True,exist_ok=True)
    with a.locked(path,wait=False) as held:
        if not held or path.exists():raise ValueError('Probe plan already exists/busy; never overwrite')
        a.r11.seal(path,plan)
    return plan


def run_probe(plan_path,*,client=None,sleep=time.sleep,clock=None,monotonic=time.monotonic,limit_ms=1000):
    """One independently armed quote observation, NEVER a hypothetical trade."""
    a,_=components();clock=clock or a.r11.utc_ms;path=Path(plan_path)
    plan=a.r11.unseal(path);identity=plan.get('plan_id')
    if identity!=a.digest({k:v for k,v in plan.items() if k!='plan_id'}):raise ValueError('Probe plan fingerprint')
    if plan.get('version')!='r14-public-timing-probe-v1' or plan.get('scope')!='PROBE_ONLY_NOT_F1_F2_F3':raise ValueError('Probe scope')
    target=plan['target_ms'];armed=plan['armed_ms']
    if not 0<armed<target or target%a.f3.HOUR or target-armed>a.f3.HOUR:raise ValueError('Invalid prospective probe time')
    if len(set(plan['instruments']))!=len(plan['instruments']) or not 1<=len(plan['instruments'])<=3:raise ValueError('Probe budget')
    for symbol in plan['instruments']:a.f2.okx.validate_symbol(symbol)
    if any(plan.get(k) is not False for k in ('execution_enabled','allow_new_risk','simulation_admitted')) or plan.get('paper_trades')!=0:
        raise ValueError('Probe cannot grant permissions or create trades')
    started=clock();receipt=path.with_name(identity+'.result.json');attempt=path.with_name(identity+'.attempt.json')
    with a.locked(receipt,wait=False) as held:
        if not held:return {'status':'PROBE_BUSY','execution_enabled':False}
        if receipt.exists():return a.r11.unseal(receipt)
        value={**plan,'started_ms':started,'dispatch_lag_ms':started-plan['expected_dispatch_ms'],
               'observations':[],'status':'NOT_OBSERVED','invoked_by_systemd':bool(os.environ.get('INVOCATION_ID'))}
        # A killed observation can never be resumed as a contemporaneous quote.
        if attempt.exists():value['status']='INTERRUPTED_UNCONFIRMED'
        elif started<plan['expected_dispatch_ms']:return {**value,'status':'TOO_EARLY_NO_ATTEMPT'}
        elif started>target+15000:value['status']='MISSED_NO_BACKFILL'
        else:
            a.r11.seal(attempt,{'started_ms':started,'plan_id':identity,'execution_enabled':False})
            limits=a.f2.ScanLimits(max_requests=4,max_seconds=120)
            client=client or a.f2.PublicBudgetClient(limits)
            begin=monotonic();baseline=clock()
            try:
                value['clock_check']=clock_observation(client,limit_ms)
                while clock()<target+1000:
                    if monotonic()-begin>115:raise TimeoutError('Probe deadline')
                    if abs((clock()-baseline)-(monotonic()-begin)*1000)>limit_ms:raise ValueError('Clock jumped during probe')
                    sleep(min(.25,max(.001,(target+1000-clock())/1000)))
                market=a.f3.Market(client,limits)
                for symbol in plan['instruments']:
                    if abs((clock()-baseline)-(monotonic()-begin)*1000)>limit_ms:raise ValueError('Clock jumped before quote')
                    observed=market.opening(symbol,target)
                    if abs((clock()-baseline)-(monotonic()-begin)*1000)>limit_ms:raise ValueError('Clock jumped during quote')
                    value['observations'].append({'instrument':symbol,**observed,'paper_trade':False})
                value['status']='ON_TIME_PUBLIC_QUOTES_NOT_TRADES'
            except Exception as exc:
                value.update(status='MISSED_OR_UNCONFIRMED',error_type=type(exc).__name__)
            value['logical_get_calls']=getattr(client,'calls',None)
        value['finished_ms']=clock();a.r11.seal(receipt,value)
        return value


def unified_status(cfg,profiles,operations=None):
    a,r13=components();base=a.status(cfg);index=base['index'];paper=r13.status(cfg,profiles)
    assets={p.instrument:{'latest_research':base['latest_research'].get(p.instrument),'f1':{'registered':0,'completed':0,'wait':0,'failed':0,'valid_candidates':0,'data_quality_issues':0},
               'f2':{'triggered':0,'expired':0,'invalidated':0,'pending':0,'paused':0,'gap_events':0,'unobserved_expired':0,'admin_excluded':0},'paper_books':[]} for p in profiles.profiles}
    issues=[]
    for source in index['sources']:
        if source['status']!='READY':continue
        registry=a.readonly_registry(source['registry'])
        for rid in source['registrations']:
            record=registry.load(rid)
            if record.origin!='PROSPECTIVE' or record.instrument not in assets:continue
            asset=assets[record.instrument];counts=asset['f1'];counts['registered']+=1
            counts['completed']+=record.workflow_status=='COMPLETED';counts['failed']+=record.workflow_status!='COMPLETED'
            counts['wait']+=record.workflow_status=='COMPLETED' and record.action=='WAIT'
            counts['valid_candidates']+=record.workflow_status=='COMPLETED' and record.plan_valid
            counts['data_quality_issues']+=record.data_quality!='VALID'
            if not record.plan_valid:continue
            try:
                if registry.tracking_status(rid)!='QUEUED':asset['f2']['admin_excluded']+=1;continue
                events=a.readonly_tracking(source['f2'],record)
                if not events and record.expires_ms is not None and record.expires_ms<=a.r11.utc_ms():
                    asset['f2']['unobserved_expired']+=1;continue
                state=a.f2.state_of(events)
                key={'ENTRY_ELIGIBLE':'triggered','EXPIRED':'expired','INVALIDATED':'invalidated'}.get(state,'pending')
                asset['f2'][key]+=1;asset['f2']['paused']+=bool(events and events[-1]['kind']=='PAUSED')
                asset['f2']['gap_events']+=sum('GAP' in json.dumps(e.get('payload',{})) for e in events)
            except (ValueError,KeyError,OSError) as exc:issues.append({'stage':'F2','error_type':type(exc).__name__})
    for row in paper['results']:
        assets[row['instrument']]['paper_books'].append({'source':row['source'],'ledger':row['ledger'],
                 'policy':row['report']['version'],'paper':row['report']['paper'],
                 'reliability':row['report']['reliability'],'capital_scope':'INDEPENDENT_BOOK_NOT_SHARED_PORTFOLIO'})
    runs=[]
    if operations and Path(operations).is_dir():
        starts=sorted(Path(operations).glob('*.started.json'),key=lambda p:p.name)[-200:]
        for path in starts:
            start=a.r11.unseal(path);finish=path.with_name(path.name.replace('.started.','.finished.'))
            result=a.r11.unseal(finish) if finish.exists() else {'status':'RUNNING_OR_INTERRUPTED_UNKNOWN'}
            runs.append({'phase':start['phase'],'started_ms':start['started_ms'],'status':result['status'],
                         'elapsed_seconds':result.get('elapsed_seconds'),'child_resources':result.get('child_resources'),
                         'run_id':start['run_id']})
    known=[r for r in runs if r['status']!='RUNNING_OR_INTERRUPTED_UNKNOWN']
    return {'version':deployment.VERSION,'status':'READ_ONLY','assets':assets,'native_research_tasks':base['r11_tasks'],
            'native_r12_f3_counts_are_btc_only':True,'recent_operations':runs,'recent_operations_cap':200,
            'dispatch_completion_rate':sum(r['status']=='CHILD_COMPLETED' for r in known)/len(known) if known else None,
            'dispatch_completion_denominator':len(known),'dispatch_rate_scope':'PROCESS_COMPLETION_NOT_STRATEGY_OR_TIMING_SUCCESS',
            'statistics_complete':base['statistics_complete'] and not issues,'issues':issues+base['issues'],
            'capacity_alerts':capacity_guard(cfg),
            'unapproved_registrations':paper['unapproved_registrations'],'paired_performance_claim':None,
            'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}


def save_status(value,directory):
    a,_=components();directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    stem=a.digest(value);a.atomic(directory/(stem+'.json'),value)
    body=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    (directory/(stem+'.html')).write_text('<!doctype html><meta charset="utf-8"><h1>R1.4 research / paper only</h1><pre>'+html.escape(body)+'</pre>',encoding='utf-8')
    for symbol,asset in value['assets'].items():a.atomic(directory/(stem+'_'+symbol+'.json'),asset)
    return {'status':'REPORT_SAVED','json':str(directory/(stem+'.json')),'html':str(directory/(stem+'.html')),'execution_enabled':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['dry-run','run','status','report','public-probe','arm-probe','run-probe','model-probe'])
    parser.add_argument('phase',nargs='?',choices=deployment.PHASES)
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--automation',type=Path)
    parser.add_argument('--instruments',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--target-ms',type=int)
    args=parser.parse_args()
    try:
        a,r13=components()
        if args.command in {'status','report'} and args.automation and args.instruments:
            cfg=a.AutomationConfig.model_validate(a.read(args.automation));profiles=r13.load_config(args.instruments)
            value=unified_status(cfg,profiles)
        else:
            if not args.bundle:raise ValueError('Bundle required')
            config,manifest=deployment.validate_bundle(args.bundle,source_root=deployment.ROOT)
            if args.command=='dry-run':
                value={'status':'DRY_RUN_NO_SIDE_EFFECTS','commands':{p:command_for(p,args.bundle,manifest) for p in deployment.PHASES},
                       'health':deployment.host_check(config),'execution_enabled':False}
            elif args.command=='run':
                if not args.phase:raise ValueError('Phase required')
                value=run_phase(args.bundle,args.phase)
            elif args.command in {'status','report'}:
                cfg,profiles=native_config(args.bundle)
                value=unified_status(cfg,profiles,Path(config.data_dir)/'operations')
            elif args.command=='arm-probe':
                if not args.plan or not args.target_ms:raise ValueError('Explicit plan path and future hour required')
                value=arm_probe(args.plan,args.target_ms)
            else:
                _,_,health=runtime_check(args.bundle,'research' if args.command=='model-probe' else None)
                if not health['ready']:raise ValueError('Runtime not ready')
                if args.command=='run-probe':
                    if not args.plan:raise ValueError('Plan required')
                    value=run_probe(args.plan,limit_ms=config.clock_offset_limit_ms)
                elif args.command=='public-probe':value=r13.public_probe()
                else:
                    if not args.output or args.output.exists():raise ValueError('New private model probe directory required')
                    value=bounded_child([sys.executable,'-m','scripts.probe_okx_llm','--output',str(args.output)],70,
                                         Path(config.data_dir)/'operations/capture')
                    raw=value.pop('result',{})
                    value['probe']={key:raw.get(key) for key in ('status','elapsed_seconds','usage','completion_status','exception_type')}
        if args.command=='report':
            if not args.output:raise ValueError('Explicit report output required')
            value=save_status(value,args.output)
        print(json.dumps(value,ensure_ascii=True,indent=2,allow_nan=False))
    except Exception as exc:
        print(json.dumps({'status':'R14_UNAVAILABLE','error_type':type(exc).__name__,'execution_enabled':False}));raise SystemExit(2)


if __name__=='__main__':main()
