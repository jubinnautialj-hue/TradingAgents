"""F1 immutable research registry. No market requests, tracking, fills or orders."""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Literal
from uuid import uuid4
from pydantic import Field, model_validator
from tradingagents.agents.perpetual_schemas import ResearchModel, PerpetualResearchResult
from tradingagents.dataflows.files import locked
from tradingagents.memory.okx_simulation import utc_ms

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / 'docs/forward_f1_protocol.json'
ARTIFACTS = ('research_result.json','multitimeframe_snapshot.json','derivatives_snapshot.json',
             'orderflow_snapshot.json','market_checks_snapshot.json','price_depth_snapshot.json',
             'events_snapshot.json','complete_report.md','complete_report.html')
SAFE_CONFIG = ('llm_provider','quick_think_llm','deep_think_llm','market_mode','okx_interval',
               'okx_multi_timeframe','okx_trade_plan_enabled','okx_derivatives_enabled',
               'okx_orderflow_enabled','okx_market_checks_enabled','okx_price_depth_enabled','okx_events_enabled',
               'max_debate_rounds','max_risk_discuss_rounds')

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def atomic(path, value):
    temp=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    with temp.open('w',encoding='utf-8') as f:
        json.dump(value,f,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)

def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()

class ForwardProtocol(ResearchModel):
    version: Literal['forward-f1-v1']
    execution_enabled: Literal[False]
    registration_max_delay_ms: int = Field(gt=0, le=60000)
    prospective_cutoff_max_age_ms: int = Field(gt=0, le=300000)
    clock: str
    lifecycle: str
    entry: str
    costs: dict
    labels: list[str]
    metrics: dict
    limitations: list[str]

    @model_validator(mode='after')
    def frozen_costs(self):
        rules=read(ROOT/'docs/okx_d3_strategy_freeze.json')['rules']
        for key,source in [('initial_equity','equity'),('notional_usdt','notional'),
                           ('taker_fee_bps','taker_bps'),('slippage_bps','slippage_bps'),
                           ('confirmation_bars','confirmation_bars'),('max_holding_bars','maximum_holding_bars')]:
            if self.costs.get(key)!=rules[source]:raise ValueError('F1 must preserve frozen D cost model')
        return self

class Registration(ResearchModel):
    version: Literal['forward-f1-v1'] = 'forward-f1-v1'
    registration_id: str
    research_id: str
    origin: Literal['PROSPECTIVE','HISTORICAL_IMPORT','LATE_REGISTRATION','TEST_REPLAY']
    exchange: Literal['OKX'] = 'OKX'
    instrument: str
    timeframes: list[str]
    cutoff_ms: int | None = Field(default=None, gt=0)
    started_ms: int = Field(gt=0)
    completed_ms: int = Field(gt=0)
    registered_ms: int = Field(gt=0)
    data_available_ms: int | None = Field(default=None, gt=0)
    first_bar_open_ms: int | None = None
    first_check_ms: int | None
    expires_ms: int | None
    git_commit: str
    git_branch: str
    source_hashes: dict[str,str]
    protocol_hash: str
    protocol: dict
    config: dict
    failure: dict = Field(default_factory=dict)
    original_judgment: str
    lifecycle_state: str
    action: str | None
    workflow_status: str
    llm_status: str
    data_quality: str
    plan_valid: bool
    allow_new_risk: bool
    simulation_pool_eligible: bool
    requires_revalidation: Literal[True] = True
    execution_enabled: Literal[False] = False
    status: Literal['AWAITING_F2','ARCHIVED_ONLY']
    pair_id: str | None
    snapshot_id: str | None
    result_hash: str
    original_result: dict
    quality_evidence: dict
    memory_links: list[str] = Field(default_factory=list)
    report_directory: str
    artifact_hashes: dict[str,str]
    content_hash: str = ''

    @model_validator(mode='after')
    def clocks(self):
        if not (self.cutoff_ms or self.completed_ms) <= self.completed_ms <= self.registered_ms or self.started_ms > self.completed_ms:
            raise ValueError('Invalid research clocks')
        if self.data_available_ms is not None and self.data_available_ms > self.completed_ms:
            raise ValueError('Data not available at completion')
        minimum=max(self.cutoff_ms or 0,self.completed_ms,self.registered_ms,self.data_available_ms or 0)
        if self.first_check_ms is not None and (self.first_check_ms < minimum or self.first_check_ms % 3600000):
            raise ValueError('Future observation cannot precede registration/completion')
        if self.first_bar_open_ms is not None and (self.first_bar_open_ms<minimum or self.first_bar_open_ms%3600000 or self.first_check_ms!=self.first_bar_open_ms+3600000):
            raise ValueError('First check must wait for first eligible bar to close')
        if self.simulation_pool_eligible and (self.first_bar_open_ms is None or self.origin!='PROSPECTIVE' or not self.plan_valid or self.data_quality!='VALID' or self.workflow_status!='COMPLETED' or self.expires_ms is None or self.expires_ms<=self.first_check_ms):
            raise ValueError('Ineligible simulation pool sample')
        ForwardProtocol.model_validate(self.protocol)
        if self.protocol_hash != digest(self.protocol) or self.result_hash != digest(self.original_result):
            raise ValueError('Protocol/result fingerprint mismatch')
        return self

