"""F3.1 opt-in opening observation. Frozen F3/R13 modules stay untouched."""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import time
from uuid import uuid4
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from pydantic import Field
from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents import auto_research as audit, automation, paper_instruments as instruments
from tradingagents import forward_paper as native, paper_timing, forward_tracking as tracking
from tradingagents.dataflows.files import locked
from tradingagents.opening_observation_transport import PublicClient, PublicFailure, WireBudget
from tradingagents.quote_probe_diagnostics import WindowGuard, WindowDeadline

VERSION='forward-paper-opening-observation-v1'
SYMBOLS=('BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP')
ROOT=Path(__file__).resolve().parents[1]
CODE_HASH=audit.digest({name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
    'tradingagents/forward_paper_opening_observation.py','tradingagents/opening_observation_transport.py',
    'tradingagents/forward_paper.py','tradingagents/paper_timing.py','tradingagents/paper_instruments.py',
    'tradingagents/quote_probe_diagnostics.py')})


class Config(ResearchModel):
    version: Literal['forward-paper-opening-observation-v1']=VERSION
    enabled: bool=False
    window_ms: Literal[15000]=15000
    round_offsets_ms: tuple[Literal[1000],Literal[5000],Literal[9000]]=(1000,5000,9000)
    per_candidate_attempts: Literal[3]=3
    total_http_budget: Literal[60]=60
    quote_http_budget: Literal[9]=9
    max_due: Literal[3]=3
    request_timeout_ms: Literal[1000]=1000
    min_request_interval_ms: Literal[250]=250
    allow_new_risk: Literal[False]=False
    simulation_admitted: Literal[False]=False
    execution_enabled: Literal[False]=False


class Approval(ResearchModel):
    version: Literal['forward-paper-opening-observation-v1']=VERSION
    root: str
    activated_ms: int=Field(gt=0)
    namespace: Literal['PRODUCTION','FIXTURE']
    config: Config
    automation: dict
    instruments: dict
    code_hash: str
    git_commit: str
    allow_new_risk: Literal[False]=False
    simulation_admitted: Literal[False]=False
    execution_enabled: Literal[False]=False


