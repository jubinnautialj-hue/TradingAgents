"""F2.1 observation qualification, independent of risk/execution permission.

Only frozen research and its actual registration facts are inspected. No market
requests, TTL extensions, strategy changes, fills or account operations.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal
from datetime import datetime

from pydantic import Field, model_validator

from tradingagents.agents.perpetual_schemas import ResearchModel, PerpetualResearchResult
from tradingagents.dataflows.plan_lifecycle import HOUR, PlanTiming, evaluate_signal
from tradingagents.dataflows.vendors.okx import BARS, fingerprint, utc_ms, validate_symbol
from tradingagents.dataflows.files import locked
from tradingagents.forward_validation import atomic, digest, read

POLICY_VERSION = "forward-observation-v1"
POLICY = {
    "version": POLICY_VERSION,
    "scope": "OBSERVATION_ONLY_NOT_RISK_OR_SIMULATION_ADMISSION",
    "require": ["PROSPECTIVE_F1_V1", "COMPLETED_AVAILABLE", "VALID_ORIGINAL_PLAN_SCHEMA",
                "OPEN_CANDIDATE", "VALID_ARCHIVED_CORE_AND_EVIDENCE", "F1_POOL_ELIGIBLE",
                "COHERENT_IDENTITIES_PRICES_AND_C1_CLOCK", "REGISTERED_BEFORE_FIRST_CONFIRMATION_BAR"],
    "allow_new_risk": "Preserve both original fields. False is NOT an observation exclusion; mismatch is rejected.",
    "research_evidence": "Validate frozen quality at research/registration; do not renew TTL. Future entry evidence remains separate.",
    "expiry": "No new observation admission after expiry. An already enrolled chain may record calendar expiry only.",
    "legacy": "F1 records immutable; re-assess through separate audit. Old F2 chains read-only/paused, no silent migration.",
    "simulation_admitted": False,
    "execution_enabled": False,
}
POLICY_HASH = digest(POLICY)


class ObservationAssessment(ResearchModel):
    version: Literal["forward-observation-v1"] = POLICY_VERSION
    policy_hash: str = POLICY_HASH
    registration_id: str
    research_id: str
    registration_hash: str
    snapshot_id: str | None
    assessed_at_ms: int = Field(gt=0)
    research_completed: bool
    plan_valid: bool
    allow_new_risk: bool
    original_result_allow_new_risk: bool | None
    f1_simulation_pool_eligible: bool
    research_observation_qualified: bool
    observation_eligible: bool
    window_status: Literal["WAITING_FOR_FIRST_CLOSE", "OPEN", "EXPIRED", "UNDEFINED"]
    reasons: list[str]
    observation_only: Literal[True] = True
    simulation_admitted: Literal[False] = False
    requires_entry_revalidation: Literal[True] = True
    execution_enabled: Literal[False] = False

    @model_validator(mode="after")
    def consistency(self):
        if self.policy_hash!=POLICY_HASH:raise ValueError('Unknown observation policy hash')
        if self.observation_eligible and (not self.research_completed or not self.plan_valid
            or not self.f1_simulation_pool_eligible or not self.research_observation_qualified
            or self.window_status in {'EXPIRED','UNDEFINED'} or self.reasons):
            raise ValueError('Observation eligibility requires coherent positive research evidence')
        return self

    def semantic_value(self):
        return self.model_dump(exclude={"assessed_at_ms"})

    @property
    def event_id(self):
        return digest({"kind":"OBSERVATION_ELIGIBILITY","assessment":self.semantic_value()})


def _invalid_quality(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"status", "data_quality"} and item in ("STALE_DATA", "RESEARCH_INVALID"):
                return True
            if _invalid_quality(item): return True
    elif isinstance(value, list):
        return any(_invalid_quality(item) for item in value)
    return False


def assess(record, now_ms=None):
    """The caller must use Registry.load first: hash verification is not optional."""
    now = utc_ms() if now_ms is None else now_ms
    reasons = []
    completed = record.workflow_status == "COMPLETED" and record.llm_status == "AVAILABLE"
    if record.version != "forward-f1-v1" or record.origin != "PROSPECTIVE": reasons.append("NOT_PROSPECTIVE")
    if not completed: reasons.append("RESEARCH_NOT_COMPLETED")
    if record.action not in {"OPEN_LONG_CANDIDATE", "OPEN_SHORT_CANDIDATE"}: reasons.append("NOT_OPEN_CANDIDATE")
    if not record.plan_valid: reasons.append("PLAN_INVALID")
    if record.data_quality != "VALID": reasons.append("DATA_QUALITY_INVALID")
    if not record.simulation_pool_eligible or record.status != "AWAITING_F2": reasons.append("F1_POOL_NOT_ELIGIBLE")
    if record.first_bar_open_ms is None or record.first_check_ms is None: reasons.append("MISSING_FORWARD_CLOCK")
    if now < record.registered_ms: reasons.append("ASSESSMENT_PRECEDES_REGISTRATION")
    if record.execution_enabled is not False: reasons.append("EXECUTION_MUST_BE_DISABLED")
    raw = record.original_result
    evidence = record.quality_evidence
    core = evidence.get("market_snapshot") or evidence.get("multitimeframe_snapshot.json")
    source_ids={record.snapshot_id}
    if isinstance(core,dict) and isinstance(core.get('timeframes'),dict):
        source_ids.update(child.get('snapshot_id') for child in core['timeframes'].values() if isinstance(child,dict))
    if raw.get("allow_new_risk") is not record.allow_new_risk: reasons.append("RISK_FIELD_MISMATCH")
    try:
        result = PerpetualResearchResult.model_validate(raw)
        if not result.plan_valid: reasons.append("ORIGINAL_PLAN_INVALID")
        if result.research_id != record.research_id or result.snapshot_id != record.snapshot_id or result.instrument != record.instrument:
            reasons.append("RESULT_OWNERSHIP_MISMATCH")
        if result.proposed_action.value != record.action or result.plan_valid != record.plan_valid or result.data_quality != record.data_quality:
            reasons.append("REGISTERED_RESULT_MISMATCH")
        if result.research_cutoff_utc is None:raise ValueError('No reliable research cutoff')
        stamp=datetime.fromisoformat(result.research_cutoff_utc.replace('Z','+00:00'))
        if stamp.tzinfo is None or round(stamp.timestamp()*1000)!=record.cutoff_ms:
            reasons.append('INVALID_RESEARCH_UTC_CUTOFF')
        clock = PlanTiming.model_validate(result.signal_timing)
        first = ((clock.cutoff_ms + HOUR - 1) // HOUR) * HOUR
        if (record.cutoff_ms != clock.cutoff_ms or record.first_bar_open_ms != first
            or first < max(record.completed_ms,record.registered_ms,record.data_available_ms or 0)
            or record.first_check_ms != first + HOUR or record.expires_ms != clock.hypothesis_expires_ms):
            reasons.append("FROZEN_CLOCK_MISMATCH")
        if record.expires_ms is None or record.expires_ms <= record.first_check_ms:
            reasons.append("NO_CONFIRMATION_WINDOW")
        if result.plan_valid:
            direction = "LONG" if result.proposed_action.value == "OPEN_LONG_CANDIDATE" else "SHORT"
            plan={"direction":direction,"entry":result.entry_reference.model_dump(),
                  "stop":result.stop_loss.model_dump(),"target":result.take_profit[0].model_dump()}
            if any(level.source_snapshot_id not in source_ids for level in [result.entry_reference,result.stop_loss,*result.take_profit]):
                reasons.append('PRICE_EVIDENCE_OWNERSHIP_MISMATCH')
            evaluate_signal(plan,clock.model_dump(),[],clock.cutoff_ms)
    except (ValueError,KeyError,TypeError,ArithmeticError):
        reasons.append("INVALID_ORIGINAL_PLAN_OR_CLOCK")
    evidence = record.quality_evidence
    from tradingagents import registration_revalidation as admission
    extended=admission.KEY in evidence
    if extended and not admission.verify(record): reasons.append("INVALID_REGISTRATION_REVALIDATION_PROOF")
    if _invalid_quality(evidence) and not (extended and admission.verify(record)):
        reasons.append("FROZEN_EVIDENCE_INVALID_OR_STALE")
    core = evidence.get("market_snapshot") or evidence.get("multitimeframe_snapshot.json")
    try:
        validate_symbol(record.instrument)
        if not core or not core.get("multi_timeframe") or set(core.get("timeframes",{})) != {"1h","4h","1d"}:
            raise ValueError("Multi-timeframe evidence required")
        if core.get('symbol')!=record.instrument or core.get("status")!="VALID" or core.get("snapshot_id")!=record.snapshot_id or fingerprint(core)!=record.snapshot_id:
            raise ValueError("Core evidence fingerprint/quality mismatch")
        for child in [core,*core['timeframes'].values()]:
            if child['cutoff_ms']!=record.cutoff_ms or child['status']!='VALID' or child['snapshot_id']!=fingerprint(child):
                raise ValueError("Child evidence mismatch")
            step=BARS[child['interval']][1]
            stamps=[r['ts'] for r in child['records']]
            if not stamps or len(set(stamps))!=len(stamps) or any(b-a!=step for a,b in zip(stamps,stamps[1:])):
                raise ValueError("Frozen evidence coverage gap")
            if any(ts+step>record.cutoff_ms for ts in stamps): raise ValueError("Future research evidence")
    except (ValueError,KeyError,TypeError,AttributeError):
        reasons.append("INVALID_FROZEN_MARKET_EVIDENCE")
    qualified = not reasons
    window = "UNDEFINED"
    if record.expires_ms is not None and record.first_check_ms is not None:
        window = "EXPIRED" if now >= record.expires_ms else ("WAITING_FOR_FIRST_CLOSE" if now < record.first_check_ms else "OPEN")
    active = qualified and window != "EXPIRED"
    if qualified and window == "EXPIRED": reasons.append("OBSERVATION_WINDOW_EXPIRED")
    return ObservationAssessment(
        registration_id=record.registration_id,research_id=record.research_id,registration_hash=record.content_hash,
        snapshot_id=record.snapshot_id,assessed_at_ms=now,research_completed=completed,
        plan_valid=record.plan_valid,allow_new_risk=record.allow_new_risk,
        original_result_allow_new_risk=raw.get("allow_new_risk"),f1_simulation_pool_eligible=record.simulation_pool_eligible,
        research_observation_qualified=qualified,observation_eligible=active,window_status=window,reasons=sorted(set(reasons)))


class EligibilityAudit:
    """Independent versioned events; never write F1 records or old F2 chains."""
    def __init__(self, output):
        self.root=Path(output)/'observation_eligibility'
        self.root.mkdir(parents=True,exist_ok=True)
        self.policy_path=self.root/(POLICY_HASH+'.policy.json')
        with locked(self.root/'policy'):
            if self.policy_path.exists() and read(self.policy_path)!=POLICY: raise ValueError('Observation policy changed')
            if not self.policy_path.exists(): atomic(self.policy_path,POLICY)

    def path(self,rid):
        if len(rid)!=64 or any(c not in '0123456789abcdef' for c in rid):raise ValueError('Invalid registration id')
        return self.root/(rid+'.events.json')

    def events(self,rid):
        p=self.path(rid)
        value=read(p) if p.exists() else []
        seen=set()
        for i,event in enumerate(value):
            assessment=ObservationAssessment.model_validate(event['assessment'])
            if (event.get('kind')!='OBSERVATION_ELIGIBILITY' or event.get('policy_version')!=POLICY_VERSION
                or event.get('recorded_ms')!=assessment.assessed_at_ms or event['event_id']!=assessment.event_id or event['event_id'] in seen
                or assessment.registration_id!=rid or assessment.policy_hash!=POLICY_HASH
                or event['previous_hash']!=(value[i-1]['event_hash'] if i else None)
                or event['event_hash']!=digest({k:v for k,v in event.items() if k!='event_hash'})):
                raise ValueError('Observation eligibility audit invalid')
            seen.add(event['event_id'])
        return value

    def record(self, assessment):
        with locked(self.path(assessment.registration_id)):
            events=self.events(assessment.registration_id)
            for event in events:
                if event['event_id']==assessment.event_id:return event
            if events and assessment.assessed_at_ms<events[-1]['recorded_ms']:raise ValueError('Observation audit clock rollback')
            event={'kind':'OBSERVATION_ELIGIBILITY','event_id':assessment.event_id,
                   'recorded_ms':assessment.assessed_at_ms,'policy_version':POLICY_VERSION,
                   'policy_hash':POLICY_HASH,'assessment':assessment.model_dump(),
                   'previous_hash':events[-1]['event_hash'] if events else None,'execution_enabled':False}
            event['event_hash']=digest(event)
            events.append(event);atomic(self.path(assessment.registration_id),events)
            return event