class FutureBarObservation(ResearchModel):
    """F2 interchange contract only; F1 does not collect or process bars."""
    version: Literal['forward-observation-v1'] = 'forward-observation-v1'
    registration_id: str
    bar_open_ms: int = Field(gt=0)
    bar_close_ms: int = Field(gt=0)
    available_ms: int = Field(gt=0)
    recorded_ms: int = Field(gt=0)
    bar_fingerprint: str = Field(min_length=64, max_length=64)
    confirm: Literal['1'] = '1'
    execution_enabled: Literal[False] = False

    @model_validator(mode='after')
    def times(self):
        if self.bar_open_ms%3600000 or self.bar_close_ms!=self.bar_open_ms+3600000 or not self.bar_close_ms<=self.available_ms<=self.recorded_ms:
            raise ValueError('Only closed, available UTC hourly observations')
        return self

    def validate_against(self, registration):
        if self.registration_id!=registration.registration_id or registration.first_bar_open_ms is None or registration.first_check_ms is None or self.bar_open_ms<registration.first_bar_open_ms or self.bar_close_ms<registration.first_check_ms:
            raise ValueError('Observation ownership/time leak')
        return digest({'registration_id':self.registration_id,'bar_open_ms':self.bar_open_ms})


def sample_category(record):
    """Independent denominators; a failure-shaped WAIT is never a WAIT judgment."""
    if record.llm_status=='LLM_UNAVAILABLE' or record.workflow_status=='FAILED':return 'RESEARCH_FAILED'
    if record.data_quality!='VALID':return 'DATA_QUALITY_INSUFFICIENT'
    if record.workflow_status!='COMPLETED':return 'RESEARCH_NOT_COMPLETED'
    if record.action=='WAIT':return 'WAIT'
    if record.action in {'REDUCE_LONG','REDUCE_SHORT','EXIT_CANDIDATE'}:return record.action
    if not record.plan_valid:return 'PLAN_INVALID'
    return record.action or 'UNSTRUCTURED_RESEARCH'