def _reviewed(commit):
    for name in ('tradingagents/forward_paper_opening_observation.py','tradingagents/opening_observation_transport.py',
                 'tradingagents/forward_paper.py','tradingagents/paper_timing.py','tradingagents/paper_instruments.py',
                 'tradingagents/quote_probe_diagnostics.py'):
        try:stored=subprocess.check_output(['git','show',commit+':'+name],cwd=ROOT,stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:raise ValueError('Reviewed implementation commit required') from None
        if stored.replace(b'\r\n',b'\n')!=(ROOT/name).read_bytes().replace(b'\r\n',b'\n'):
            raise ValueError('Reviewed implementation commit required')


def initialize(root,cfg,profiles,*,enable=False,config=None,namespace='PRODUCTION',now=None):
    """No legacy import. A production build must already be reviewed in Git."""
    root=Path(root).resolve();config=(config or Config()).model_copy(update={'enabled':bool(enable)})
    if not config.enabled:raise ValueError('Explicit --enable required')
    if any(p.enabled and p.instrument not in SYMBOLS for p in profiles.profiles):
        raise ValueError('Instrument not approved for F31')
    if namespace=='PRODUCTION':_reviewed('HEAD')
    protected=[]
    for route in automation.paths(cfg):
        protected.extend(Path(route[k]).resolve() for k in ('registry','f2','f3'))
    for route in instruments.routes(profiles,automation.paths(cfg)):
        protected.append(Path(route['f3']).resolve())
    if any(root==p or root in p.parents or p in root.parents for p in protected):
        raise ValueError('New archive must not overlap sealed or legacy archives')
    if root.exists():raise ValueError('New empty extension root required')
    value=Approval(root=str(root),activated_ms=native.okx.utc_ms() if now is None else now,
        namespace=namespace,config=config,automation=cfg.model_dump(),instruments=profiles.model_dump(),
        code_hash=CODE_HASH,git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
    root.mkdir(parents=True,mode=0o700)
    audit.seal(root/'approval.json',value.model_dump())
    return value


def load(root):
    root=Path(root).resolve();value=Approval.model_validate(audit.unseal(root/'approval.json'))
    if value.root!=str(root) or value.code_hash!=CODE_HASH or not value.config.enabled:
        raise ValueError('Extension approval or code mismatch')
    if value.namespace=='PRODUCTION':_reviewed(value.git_commit)
    automation.AutomationConfig.model_validate(value.automation)
    instruments.InstrumentConfig.model_validate(value.instruments)
    return value


class Protocol(native.PaperProtocol):
    version: Literal['forward-paper-opening-observation-v1']=VERSION
    parent_policy_hash: str
    registry_binding_hash: str
    instrument_admission: dict
    opening_observation: Config


class Event(native.PaperEvent):
    version: Literal['forward-paper-opening-observation-v1']=VERSION


class Journal(ResearchModel):
    policy: Protocol
    code_hash: str
    namespace: Literal['PRODUCTION','FIXTURE']
    events: list[Event]=Field(max_length=20000)


class Report(native.PaperReport):
    version: Literal['forward-paper-opening-observation-v1']
    opening_policy_hash: str
    parent_policy_hash: str
    approval_hash: str


class TargetNotVisible(ValueError):
    pass


def engine(profile,registry,approval):
    """Compose the original R13 engine; never mutate its globals or identities."""
    if not approval.config.enabled or approval.code_hash!=CODE_HASH:raise ValueError('Observation extension disabled or code mismatch')
    approved=instruments.InstrumentConfig.model_validate(approval.instruments)
    if profile.instrument not in SYMBOLS or not any(p.model_dump()==profile.model_dump() for p in approved.profiles):
        raise ValueError('Profile not present in sealed observation approval')
    profile=profile.model_copy(deep=True);approval=approval.model_copy(deep=True)
    parent=instruments.engine(profile,registry)
    protocol=Protocol(**{**native.POLICY,'version':VERSION},parent_policy_hash=parent.POLICY_HASH,registry_binding_hash=audit.digest(str(Path(registry).resolve())),
        instrument_admission=profile.model_dump(exclude={'enabled'}),opening_observation=approval.config)
    policy=protocol.model_dump(mode='json');policy_hash=audit.digest(policy);approval_hash=audit.digest(approval.model_dump())
    symbol=profile.instrument
    code_hash=audit.digest({'extension':CODE_HASH,'parent':parent.CODE_HASH,'policy':policy_hash})
    def check(s):
        if (s.namespace!=approval.namespace or s.identity.get('opening_observation_policy_hash')!=policy_hash
            or s.identity.get('opening_approval_hash')!=approval_hash
            or s.identity.get('paper_protocol_hash')!=parent.POLICY_HASH
            or s.identity.get('instrument')!=symbol):raise ValueError('Foreign or legacy observation state')
    deps=dict(vars(native));deps.update(VERSION=VERSION,PROTOCOL=protocol,POLICY=policy,POLICY_HASH=policy_hash,
        CODE_HASH=code_hash,PaperJournal=Journal,PaperEvent=Event,PaperReport=Report)
    original_load=instruments.bind(native.Ledger.load,deps)
    original_append=instruments.bind(native.Ledger.append,deps)
    class Ledger(native.Ledger):
        def __init__(self,root,namespace=None):
            if namespace is not None and namespace!=approval.namespace:raise ValueError('Wrong namespace')
            actual=Path(root).resolve()
            if approval.namespace=='PRODUCTION' and not actual.is_relative_to(Path(approval.root)):
                raise ValueError('Production book outside approved root')
            super().__init__(root,approval.namespace)
        def load(self):
            events=original_load(self)
            intents={e['payload']['attempt_id']:e['registration_id'] for e in events if e['kind']=='OPENING_ATTEMPT_STARTED'}
            for s in self.states(events).values():
                check(s)
                if s.position:
                    opening=s.position['opening_observation']
                    if intents.get(opening.get('attempt_id'))!=s.registration_id:
                        raise ValueError('Fill without durable opening attempt')
                    if s.position['entry_ms']!=opening['observed_ms']:raise ValueError('Backdated entry')
            return events
        def append(self,events,rid,kind,payload,now):
            if isinstance(payload.get('state'),dict):
                state=native.PaperState.model_validate(payload['state']);check(state)
                if state.position:
                    opening=state.position['opening_observation']
                    if not any(e['kind']=='OPENING_ATTEMPT_STARTED' and e['registration_id']==rid
                        and e['payload']['attempt_id']==opening.get('attempt_id')
                        and e['recorded_ms']<=opening['request_started_ms'] for e in events):
                        raise ValueError('Opening attempt was not durably recorded')
            return original_append(self,events,rid,kind,payload,now)
    deps['Ledger']=Ledger
    def admission(record,trigger,now):
        reasons=parent.admission(record,trigger,now)
        if symbol not in SYMBOLS or not profile.enabled:reasons.append('OBSERVATION_INSTRUMENT_NOT_APPROVED')
        if record.registered_ms<approval.activated_ms:reasons.append('REGISTERED_BEFORE_OBSERVATION_ACTIVATION')
        if record.instrument!=symbol:reasons.append('OBSERVATION_INSTRUMENT_MISMATCH')
        return sorted(set(reasons))
    def create_state(record,trigger,proof,now,namespace=None):
        if admission(record,trigger,now):raise ValueError('Observation admission rejected')
        s=parent.create_state(record,trigger,proof,now,namespace=approval.namespace)
        s.identity.update(instrument=symbol,opening_observation_policy_hash=policy_hash,opening_approval_hash=approval_hash,
                          opening_observation_code_hash=code_hash)
        s.paper_trade_id=audit.digest({'parent_trade':s.paper_trade_id,'observation_policy':policy_hash})
        s.group=audit.digest({'config':record.config,'protocol':policy_hash})
        return s
    def fill(s,opening,now,equity=10000):
        check(s)
        if (opening.get('opening_policy_hash')!=policy_hash or not opening.get('attempt_id')
            or opening.get('http_evidence',{}).get('http_status')!=200):raise ValueError('Missing observation proof')
        if instruments.specification(s.info,symbol)!=profile.certificate.specification:raise ValueError('Instrument specification changed')
        proof=s.revalidation.get('instrument_specification',{})
        if (proof.get('specification')!=profile.certificate.specification or proof.get('source')!='/api/v5/public/instruments'
            or proof.get('fetched_ms',0)>s.revalidation['completed_ms']
            or proof.get('fetched_ms',0)<s.revalidation['checked_ms']-60000
            or proof.get('fingerprint')!=audit.digest({k:v for k,v in proof.items() if k!='fingerprint'})):
            raise ValueError('Missing fresh specification proof')
        wire=opening['http_evidence'];context=wire.get('context') or {}
        if (wire.get('path')!='/api/v5/market/candles' or wire.get('query',{}).get('instId')!=symbol
            or context.get('attempt_id')!=opening['attempt_id'] or context.get('registration_id')!=s.registration_id
            or not opening['request_started_ms']<=wire.get('http_dispatched_ms',0)<=wire.get('response_received_ms',0)
                <=wire.get('completed_ms',0)<=opening['observed_ms']):
            raise ValueError('Opening transport provenance mismatch')
        result=parent.fill(s,opening,now,equity)
        if result.position['entry_ms']!=opening['observed_ms']:raise ValueError('Backdated parent fill')
        return result
    class Market(parent.Market):
        def refresh(self,record,trigger,now):
            captured={};client=self.client
            class Recording:
                get=client.get
                def instrument(self,requested):
                    info=client.instrument(requested)
                    if requested!=symbol or instruments.specification(info,symbol)!=profile.certificate.specification:
                        raise ValueError('Current specification mismatch')
                    captured.update(specification=instruments.specification(info,symbol),source='/api/v5/public/instruments',
                                    fetched_ms=native.okx.utc_ms())
                    return info
            value=parent.Market(Recording(),self.limits).refresh(record,trigger,now)
            if not captured:raise ValueError('Current specification unavailable')
            captured['fingerprint']=audit.digest(captured);value['instrument_specification']=captured
            return value
        def opening(self,requested,target):
            client=self.client;context=getattr(client,'context',None)
            if (requested!=symbol or not context or context.get('target_ms')!=target
                or context.get('instrument')!=symbol):raise ValueError('Bounded timely attempt required')
            client.quote_reason=None
            try:
                obs=super().opening(requested,target)
            except (ValueError,TypeError,IndexError):
                rows=getattr(client,'last_rows',None)
                record=getattr(client,'last_record',None) or {}
                # Retry only structurally valid OLD rows, not malformed/future/invalid target rows.
                old=isinstance(rows,list) and all(isinstance(r,list) and len(r)==9 and str(r[0]).isdigit()
                    and 0<int(r[0])<target for r in rows)
                if record.get('http_status')==200 and old and native.okx.utc_ms()<=target+15000:
                    client.quote_reason='TARGET_BAR_NOT_VISIBLE'
                    raise TargetNotVisible('TARGET_BAR_NOT_VISIBLE') from None
                client.quote_reason='INVALID_OR_UNAVAILABLE_OPENING'
                raise ValueError('INVALID_OR_UNAVAILABLE_OPENING') from None
            record=client.last_record
            if not record or record.get('http_status')!=200:raise ValueError('Missing HTTP proof')
            obs.update(opening_policy_hash=policy_hash,attempt_id=context['attempt_id'],http_evidence=record)
            return obs
    def report(records,events,namespace=None):
        value=instruments.bind(native.report,deps)(records,events,namespace or approval.namespace)
        value.update(opening_policy_hash=policy_hash,parent_policy_hash=parent.POLICY_HASH,approval_hash=approval_hash)
        return value
    deps.update(admission=admission,create_state=create_state,fill=fill,Market=Market,report=report,
                save_report=instruments.bind(native.save_report,deps))
    # Reuse all financial rules in the approved instrument engine.
    deps.update(advance_bar=parent.advance_bar,settle=parent.settle,feedback=parent.feedback,
                _verified_inputs=parent._verified_inputs,instrument_allowed=parent.instrument_allowed,
                Registry=automation.readonly_registry,Path=parent.Path)
    deps['scan']=instruments.bind(native.scan,deps)
    return SimpleNamespace(**deps)


def routes(approval):
    if not approval.config.enabled or approval.code_hash!=CODE_HASH:raise ValueError('Observation extension disabled or code mismatch')
    if load(approval.root)!=approval:raise ValueError('Unsealed runtime approval')
    cfg=automation.AutomationConfig.model_validate(approval.automation)
    profiles=instruments.InstrumentConfig.model_validate(approval.instruments)
    result=[]
    for source in automation.paths(cfg):
        for profile in profiles.profiles:
            if profile.instrument not in SYMBOLS:continue
            core=engine(profile,source['registry'],approval)
            output=Path(approval.root)/'books'/audit.digest(str(Path(source['registry']).resolve()))[:16]/profile.instrument
            if not output.resolve().is_relative_to(Path(approval.root).resolve()):raise ValueError('Book symlink outside approved root')
            result.append({**source,'name':source['name']+'/'+profile.instrument,'instrument':profile.instrument,
                           'f3':str(output),'_paper_engine':core})
    return cfg,result


def readonly_events(source,approval):
    core=source['_paper_engine'];file=Path(source['f3'])/approval.namespace.lower()/'ledger.json'
    if not file.exists():return []
    obj=object.__new__(core.Ledger);obj.root=file.parent;obj.path=file;obj.namespace=approval.namespace
    return obj.load()


def status(root):
    if not (Path(root)/'approval.json').exists():return {'status':'DISABLED','execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
    approval=load(root);_,sources=routes(approval);values=[]
    for source in sources:
        core=source['_paper_engine'];events=readonly_events(source,approval)
        values.append({'instrument':source['instrument'],'source':source['name'],
                       'ledger':source['f3'],'report':core.report([],events)})
    return {'status':'READY','version':VERSION,'namespace':approval.namespace,'books':values,
            'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}


def paper(approval,*,clients=None):
    cfg,sources=routes(approval);results=[]
    if automation.paused(cfg,'paper'):return {'status':'PAUSED','execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
    root=Path(approval.root);root.mkdir(parents=True,exist_ok=True)
    with locked(root/'phase',wait=False) as held:
        if not held:return {'status':'BUSY','execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
        budget=WireBudget(root/'paper-runs'/(str(native.okx.utc_ms())+'_'+uuid4().hex),audit.digest(approval.model_dump()))
        for source in sources:
            core=source['_paper_engine']
            if not Path(source['registry']).is_dir() or not Path(source['f2']).is_dir():
                results.append({'instrument':source['instrument'],'status':'WAITING_FOR_F1_F2'});continue
            client=clients[source['instrument']] if clients else PublicClient(budget)
            limits=tracking.ScanLimits(max_requests=60,max_seconds=120)
            try:value=core.scan(source['registry'],source['f2'],source['f3'],limits,market=core.Market(client,limits))
            except (ValueError,TypeError,IndexError,OSError,tracking.BudgetExhausted) as exc:value={'status':'PAUSED','error_type':type(exc).__name__}
            results.append({'instrument':source['instrument'],'result':value})
    return {'version':VERSION,'results':results,'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}


def _safe_step(cfg,source,rid,phase,market,*,scheduled_for_ms):
    try:return paper_timing.step(cfg,source,rid,phase,market,scheduled_for_ms=scheduled_for_ms)
    except Exception as exc:
        value={'status':'PAUSED','registration_id':rid,'instrument':source['instrument'],'error_type':type(exc).__name__}
        ledger=source['_paper_engine'].Ledger(source['f3'])
        try:
            with locked(ledger.path):
                events=ledger.load();ledger.append(events,rid,'PAUSED',{'reason':'OBSERVATION_COMPONENT_UNAVAILABLE',
                    'error_type':type(exc).__name__},native.okx.utc_ms())
        except (ValueError,OSError):value['audit_status']='BOOK_UNAVAILABLE_NO_REPAIR'
        return value


def _attempt(source,rid,index,target,approval,cfg,client,*,market=None):
    core=source['_paper_engine'];ledger=core.Ledger(source['f3']);now=native.okx.utc_ms()
    if index not in range(3):raise ValueError('Invalid opening round')
    if not target+approval.config.round_offsets_ms[index]<=now<=target+15000:
        return {'status':'NOT_DUE','registration_id':rid}
    identity=audit.digest({'registration':rid,'instrument':source['instrument'],'policy':core.POLICY_HASH,
                           'target_ms':target,'round':index})
    with locked(ledger.path,wait=False) as held:
        if not held:return {'status':'LEDGER_BUSY','registration_id':rid,'instrument':source['instrument']}
        events=ledger.load();s=ledger.states(events).get(rid)
        if not s or s.status!='SCHEDULED':return {'status':'NOT_SCHEDULED','registration_id':rid,'instrument':source['instrument']}
        prior=[e for e in events if e['registration_id']==rid and e['kind']=='OPENING_ATTEMPT_STARTED']
        done={e['payload']['attempt_id']:e['payload'] for e in events if e['kind']=='OPENING_ATTEMPT_RESULT' and e['registration_id']==rid}
        if any(e['payload']['attempt_id']==identity for e in prior):return {'status':'ATTEMPT_ALREADY_CONSUMED','registration_id':rid,'instrument':source['instrument']}
        if len(prior)>=3 or any(done.get(e['payload']['attempt_id'],{}).get('retryable') is not True for e in prior):
            return {'status':'NO_SAFE_RETRY','registration_id':rid,'instrument':source['instrument']}
        payload={'attempt_id':identity,'instrument':source['instrument'],'target_ms':target,'deadline_ms':target+15000,'round':index}
        ledger.append(events,rid,'OPENING_ATTEMPT_STARTED',payload,now)
    client.context={**payload,'registration_id':rid};client.quote_reason=None
    try:
        result=_safe_step(cfg,source,rid,'open',market or core.Market(client,tracking.ScanLimits()),
                                scheduled_for_ms=target+approval.config.round_offsets_ms[index])
    except Exception as exc:
        result={'status':'PAUSED','error_type':type(exc).__name__}
        with locked(ledger.path):
            events=ledger.load();ledger.append(events,rid,'PAUSED',{'reason':'OBSERVATION_COMPONENT_UNAVAILABLE',
                'error_type':type(exc).__name__},native.okx.utc_ms())
    finally:client.context=None
    receipt={'registration_id':rid,'instrument':source['instrument'],'round':index,'target_ms':target,'attempt_id':identity,'result':result,'retryable':result['status']=='PAUSED' and client.quote_reason=='TARGET_BAR_NOT_VISIBLE'}
    with locked(ledger.path):
        events=ledger.load();ledger.append(events,rid,'OPENING_ATTEMPT_RESULT',receipt,native.okx.utc_ms())
    return receipt


def timely(approval,*,client=None,sleep=time.sleep,monotonic=time.monotonic,market=None):
    """Single upcoming boundary. No model or autonomous service is started."""
    cfg,sources=routes(approval);root=Path(approval.root)
    if automation.paused(cfg,'timely'):return {'status':'PAUSED','execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
    with locked(root/'phase',wait=False) as held:
        if not held:return {'status':'BUSY','execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
        rows=[];excluded=[]
        for source in sources:
            core=source['_paper_engine']
            try:events=readonly_events(source,approval)
            except (ValueError,OSError) as exc:
                excluded.append({'instrument':source['instrument'],'source':source['name'],'reason':'BOOK_UNAVAILABLE',
                                 'error_type':type(exc).__name__})
                continue
            for state in core.Ledger.states(events).values():
                if state.status!='SCHEDULED':continue
                intents=[e for e in events if e['registration_id']==state.registration_id and e['kind']=='OPENING_ATTEMPT_STARTED']
                receipts={e['payload']['attempt_id']:e['payload'] for e in events if e['registration_id']==state.registration_id and e['kind']=='OPENING_ATTEMPT_RESULT'}
                if native.okx.utc_ms()<=state.planned_entry_ms+15000 and (len(intents)>=3 or any(
                    receipts.get(e['payload']['attempt_id'],{}).get('retryable') is not True for e in intents)):
                    reason=('ATTEMPT_LIMIT_REACHED' if len(intents)>=3 else 'INTERRUPTED_UNCONFIRMED_ATTEMPT' if any(
                        e['payload']['attempt_id'] not in receipts for e in intents) else 'NON_RETRYABLE_FAILURE')
                    excluded.append({'registration_id':state.registration_id,'instrument':source['instrument'],'reason':reason})
                    continue
                rows.append((state.planned_entry_ms,source,state.registration_id))
        now=native.okx.utc_ms();rows=sorted(rows,key=lambda r:(r[0],r[1]['name'],r[2]))
        rows=[r for r in rows if r[0]-80000<=now][:approval.config.max_due]
        if not rows:return {'status':'NO_SAFE_PENDING_ATTEMPTS' if excluded else 'NO_PENDING_OPEN_IN_WINDOW',
                            'excluded':excluded,'results':[],'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
        target=rows[0][0];rows=[r for r in rows if r[0]==target]
        budget=WireBudget(root/'windows'/str(target),audit.digest(approval.model_dump()),clock=native.okx.utc_ms,sleep=sleep)
        client=client or PublicClient(budget,deadline=target+15000)
        result=[];baseline=native.okx.utc_ms();begin=monotonic()
        run_id=str(baseline)+'_'+uuid4().hex
        run_root=root/'runs';run_root.mkdir(parents=True,exist_ok=True)
        audit.seal(run_root/(run_id+'.started.json'),{'started_ms':baseline,'policy':VERSION,'target_ms':target,
                   'code_hash':CODE_HASH,'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False})
        clock_proof=None
        guard=WindowGuard(target+15000,native.okx.utc_ms)
        def wait(until):
            while native.okx.utc_ms()<until:
                if automation.paused(cfg,'timely'):raise PublicFailure('PAUSED')
                if abs(native.okx.utc_ms()-baseline-(monotonic()-begin)*1000)>1000:raise PublicFailure('CLOCK_DRIFT')
                sleep(min(.1,(until-native.okx.utc_ms())/1000))
        try:
            if now<=target+15000:
                guard.arm()
                from tradingagents.ubuntu_operations import clock_observation
                clock_proof=clock_observation(client,1000)
            if now<target:
                wait(target-40000)
                for _,source,rid in rows:
                    result.append(_safe_step(cfg,source,rid,'refresh',market or source['_paper_engine'].Market(client,tracking.ScanLimits()),scheduled_for_ms=target-40000))
            for index,offset in enumerate(approval.config.round_offsets_ms):
                wait(target+offset)
                for _,source,rid in rows:
                    if native.okx.utc_ms()>target+15000:break
                    if automation.paused(cfg,'timely'):raise PublicFailure('PAUSED')
                    if abs(native.okx.utc_ms()-baseline-(monotonic()-begin)*1000)>1000:raise PublicFailure('CLOCK_DRIFT')
                    try:result.append(_attempt(source,rid,index,target,approval,cfg,client,market=market))
                    except Exception as exc:
                        result.append({'status':'PAUSED','instrument':source['instrument'],'registration_id':rid,
                                       'error_type':type(exc).__name__})
            # Same native MISSED transition for exhausted/past windows; no backfill.
            if native.okx.utc_ms()>target+15000:
                guard.close()
                # Maintenance clock GET only; core marks MISSED before requesting any quote.
                client.deadline=None
                for _,source,rid in rows:
                    result.append(_safe_step(cfg,source,rid,'open',market or source['_paper_engine'].Market(client,tracking.ScanLimits()),scheduled_for_ms=target+1000))
        except (ValueError,OSError,tracking.BudgetExhausted,WindowDeadline) as exc:
            result.append({'status':'PAUSED','error_type':type(exc).__name__})
        finally:guard.close()
        value={'version':VERSION,'status':'WINDOW_COMPLETE','results':result,'http_budget':budget.status(),
               'excluded':excluded,'clock_check':clock_proof,'started_ms':baseline,'completed_ms':native.okx.utc_ms(),
               'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}
        audit.seal(run_root/(run_id+'.result.json'),value)
        return value


def _cli():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','status','paper','timely'])
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--automation',type=Path,default=ROOT/'docs/automation_r12.json')
    parser.add_argument('--instruments',type=Path,default=instruments.DEFAULT_CONFIG)
    parser.add_argument('--config',type=Path,default=ROOT/'docs/forward_paper_opening_observation_v1.json')
    parser.add_argument('--enable',action='store_true')
    args=parser.parse_args()
    if args.command=='init':
        value=initialize(args.root,automation.AutomationConfig.model_validate(audit.read(args.automation)),
            instruments.load_config(args.instruments),enable=args.enable,config=Config.model_validate(audit.read(args.config))).model_dump()
    elif args.command=='status':value=status(args.root)
    else:
        approval=load(args.root)
        value=paper(approval) if args.command=='paper' else timely(approval)
    print(json.dumps(value,indent=2,allow_nan=False))


def main():
    try:_cli()
    except (ValueError,OSError,subprocess.CalledProcessError) as exc:
        allowed={'Explicit --enable required','Reviewed implementation commit required',
                 'New empty extension root required','Instrument not approved for F31'}
        reason=str(exc) if type(exc) is ValueError and str(exc) in allowed else 'CONFIGURATION_OR_ARCHIVE_UNAVAILABLE'
        print(json.dumps({'status':'BLOCKED','error_type':type(exc).__name__,'reason':reason,
                         'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False}))
        raise SystemExit(2)


if __name__=='__main__':main()