class Registry:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        protocol=ForwardProtocol.model_validate(read(PROTOCOL)).model_dump()
        with locked(self.root/'registry'):
            path=self.root/'protocol-forward-f1-v1.json'
            if path.exists() and read(path)!=protocol:raise ValueError('Protocol version already frozen; explicit new version required')
            if not path.exists():atomic(path,protocol)

    def begin(self, instrument, config):
        """Persist intent BEFORE research. Never accepts externally supplied start clock."""
        protocol=ForwardProtocol.model_validate(read(PROTOCOL)).model_dump()
        if protocol!=read(self.root/'protocol-forward-f1-v1.json'):raise ValueError('Frozen protocol changed')
        token=uuid4().hex
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.okx_traded_comparison import safe_config
        safe={k:config[k] for k in SAFE_CONFIG if k in config}
        safe.update({k:config[k] for k in DEFAULT_CONFIG if k.startswith('okx_') and k in config})
        safe['model_settings']=safe_config(config)
        sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                 for base in ('tradingagents','cli') for p in (ROOT/base).rglob('*.py') if '.venv' not in p.parts}
        intent={'token':token,'instrument':instrument,'started_ms':utc_ms(),
                'config':safe,
                'git_commit':git('rev-parse','HEAD'),'git_branch':git('branch','--show-current'),
                'source_hashes':sources,'protocol':protocol}
        atomic(self.root/(token+'.intent.json'),intent)
        return token

    def register(self, directory, state, *, token=None, completed_ms=None, origin='HISTORICAL_IMPORT'):
        """Retry same token after interruption; sealed registration is authoritative."""
        directory=Path(directory)
        result=state.get('research_result') or {}
        if result: result=PerpetualResearchResult.model_validate(result).model_dump(mode='json')
        snapshot=state.get('market_snapshot') or {}
        if state.get('execution_enabled',False) is not False:raise ValueError('Execution must stay disabled')
        cutoff=snapshot.get('cutoff_ms')
        if cutoff is not None and type(cutoff) is not int:raise ValueError('Invalid market cutoff')
        if cutoff is None and snapshot.get('status')=='VALID':raise ValueError('Valid market data requires an explicit cutoff')
        if snapshot.get('status')=='VALID':
            from tradingagents.dataflows.vendors.okx import fingerprint, BARS
            for child in [snapshot,*snapshot.get('timeframes',{}).values()]:
                if child.get('snapshot_id')!=fingerprint(child):raise ValueError('Snapshot fingerprint mismatch')
                if any(r['ts']+BARS[child['interval']][1]>cutoff for r in child['records']):raise ValueError('Future candle in research')
        if result and result.get('snapshot_id') not in (None,snapshot.get('snapshot_id')):raise ValueError('Result/snapshot ownership mismatch')
        identity=result.get('research_id') or digest({'snapshot_id':snapshot.get('snapshot_id'),'cutoff':cutoff,'judgment':state.get('final_trade_decision'),'intent':token})
        rid=digest({'research_id':identity})
        with locked(self.root/'registry'):
            path=self.root/(rid+'.json')
            if path.exists():
                existing=self.load(rid)
                if existing.result_hash!=digest(result):raise ValueError('Research identity already sealed with different result')
                archived=read(self.root/(rid+'.artifacts.json'))
                if 'multitimeframe_snapshot.json' in archived and json.loads(archived['multitimeframe_snapshot.json'])!=snapshot:
                    raise ValueError('Research identity already sealed with different snapshot')
                return existing
            now=utc_ms()
            if token:
                intent=read(self.root/(token+'.intent.json'))
                if snapshot.get('symbol') not in (None,intent['instrument']):raise ValueError('Intent instrument mismatch')
                # Journal freezes completion and source bytes before final commit; retry cannot refresh clocks.
                journal=self.root/(token+'.journal.json')
                if journal.exists():
                    payload=read(journal)
                    if payload['result']!=result:raise ValueError('Recovery result changed')
                else:
                    if completed_ms is None:raise ValueError('Automatic research completion timestamp required')
                    payload={'completed_ms':completed_ms,'result':result,'state':{k:state[k] for k in ('research_result','market_snapshot','workflow_status','llm_status','llm_failure','data_quality','final_trade_decision','orderflow_evidence','market_checks_evidence','price_depth_evidence','event_evidence') if k in state},'artifacts':self._capture(directory)}
                    atomic(journal,payload)
                completed=payload['completed_ms'];artifacts=payload['artifacts']
                origin='PROSPECTIVE'
                protocol=intent['protocol']
                if now-completed>protocol['registration_max_delay_ms'] or (cutoff is not None and cutoff<intent['started_ms']) or completed>now:
                    origin='LATE_REGISTRATION'
                started=intent['started_ms']
            else:
                if origin not in {'HISTORICAL_IMPORT','TEST_REPLAY'}:raise ValueError('Prospective registration requires pre-run intent')
                completed=completed_ms or now;started=completed
                artifacts=self._capture(directory);protocol=read(PROTOCOL)
                intent={'config':{},'git_commit':git('rev-parse','HEAD'),'git_branch':git('branch','--show-current'),'source_hashes':{}}
            if 'research_result.json' in artifacts and json.loads(artifacts['research_result.json'])!=result:
                raise ValueError('Report/state result mismatch')
            if 'multitimeframe_snapshot.json' in artifacts:
                archived=json.loads(artifacts['multitimeframe_snapshot.json'])
                if archived!=snapshot:raise ValueError('Report/state snapshot mismatch')
                if snapshot.get('status')=='VALID':
                    from tempfile import TemporaryDirectory
                    from tradingagents.okx_signal_audit import freeze_report
                    # Validate the journal's captured bytes, not a later edit of the source report.
                    with TemporaryDirectory(prefix='tradingagents-f1-verify-') as temporary:
                        for name,content in artifacts.items():
                            (Path(temporary)/name).write_text(content,encoding='utf-8')
                        freeze_report(temporary)
            availability=max([snapshot.get('fetched_at_ms',cutoff or completed)]+[c.get('fetched_at_ms',cutoff or completed) for c in snapshot.get('timeframes',{}).values()])
            evidence={k:v for k,v in state.items() if k in ('data_quality','orderflow_evidence','market_checks_evidence','price_depth_evidence','event_evidence')}
            evidence['market_snapshot']=snapshot
            # Preserve effective views (including stale statuses) from completed report. No TTL extension.
            for name,content in artifacts.items():
                if name.endswith('_snapshot.json'):evidence[name]=json.loads(content)
            from importlib import import_module
            for name,module in [('orderflow_snapshot.json','okx_orderflow'),
                                ('market_checks_snapshot.json','okx_market_checks'),
                                ('price_depth_snapshot.json','okx_price_depth'),('events_snapshot.json','okx_events')]:
                raw=evidence.get(name,{}).get('original')
                if raw:
                    view=import_module('tradingagents.dataflows.vendors.'+module).evidence_view(raw,now)
                    evidence[name]['registration_view']=view
            if snapshot.get('derivatives'):
                from tradingagents.dataflows.vendors.okx_derivatives import evidence_view
                evidence['derivatives_at_registration']=evidence_view(snapshot['derivatives'],now)
            def acquired(value):
                if isinstance(value,dict):
                    for k,v in value.items():
                        if k=='fetched_at_ms' and type(v) is int:yield v
                        else:yield from acquired(v)
                elif isinstance(value,list):
                    for v in value:yield from acquired(v)
            availability=max([availability,*acquired(evidence)])
            stale=any(x in json.dumps(evidence) for x in ('STALE_DATA','RESEARCH_INVALID'))
            first=((max(cutoff or 0,completed,now,availability)+3599999)//3600000)*3600000
            clock=result.get('signal_timing') or {}
            expiry=clock.get('hypothesis_expires_ms')
            eligible=bool(cutoff is not None and origin=='PROSPECTIVE' and result.get('plan_valid') and state.get('data_quality')=='VALID' and state.get('workflow_status')=='COMPLETED' and not stale and expiry and expiry>first+3600000 and first==((cutoff+3599999)//3600000)*3600000)
            record=Registration(registration_id=rid,research_id=identity,origin=origin,
                instrument=snapshot.get('symbol',result.get('instrument',intent.get('instrument','UNKNOWN'))),
                timeframes=sorted(snapshot.get('timeframes',{})) or [snapshot.get('interval','UNKNOWN')],
                cutoff_ms=cutoff,started_ms=started,completed_ms=completed,registered_ms=now,data_available_ms=availability if cutoff is not None else None,
                first_bar_open_ms=first if cutoff is not None else None,first_check_ms=first+3600000 if cutoff is not None else None,expires_ms=expiry,git_commit=intent['git_commit'],git_branch=intent['git_branch'],source_hashes=intent['source_hashes'],
                protocol=protocol,protocol_hash=digest(protocol),config=intent['config'],action=result.get('proposed_action'),failure=state.get('llm_failure',{}),original_judgment=state.get('final_trade_decision',''),lifecycle_state=result.get('signal_state','WAIT'),
                workflow_status=state.get('workflow_status','NOT_STARTED'),llm_status=state.get('llm_status','NOT_STARTED'),
                data_quality=state.get('data_quality','DATA_UNAVAILABLE'),plan_valid=bool(result.get('plan_valid')),
                allow_new_risk=bool(result.get('allow_new_risk',state.get('allow_new_risk',False))),simulation_pool_eligible=eligible,
                status='AWAITING_F2' if eligible else 'ARCHIVED_ONLY',
                pair_id=digest({'exchange':'OKX','instrument':snapshot.get('symbol'),'cutoff':cutoff,'timeframes':sorted(snapshot.get('timeframes',{})) or [snapshot.get('interval')]}) if cutoff is not None else None,
                snapshot_id=snapshot.get('snapshot_id'),result_hash=digest(result),original_result=result,quality_evidence=evidence,
                report_directory=str(directory.resolve()),artifact_hashes={k:hashlib.sha256(v.encode()).hexdigest() for k,v in artifacts.items()})
            archive=self.root/(rid+'.artifacts.json')
            if archive.exists() and read(archive)!=artifacts:raise ValueError('Interrupted archive differs')
            atomic(archive,artifacts)
            record.content_hash=digest(record.model_dump(exclude={'content_hash'}))
            atomic(path,record.model_dump())
            return record

    def _capture(self,directory):
        result={}
        for name in ARTIFACTS:
            p=directory/name
            if p.exists():
                if p.stat().st_size>32*1024*1024:raise ValueError('Artifact size budget exceeded')
                result[name]=p.read_text(encoding='utf-8')
        if 'complete_report.md' not in result:raise ValueError('Completed report required')
        return result

    def load(self,rid):
        if len(rid)!=64 or any(c not in '0123456789abcdef' for c in rid):raise ValueError('Invalid registration id')
        raw=read(self.root/(rid+'.json'))
        if digest({k:v for k,v in raw.items() if k!='content_hash'})!=raw.get('content_hash'):raise ValueError('Registration fingerprint mismatch')
        record=Registration.model_validate(raw)
        artifacts=read(self.root/(rid+'.artifacts.json'))
        if {k:hashlib.sha256(v.encode()).hexdigest() for k,v in artifacts.items()}!=record.artifact_hashes:raise ValueError('Archived artifacts changed')
        return record

    def registrations(self):
        for p in sorted(self.root.glob('*.json')):
            if len(p.stem)==64:
                yield self.load(p.stem)

    def frozen_conditions(self,rid):
        record=self.load(rid)
        return {'registration_id':rid,'research_id':record.research_id,'pair_id':record.pair_id,
                'snapshot_id':record.snapshot_id,'protocol_hash':record.protocol_hash,
                'result_hash':record.result_hash,'first_bar_open_ms':record.first_bar_open_ms,'first_check_ms':record.first_check_ms,
                'expires_ms':record.expires_ms,'plan':record.original_result,
                'costs':record.protocol['costs'],'requires_revalidation':True,'execution_enabled':False}

    def pending(self):
        """F2 read interface; no polling or transitions performed."""
        for record in self.registrations():
            if record.status=='AWAITING_F2' and self.tracking_status(record.registration_id)=='QUEUED':yield record

    def verify_original(self,rid):
        record=self.load(rid)
        return {name:(Path(record.report_directory)/name).exists() and hashlib.sha256((Path(record.report_directory)/name).read_text(encoding='utf-8').encode()).hexdigest()==value for name,value in record.artifact_hashes.items()}

    def events(self,rid):
        self.load(rid)
        p=self.root/(rid+'.events.json')
        events=read(p) if p.exists() else []
        seen=set()
        for i,event in enumerate(events):
            if digest({k:v for k,v in event.items() if k!='event_hash'})!=event.get('event_hash') or event['registration_id']!=rid or event['event_id'] in seen or event['previous_hash']!=(digest(events[i-1]) if i else None):
                raise ValueError('Audit event chain invalid')
            seen.add(event['event_id'])
        return events

    def annotate(self,rid,event_id,note):
        """F1 audit annotations only. F2/F3 observation schemas deliberately not implemented."""
        self.load(rid)
        if not event_id or len(note)>4000:raise ValueError('Invalid annotation')
        with locked(self.root/'registry'):
            p=self.root/(rid+'.events.json')
            events=self.events(rid)
            for e in events:
                if e['event_id']==event_id:
                    if e['note']!=note:raise ValueError('Event identity conflict')
                    return e
            event={'event_id':event_id,'registration_id':rid,'kind':'ANNOTATION','note':note,'recorded_ms':utc_ms(),'previous_hash':digest(events[-1]) if events else None,'execution_enabled':False}
            event['event_hash']=digest(event)
            events.append(event);atomic(p,events);return event

    def tracking_status(self,rid):
        record=self.load(rid)
        for event in reversed(self.events(rid)):
            if event['kind']=='TRACKING_STATUS':return event['status']
        return 'QUEUED' if record.simulation_pool_eligible else 'ARCHIVED_ONLY'

    def update_tracking_status(self,rid,event_id,status,note):
        """Administrative audit updates only, never signal transitions or fills."""
        record=self.load(rid)
        if status not in {'QUEUED','DEFERRED','CANCELLED'} or not event_id or len(note)>4000:
            raise ValueError('Invalid administrative tracking status')
        if status=='QUEUED' and not record.simulation_pool_eligible:
            raise ValueError('An archived/invalid sample cannot be enabled by an event')
        with locked(self.root/'registry'):
            events=self.events(rid)
            for event in events:
                if event['event_id']==event_id:
                    if event.get('status')!=status or event.get('note')!=note:raise ValueError('Event identity conflict')
                    return event
            event={'event_id':event_id,'registration_id':rid,'kind':'TRACKING_STATUS',
                   'status':status,'note':note,'recorded_ms':utc_ms(),
                   'previous_hash':digest(events[-1]) if events else None,'execution_enabled':False}
            event['event_hash']=digest(event)
            events.append(event);atomic(self.root/(rid+'.events.json'),events)
            return event


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('list')
    verify=sub.add_parser('verify');verify.add_argument('registration_id')
    imp=sub.add_parser('import-report');imp.add_argument('directory')
    resume=sub.add_parser('resume');resume.add_argument('token');resume.add_argument('directory')
    args=parser.parse_args();registry=Registry(args.registry)
    if args.command=='list':
        output=[{'registration_id':r.registration_id,'research_id':r.research_id,'origin':r.origin,'action':r.action,'category':sample_category(r),'status':r.status,'pair_id':r.pair_id,'execution_enabled':False} for r in registry.registrations()]
    elif args.command=='verify':
        registry.load(args.registration_id);output=registry.verify_original(args.registration_id)
    elif args.command=='resume':
        payload=read(registry.root/(args.token+'.journal.json'))
        output=registry.register(args.directory,payload['state'],token=args.token,completed_ms=payload['completed_ms']).model_dump()
    else:
        directory=Path(args.directory)
        state={'market_snapshot':read(directory/'multitimeframe_snapshot.json'),
               'research_result':read(directory/'research_result.json'),'workflow_status':'IMPORTED_UNVERIFIED',
               'llm_status':'IMPORTED_UNVERIFIED'}
        state['data_quality']=state['market_snapshot']['status']
        output=registry.register(directory,state).model_dump()
    print(json.dumps(output,indent=2,allow_nan=False))

if __name__=='__main__':main()
