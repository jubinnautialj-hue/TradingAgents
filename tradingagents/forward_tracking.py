"""F2 bounded public-market lifecycle observation. No LLM, account, fills or PnL.

F1 archives and C.1 are read-only. Events live in a separate F2 namespace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents import observation_eligibility as observation_policy
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.plan_lifecycle import HOUR, PlanTiming, SignalResult, evaluate_signal
from tradingagents.dataflows.vendors import okx
from tradingagents.forward_validation import (
    FutureBarObservation, Registry, atomic, digest, read, sample_category,
)

VERSION = "forward-f2.1-v1"
CODE_HASH = digest({"tracking":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "observation_policy":hashlib.sha256(Path(observation_policy.__file__).read_bytes()).hexdigest()})
TERMINAL = {"EXPIRED", "INVALIDATED"}


class LegacyTrackingChain(ValueError):
    """Old F2 evidence remains read-only until explicit migration is approved."""


class BudgetExhausted(RuntimeError):
    pass


class ScanLimits(ResearchModel):
    max_requests: int = Field(default=8, ge=1, le=100)
    max_candidates: int = Field(default=20, ge=1, le=200)
    max_records: int = Field(default=200, ge=1, le=2000)
    max_pages: int = Field(default=3, ge=1, le=5)
    max_seconds: int = Field(default=180, ge=5, le=600)
    delivery_grace_seconds: int = Field(default=90, ge=1, le=300)
    execution_enabled: Literal[False] = False


class PublicBudgetClient(okx.OKXPublicClient):
    """Reuse the GET allowlist, retry, timeout and pacing; cap logical requests.

    Each inherited GET has at most three HTTP attempts. The CLI supervisor adds
    a hard wall-clock bound; deadline checks alone cannot interrupt a socket.
    """
    def __init__(self, limits):
        self.limits = limits
        self.calls = 0
        self.deadline = time.monotonic() + limits.max_seconds

    def get(self, path, params=None):
        if self.calls >= self.limits.max_requests or time.monotonic() >= self.deadline:
            raise BudgetExhausted("F2 public request/time budget exhausted")
        self.calls += 1
        return super().get(path, params)


class FrozenCandidate(ResearchModel):
    registration_id: str
    research_id: str
    registration_hash: str
    protocol_hash: str
    snapshot_id: str
    result_hash: str
    instrument: str
    direction: Literal["LONG", "SHORT"]
    plan: dict
    original_result: dict = Field(default_factory=dict)
    clock: PlanTiming
    first_bar_open_ms: int
    first_check_ms: int
    registered_ms: int
    namespace: Literal["PRODUCTION", "FIXTURE"] = "PRODUCTION"
    observation_policy_hash: str | None = None
    original_allow_new_risk: bool = False
    observation_only: Literal[True] = True
    simulation_admitted: Literal[False] = False
    execution_enabled: Literal[False] = False

    @model_validator(mode="after")
    def alignment(self):
        if self.namespace=="PRODUCTION" and self.observation_policy_hash!=observation_policy.POLICY_HASH:
            raise ValueError("Production observation requires a versioned qualification policy")
        expected = ((self.clock.cutoff_ms + HOUR - 1) // HOUR) * HOUR
        if self.first_bar_open_ms != expected or self.first_bar_open_ms < self.registered_ms:
            raise ValueError("Original first confirmation bar was missed or precedes registration")
        if self.first_check_ms != expected + HOUR:
            raise ValueError("First check must follow the eligible bar close")
        if self.clock.hypothesis_expires_ms != expected + self.clock.confirmation_bars * HOUR:
            raise ValueError("Frozen confirmation window mismatch")
        if self.plan.get("direction") != self.direction:
            raise ValueError("Direction differs from original plan")
        evaluate_signal(self.plan, self.clock.model_dump(), [], self.clock.cutoff_ms)
        return self


class TrackingEvent(ResearchModel):
    version: Literal["forward-f2.1-v1"] = VERSION
    event_id: str
    registration_id: str
    research_id: str
    namespace: Literal["PRODUCTION", "FIXTURE"]
    protocol_hash: str
    snapshot_id: str
    result_hash: str
    registration_hash: str
    direction: Literal["LONG", "SHORT"]
    observation_only: Literal[True] = True
    simulation_admitted: Literal[False] = False
    allow_new_risk: Literal[False] = False
    observation_policy_hash: str = observation_policy.POLICY_HASH
    kind: Literal["BAR_OBSERVATION", "STATE_TRANSITION", "WAIT", "PAUSED", "CLOCK_EXPIRY"]
    recorded_ms: int = Field(gt=0)
    market_cutoff_ms: int = Field(gt=0)
    source: str
    evidence_fingerprint: str
    payload: dict
    previous_hash: str | None
    event_hash: str
    execution_enabled: Literal[False] = False


class ConditionTrigger(ResearchModel):
    """F3 input contract. This is NEVER a fill or a settlement result."""
    version: Literal["forward-observation-condition-v1"] = "forward-observation-condition-v1"
    observation_policy_version: Literal["forward-observation-v1"] = observation_policy.POLICY_VERSION
    observation_policy_hash: str = observation_policy.POLICY_HASH
    observation_only: Literal[True] = True
    simulation_admitted: Literal[False] = False
    requires_separate_simulation_admission: Literal[True] = True
    event_id: str
    research_id: str
    registration_id: str
    protocol_hash: str
    snapshot_id: str
    result_hash: str
    direction: Literal["LONG", "SHORT"]
    signal_confirmed_ms: int
    observed_available_ms: int
    decision_available_ms: int
    next_tradable_ms: int
    hypothesis_expires_ms: int
    original_conditions: dict
    confirmation_bar: dict
    evidence_fingerprint: str
    previous_state: str
    new_state: Literal["ENTRY_ELIGIBLE"] = "ENTRY_ELIGIBLE"
    collection_mode: Literal["CONTEMPORANEOUS", "BACKFILL"]
    data_quality: Literal["VALID"] = "VALID"
    continuity: Literal["COMPLETE"] = "COMPLETE"
    requires_revalidation: Literal[True] = True
    is_simulated_fill: Literal[False] = False
    allow_new_risk: Literal[False] = False
    execution_enabled: Literal[False] = False

    @model_validator(mode="after")
    def clock(self):
        if not self.signal_confirmed_ms <= self.observed_available_ms <= self.decision_available_ms < self.hypothesis_expires_ms:
            raise ValueError("Condition observation expired or precedes confirmation")
        if self.next_tradable_ms != self.signal_confirmed_ms:
            raise ValueError("C.1 next-open boundary changed")
        if self.confirmation_bar["ts"] + HOUR != self.signal_confirmed_ms:
            raise ValueError("Wrong confirmation bar")
        return self


def exclusion_reasons(record):
    """F2.1 observation rules; never translate observation into risk permission."""
    return observation_policy.assess(record,okx.utc_ms()).reasons


def candidate_from_record(record, assessment=None, *, existing_expiry=False):
    checked=observation_policy.assess(record,okx.utc_ms())
    if assessment is not None and assessment.semantic_value()!=checked.semantic_value():
        raise ValueError("Observation facts changed; qualification must be recomputed")
    assessment=checked
    if not assessment.observation_eligible and not (existing_expiry and assessment.research_observation_qualified and assessment.window_status=="EXPIRED"):
        raise ValueError("F2 observation excluded: " + ",".join(assessment.reasons))
    r = record.original_result
    direction = "LONG" if record.action == "OPEN_LONG_CANDIDATE" else "SHORT"
    plan = {"direction": direction, "entry": r["entry_reference"],
            "stop": r["stop_loss"], "target": r["take_profit"][0]}
    return FrozenCandidate(
        registration_id=record.registration_id, research_id=record.research_id,
        registration_hash=record.content_hash, protocol_hash=record.protocol_hash,
        snapshot_id=record.snapshot_id, result_hash=record.result_hash,
        instrument=okx.validate_symbol(record.instrument), direction=direction, plan=plan, original_result=r,
        observation_policy_hash=observation_policy.POLICY_HASH,original_allow_new_risk=record.allow_new_risk,
        clock=PlanTiming.model_validate(r["signal_timing"]),
        first_bar_open_ms=record.first_bar_open_ms, first_check_ms=record.first_check_ms,
        registered_ms=record.registered_ms,
    )


class EventStore:
    """One atomic chain file per registration. It is the only processing cursor.

    Appends never rewrite prior event objects. A killed writer leaves either the
    previous complete chain or the new complete chain; no separate cursor can
    commit ahead of its evidence. All writers hold the registration lock.
    """
    def __init__(self, root, namespace="PRODUCTION", delivery_grace_seconds=90):
        if namespace not in {"PRODUCTION", "FIXTURE"}: raise ValueError("Invalid namespace")
        self.root = Path(root) / namespace.lower()
        self.root.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.policy = {"version":VERSION,"delivery_grace_seconds":delivery_grace_seconds,"observation_policy_hash":observation_policy.POLICY_HASH}

    def path(self, rid):
        if len(rid) != 64 or any(c not in "0123456789abcdef" for c in rid):
            raise ValueError("Invalid registration id")
        return self.root / (rid + ".events.json")

    def load(self, candidate):
        path = self.path(candidate.registration_id)
        if not path.exists(): return []
        value = read(path)
        if value.get('version')!=VERSION or 'observation_policy_hash' not in value.get('policy',{}):
            raise LegacyTrackingChain('Legacy F2 chain retained read-only; explicit migration required')
        if value["candidate_hash"] != digest(candidate.model_dump()) or value["namespace"] != self.namespace or value.get("policy") != self.policy or value.get("code_sha256") != CODE_HASH:
            raise ValueError("Frozen candidate identity changed")
        events, seen = value["events"], set()
        bars = []
        previous_state = "PENDING_BREAKOUT"
        for i, item in enumerate(events):
            event = TrackingEvent.model_validate(item)
            if (event.namespace != self.namespace or event.registration_id != candidate.registration_id or event.research_id != candidate.research_id
                or event.protocol_hash!=candidate.protocol_hash or event.snapshot_id!=candidate.snapshot_id
                or event.result_hash!=candidate.result_hash or event.registration_hash!=candidate.registration_hash
                or event.direction!=candidate.direction or event.observation_policy_hash!=observation_policy.POLICY_HASH):
                raise ValueError("Cross-candidate event ownership")
            if event.event_id in seen or event.previous_hash != (events[i-1]["event_hash"] if i else None):
                raise ValueError("Duplicate or conflicting event chain")
            if digest({k:v for k,v in item.items() if k != "event_hash"}) != event.event_hash:
                raise ValueError("F2 event fingerprint mismatch")
            if event.evidence_fingerprint != digest(event.payload):
                raise ValueError("F2 evidence fingerprint mismatch")
            if i and event.recorded_ms < events[i-1]["recorded_ms"]:
                raise ValueError("Event time moved backward")
            seen.add(event.event_id)
            if event.kind == "BAR_OBSERVATION":
                observation = event.payload["observation"]
                FutureBarObservation.model_validate(observation)
                row = event.payload["bar"]
                expected = candidate.first_bar_open_ms + len(bars) * HOUR
                if row["ts"] != expected or observation["bar_open_ms"] != expected:
                    raise ValueError("Missing/duplicate/out-of-order processed bar")
                if observation["bar_close_ms"] > event.market_cutoff_ms or observation["bar_open_ms"] < candidate.first_bar_open_ms:
                    raise ValueError("Future or pre-registration event")
                if observation["bar_fingerprint"] != digest(row):
                    raise ValueError("Candle evidence changed")
                bars.append(row)
            elif event.kind in {"STATE_TRANSITION", "CLOCK_EXPIRY"}:
                SignalResult.model_validate(event.payload["signal"])
                state = event.payload["signal"]["state"]
                if event.payload["previous_state"] != previous_state or previous_state in TERMINAL:
                    raise ValueError("Invalid/terminal lifecycle transition")
                as_of = event.payload["signal_as_of_ms"]
                checked = evaluate_signal(candidate.plan, candidate.clock.model_dump(), bars, as_of).model_dump()
                if checked != event.payload["signal"]:
                    raise ValueError("Signal does not match frozen C.1 replay")
                if "f3_condition" in event.payload:
                    trigger=ConditionTrigger.model_validate(event.payload['f3_condition'])
                    expected_conditions={'plan':candidate.plan,'signal_timing':candidate.clock.model_dump(),'original_result':candidate.original_result}
                    if (self.namespace!='PRODUCTION' or state!='ENTRY_ELIGIBLE'
                        or trigger.registration_id!=candidate.registration_id or trigger.research_id!=candidate.research_id
                        or trigger.observation_policy_hash!=observation_policy.POLICY_HASH
                        or trigger.protocol_hash!=candidate.protocol_hash or trigger.snapshot_id!=candidate.snapshot_id
                        or trigger.result_hash!=candidate.result_hash or trigger.direction!=candidate.direction
                        or trigger.original_conditions!=expected_conditions or trigger.confirmation_bar!=bars[-1]
                        or trigger.signal_confirmed_ms!=checked['retest_confirmed_ms']
                        or trigger.next_tradable_ms!=checked['next_tradable_ms']
                        or trigger.hypothesis_expires_ms!=candidate.clock.hypothesis_expires_ms
                        or trigger.evidence_fingerprint!=digest(bars)
                        or any(e['payload'].get('collection_mode')!='CONTEMPORANEOUS' for e in events[:i] if e['kind']=='BAR_OBSERVATION')):
                        raise ValueError('Condition event does not match original evidence/namespace')
                previous_state = state
        return events

    def append(self, candidate, events, kind, payload, recorded, cutoff, identity):
        event_id = digest({"registration_id":candidate.registration_id,"kind":kind,"identity":identity})
        for event in events:
            if event["event_id"] == event_id:
                if event["payload"] != payload: raise ValueError("Conflicting idempotency key")
                return False
        event = dict(version=VERSION,event_id=event_id,registration_id=candidate.registration_id,
                     research_id=candidate.research_id,namespace=self.namespace,kind=kind,
                     protocol_hash=candidate.protocol_hash,snapshot_id=candidate.snapshot_id,
                     result_hash=candidate.result_hash,registration_hash=candidate.registration_hash,direction=candidate.direction,
                     recorded_ms=recorded,market_cutoff_ms=cutoff,source=("OKX API V5 public REST /api/v5/market/history-candles" if kind in {"BAR_OBSERVATION","STATE_TRANSITION"} else "F2 scheduler clock; pinned OKX /api/v5/public/time"),
                     evidence_fingerprint=digest(payload),payload=payload,
                     previous_hash=events[-1]["event_hash"] if events else None,execution_enabled=False)
        event["event_hash"] = digest(event)
        TrackingEvent.model_validate(event)
        events.append(event)
        return True

    def commit(self, candidate, events):
        atomic(self.path(candidate.registration_id), {
            "version":VERSION,"namespace":self.namespace,"policy":self.policy,"candidate_hash":digest(candidate.model_dump()),
            "events":events,"code_sha256":CODE_HASH,"execution_enabled":False,
        })

    def triggers(self, candidate):
        if candidate.namespace != "PRODUCTION" or self.namespace != "PRODUCTION":
            return []
        result = []
        for event in self.load(candidate):
            trigger = event["payload"].get("f3_condition")
            if trigger:
                result.append(ConditionTrigger.model_validate(trigger))
        return result


def normalize_rows(rows, start, end):
    """Keep original nine-field units. Deduplicate identical overlap, never conflicts."""
    unique, duplicates, excluded = {}, 0, 0
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 9:
            raise ValueError("Malformed V5 candle")
        stamp = int(row[0])
        if stamp in unique:
            if list(row) != unique[stamp]: raise ValueError("Conflicting duplicate candle")
            duplicates += 1
        else:
            unique[stamp] = list(row)
    selected = []
    for stamp, row in unique.items():
        if stamp < start or stamp >= end or str(row[8]) != "1" or stamp+HOUR > end:
            excluded += 1
        else:
            selected.append(row)
    count = (end-start)//HOUR
    if count <= 0: return [], {"duplicate_rows":duplicates,"excluded_rows":excluded}
    frame, notes = okx.validate_candles(selected,"1h",end,count,count,max_lag_bars=0)
    if list(frame["ts"]) != list(range(start,end,HOUR)):
        raise ValueError("Requested candle coverage incomplete")
    bars = [{"ts":int(r["ts"]),"open":float(r["Open"]),"high":float(r["High"]),
             "low":float(r["Low"]),"close":float(r["Close"]),"confirm":"1",
             "volume_contracts":float(r["VolumeContracts"]),"volume_base":float(r["Volume"]),
             "volume_quote":float(r["VolumeQuote"])} for r in frame.to_dict("records")]
    return bars, {"duplicate_rows":duplicates,"excluded_rows":excluded,"notes":notes}


class Collector:
    """Bounded incremental fetch, shared across candidates within this scan.

    Validated observations in the event store are the durable cache. An
    interrupted, uncommitted fetch may be repeated, but can never create a fill.
    """
    def __init__(self, client, limits):
        self.client, self.limits = client, limits
        self.cache = {}

    def get(self, symbol, start, end):
        key = (symbol,start,end)
        if key in self.cache: return self.cache[key]
        rows, cursor = [], end
        for page_number in range(self.limits.max_pages):
            page = self.client.get("/api/v5/market/history-candles", {
                "instId":okx.validate_symbol(symbol),"bar":okx.BARS["1h"][0],"after":str(cursor),"limit":"100"})
            if not page: break
            try:
                if any(not isinstance(row,(list,tuple)) or len(row)!=9 for row in page):
                    raise ValueError("Malformed V5 candle page")
                stamps = [int(row[0]) for row in page]
            except (TypeError,IndexError) as exc:
                raise ValueError("Malformed candle timestamp") from None
            if min(stamps) >= cursor: raise ValueError("Pagination made no progress")
            rows.extend(page)
            if min(stamps) <= start: break
            cursor = min(stamps)
        bars, quality = normalize_rows(rows,start,end)
        value = {"bars":bars,"quality":quality,"source":"OKX API V5 public REST",
                 "fetched_at_ms":okx.utc_ms(),"requested_start_ms":start,"requested_end_ms":end,
                 "volume_units":{"volume_contracts":"contracts","volume_base":"base coin","volume_quote":"USDT"}}
        value["fingerprint"] = digest(value)
        self.cache[key] = value
        return value


def state_of(events):
    state = "PENDING_BREAKOUT"
    for event in events:
        if event["kind"] in {"STATE_TRANSITION","CLOCK_EXPIRY"}:
            state = event["payload"]["signal"]["state"]
    return state


def process_candidate(candidate, store, collector, server_ms, limits):
    """One locked transaction. Also used with explicit FIXTURE namespace in tests."""
    if candidate.namespace != store.namespace or store.policy["delivery_grace_seconds"]!=limits.delivery_grace_seconds:
        raise ValueError("Namespace or frozen observation policy mismatch")
    with locked(store.path(candidate.registration_id), wait=False) as acquired:
        if not acquired: return {"status":"BUSY","registration_id":candidate.registration_id}
        events = store.load(candidate)
        initial_count = len(events)
        state = state_of(events)
        if state in TERMINAL:
            return {"status":state,"operational_status":"TERMINAL","new_events":0,"registration_id":candidate.registration_id}
        bars = [e["payload"]["bar"] for e in events if e["kind"]=="BAR_OBSERVATION"]
        recorded = okx.utc_ms()
        if recorded < candidate.registered_ms or abs(recorded-server_ms)>30_000:
            raise ValueError("Server/local clock mismatch or local clock rollback")
        if events and recorded < events[-1]["recorded_ms"]:
            raise ValueError("Tracking clock moved backward")
        expiry = candidate.clock.hypothesis_expires_ms
        if server_ms >= expiry:
            signal = evaluate_signal(candidate.plan,candidate.clock.model_dump(),bars,server_ms).model_dump()
            store.append(candidate,events,"CLOCK_EXPIRY",{
                "previous_state":state,"signal":signal,"signal_as_of_ms":server_ms,
                "coverage":"COMPLETE" if len(bars)==candidate.clock.confirmation_bars else "UNKNOWN_UNOBSERVED",
                "reason":"Calendar expiry only; missing observation history is NOT proof of no prior trigger"},
                recorded,server_ms,"terminal-expiry")
        else:
            start = candidate.first_bar_open_ms+len(bars)*HOUR
            end = min(server_ms//HOUR*HOUR,expiry)
            if start >= end:
                store.append(candidate,events,"WAIT",{"state":state,"reason":"WAITING_FOR_NEXT_CLOSED_BAR","next_close_ms":start+HOUR},
                             recorded,server_ms,start)
            else:
                try:
                    batch = collector.get(candidate.instrument,start,end)
                except (ValueError,okx.DataQualityError,okx.VendorUnavailableError,BudgetExhausted) as exc:
                    store.append(candidate,events,"PAUSED",{"state":state,"reason":"DATA_GAP_OR_UNAVAILABLE","error_type":type(exc).__name__,
                                 "requested_start_ms":start,"requested_end_ms":end},recorded,server_ms,
                                 {"start":start,"end":end,"error_type":type(exc).__name__})
                else:
                    fetched = batch["fetched_at_ms"]
                    if fetched < candidate.registered_ms: raise ValueError("Pre-registration acquisition")
                    recorded = max(recorded,fetched,okx.utc_ms())
                    if fetched >= expiry:
                        signal=evaluate_signal(candidate.plan,candidate.clock.model_dump(),bars,fetched).model_dump()
                        store.append(candidate,events,"CLOCK_EXPIRY",{
                            "previous_state":state,"signal":signal,"signal_as_of_ms":fetched,
                            "coverage":"UNKNOWN_UNOBSERVED","reason":"Fetch completed after expiry; no historical trigger promoted",
                            "late_batch_fingerprint":batch["fingerprint"]},recorded,server_ms,"terminal-expiry")
                        store.commit(candidate,events)
                        return {"registration_id":candidate.registration_id,"status":"EXPIRED","operational_status":"TERMINAL","new_events":len(events)-initial_count}

                    for row in batch["bars"]:
                        close_ms = row["ts"]+HOUR
                        mode = "BACKFILL" if fetched-close_ms>limits.delivery_grace_seconds*1000 else "CONTEMPORANEOUS"
                        observation = FutureBarObservation(registration_id=candidate.registration_id,
                            bar_open_ms=row["ts"],bar_close_ms=close_ms,available_ms=fetched,
                            recorded_ms=recorded,bar_fingerprint=digest(row))
                        if row["ts"] < candidate.first_bar_open_ms or close_ms>server_ms:
                            raise ValueError("Future/pre-registration candle")
                        store.append(candidate,events,"BAR_OBSERVATION",{
                            "bar":row,"observation":observation.model_dump(),"collection_mode":mode,
                            "batch_fingerprint":batch["fingerprint"],"quality":batch["quality"],
                            "volume_units":batch["volume_units"],"continuity":"COMPLETE"},recorded,server_ms,row["ts"])
                        bars.append(row)
                        # C.1 sees exactly the prefix available at this candle close.
                        # A late observation records historical confirmation time, not an on-time fill.
                        signal = evaluate_signal(candidate.plan,candidate.clock.model_dump(),bars,close_ms).model_dump()
                        if signal["state"] != state:
                            payload = {"previous_state":state,"signal":signal,"signal_as_of_ms":close_ms,
                                       "collection_mode":mode,"confirmation_bar":row,
                                       "continuity":"COMPLETE","batch_fingerprint":batch["fingerprint"]}
                            all_live = all(e["payload"]["collection_mode"]=="CONTEMPORANEOUS" for e in events if e["kind"]=="BAR_OBSERVATION")
                            if signal["state"]=="ENTRY_ELIGIBLE":
                                payload["condition_only"] = True
                                payload["f3_exportable"] = bool(candidate.namespace=="PRODUCTION" and all_live and recorded<expiry and recorded-close_ms<=limits.delivery_grace_seconds*1000)
                                if payload["f3_exportable"]:
                                    trigger = ConditionTrigger(event_id=digest({"rid":candidate.registration_id,"retest":signal["retest_confirmed_ms"]}),
                                        research_id=candidate.research_id,registration_id=candidate.registration_id,
                                        protocol_hash=candidate.protocol_hash,snapshot_id=candidate.snapshot_id,result_hash=candidate.result_hash,
                                        direction=candidate.direction,signal_confirmed_ms=signal["retest_confirmed_ms"],
                                        observed_available_ms=fetched,decision_available_ms=recorded,next_tradable_ms=signal["next_tradable_ms"],hypothesis_expires_ms=expiry,
                                        original_conditions={"plan":candidate.plan,"signal_timing":candidate.clock.model_dump(),"original_result":candidate.original_result},confirmation_bar=row,evidence_fingerprint=digest(bars),
                                        previous_state=state,collection_mode=mode)
                                    payload["f3_condition"] = trigger.model_dump()
                            store.append(candidate,events,"STATE_TRANSITION",payload,recorded,server_ms,
                                         {"bar":row["ts"],"new_state":signal["state"]})
                            state = signal["state"]
                        if state in TERMINAL: break
        if len(events) != initial_count:
            store.commit(candidate,events)
        last=events[-1] if events else None
        operation='PAUSED' if last and last['kind']=='PAUSED' else ('WAITING_FOR_CLOSED_BAR' if last and last['kind']=='WAIT' else ('TERMINAL' if state_of(events) in TERMINAL else 'ADVANCED'))
        return {"registration_id":candidate.registration_id,"status":state_of(events),
                "operational_status":operation,"pause_reason":last['payload'].get('reason') if operation=='PAUSED' else None,
                "last_successful_bar_ms":bars[-1]['ts'] if bars else None,
                "new_events":len(events)-initial_count,"processed_bars":sum(e["kind"]=="BAR_OBSERVATION" for e in events),
                "execution_enabled":False}


def _scan(registry_root, output, limits=None, *, client=None, probe_market=None):
    limits = limits or ScanLimits()
    registry, store = Registry(registry_root), EventStore(output,delivery_grace_seconds=limits.delivery_grace_seconds)
    eligibility_audit=observation_policy.EligibilityAudit(output)
    assessments=[]
    client = client or PublicBudgetClient(limits)
    collector = Collector(client,limits)
    started = okx.utc_ms()
    records, excluded, errors = [], [], []
    cursor_path=Path(output)/'scan_cursor.json'
    cursor=read(cursor_path) if cursor_path.exists() else {}
    checked_count=0
    last_record=cursor.get('last_record_id','')
    # Validate each F1 record independently; one corrupt record cannot hide the others.
    record_paths=[p for p in sorted(Path(registry_root).glob('*.json')) if len(p.stem)==64]
    ordered_paths=[p for p in record_paths if p.stem>last_record]+[p for p in record_paths if p.stem<=last_record]
    for path in ordered_paths[:limits.max_records]:
        if time.monotonic() >= getattr(client,'deadline',float('inf')): break
        checked_count+=1
        cursor['last_record_id']=path.stem
        stage='F1_ARCHIVE'
        try:
            if path.stat().st_size>32*1024*1024 or (Path(registry_root)/(path.stem+'.artifacts.json')).stat().st_size>32*1024*1024:
                raise ValueError("F1 archive exceeds scan memory budget")
            record = registry.load(path.stem)
            registry.events(record.registration_id)
            stage='OBSERVATION_QUALIFICATION'
            assessment=observation_policy.assess(record,okx.utc_ms())
            qualification_event=eligibility_audit.record(assessment)
            assessments.append({"registration_id":record.registration_id,"event_id":qualification_event['event_id'],
                                "research_completed":assessment.research_completed,"plan_valid":assessment.plan_valid,
                                "observation_eligible":assessment.observation_eligible,"allow_new_risk":record.allow_new_risk,
                                "simulation_admitted":False,"execution_enabled":False})
            reasons = list(assessment.reasons)
            existing_expiry=(assessment.research_observation_qualified and assessment.window_status=='EXPIRED' and store.path(record.registration_id).exists())
            if not assessment.observation_eligible and not existing_expiry:
                excluded.append({"registration_id":record.registration_id,"category":sample_category(record),"reasons":reasons})
            elif registry.tracking_status(record.registration_id)!="QUEUED":
                excluded.append({"registration_id":record.registration_id,"category":sample_category(record),"reasons":["ADMINISTRATIVELY_NOT_QUEUED"]})
            else:
                candidate=candidate_from_record(record,assessment,existing_expiry=existing_expiry)
                stage='TRACKING_ARCHIVE'
                prior=store.load(candidate)
                if state_of(prior) in TERMINAL:
                    excluded.append({"registration_id":record.registration_id,"category":sample_category(record),"reasons":["F2_TERMINAL_"+state_of(prior)]})
                else:records.append(candidate)
        except LegacyTrackingChain as exc:
            errors.append({"registration_id":path.stem,"status":"LEGACY_TRACKING_CHAIN_PAUSED","error_type":type(exc).__name__})
        except (ValueError,KeyError,OSError) as exc:
            errors.append({"registration_id":path.stem,"status":stage+"_INVALID","error_type":type(exc).__name__})
    server_ms = int(client.get('/api/v5/public/time')[0]['ts'])
    if abs(server_ms-okx.utc_ms())>30_000: raise ValueError("Public server/local clock mismatch")
    results = []
    last_candidate=cursor.get('last_candidate_id','')
    records=sorted(records,key=lambda c:c.registration_id)
    ordered=[r for r in records if r.registration_id>last_candidate]+[r for r in records if r.registration_id<=last_candidate]
    attempted=0
    for candidate in ordered[:limits.max_candidates]:
        if time.monotonic() >= getattr(client,'deadline',float('inf')): break
        attempted+=1
        cursor['last_candidate_id']=candidate.registration_id
        try:
            results.append(process_candidate(candidate,store,collector,server_ms,limits))
        except (ValueError,KeyError,OSError) as exc:
            errors.append({"registration_id":candidate.registration_id,"status":"TRACKING_REJECTED","error_type":type(exc).__name__})
    probe = None
    if probe_market:
        end=server_ms//HOUR*HOUR
        data=collector.get(okx.validate_symbol(probe_market),end-2*HOUR,end)
        probe={"instrument":probe_market,"scope":"PUBLIC_CONNECTIVITY_ONLY_NOT_A_RESEARCH_SAMPLE",
               "fetched_at_ms":data['fetched_at_ms'],"rows":data['bars'],"fingerprint":data['fingerprint']}
    summary={"version":VERSION,"started_ms":started,"completed_ms":okx.utc_ms(),"market_cutoff_ms":server_ms,
             "excluded":excluded,"errors":errors,"eligible_candidates":sum(a["observation_eligible"] for a in assessments),"tracking_jobs":len(records),"results":results,
             "observation_assessments":assessments,"observation_eligible_count":sum(a['observation_eligible'] for a in assessments),
             "observation_policy_version":observation_policy.POLICY_VERSION,"observation_policy_hash":observation_policy.POLICY_HASH,
             "simulation_admitted":False,"simulated_trade_candidates":0,"allow_new_risk":False,
             "deferred_by_candidate_budget":max(0,len(records)-limits.max_candidates),
             "deferred_by_record_budget":max(0,len(record_paths)-checked_count),
             "deferred_candidates":max(0,len(records)-attempted),"records_checked":checked_count,"code_sha256":CODE_HASH,
             "logical_get_calls":getattr(client,'calls',None),"max_http_attempts_per_get":3,"limits":limits.model_dump(),
             "probe":probe,"execution_enabled":False,
             "risk_permission_scope":"Observation only; original risk fields unchanged; no simulation admission or execution authorization"}
    run_id=digest(summary)
    runs=Path(output)/'runs';runs.mkdir(parents=True,exist_ok=True)
    atomic(runs/(run_id+'.json'),summary)
    atomic(cursor_path,cursor)
    return summary


def scan(registry_root, output, limits=None, *, client=None, probe_market=None):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    # Avoid duplicate acquisition by independent scheduled scans on this host.
    with locked(output/'scan',wait=False) as acquired:
        if not acquired:
            return {'version':VERSION,'status':'SCAN_BUSY','execution_enabled':False,'errors':[]}
        return _scan(registry_root,output,limits,client=client,probe_market=probe_market)


def read_condition_events(registry_root,output,registration_id):
    """Observation evidence only; never a simulated-trade admission interface."""
    registry=Registry(registry_root)
    record=registry.load(registration_id)
    registry.events(registration_id)
    assessment=observation_policy.assess(record,okx.utc_ms())
    if not assessment.observation_eligible or registry.tracking_status(registration_id)!='QUEUED':return []
    observation_policy.EligibilityAudit(output).record(assessment)
    candidate=candidate_from_record(record,assessment)
    return EventStore(output).triggers(candidate)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['scan','_worker'])
    parser.add_argument('--registry',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--max-seconds',type=int,default=180)
    parser.add_argument('--max-requests',type=int,default=8)
    parser.add_argument('--max-candidates',type=int,default=20)
    parser.add_argument('--max-records',type=int,default=200)
    parser.add_argument('--max-pages',type=int,default=3)
    parser.add_argument('--probe-market')
    args=parser.parse_args()
    limits=ScanLimits(max_seconds=args.max_seconds,max_requests=args.max_requests,
                      max_candidates=args.max_candidates,max_records=args.max_records,max_pages=args.max_pages)
    if args.command=='scan':
        command=[sys.executable,'-m','tradingagents.forward_tracking','_worker','--registry',str(args.registry),
                 '--output',str(args.output),'--max-seconds',str(limits.max_seconds),
                 '--max-requests',str(limits.max_requests),'--max-candidates',str(limits.max_candidates),'--max-records',str(limits.max_records),
                 '--max-pages',str(limits.max_pages)]
        if args.probe_market:command.extend(['--probe-market',args.probe_market])
        try:
            completed=subprocess.run(command,timeout=limits.max_seconds,check=False)
            raise SystemExit(completed.returncode)
        except subprocess.TimeoutExpired:
            failure={'status':'TIME_BUDGET_EXHAUSTED','recorded_ms':okx.utc_ms(),'execution_enabled':False}
            runs=args.output/'runs';runs.mkdir(parents=True,exist_ok=True)
            atomic(runs/(digest(failure)+'.json'),failure)
            print(json.dumps(failure))
            raise SystemExit(4) from None
    try:
        result=scan(args.registry,args.output,limits,probe_market=args.probe_market)
        print(json.dumps(result,indent=2,allow_nan=False))
        if result['errors']:raise SystemExit(2)
    except (ValueError,KeyError,OSError,BudgetExhausted,okx.VendorUnavailableError) as exc:
        failure={'version':VERSION,'status':'SCAN_UNAVAILABLE','error_type':type(exc).__name__,'recorded_ms':okx.utc_ms(),'market_cutoff_ms':None,'execution_enabled':False}
        runs=args.output/'runs';runs.mkdir(parents=True,exist_ok=True)
        atomic(runs/(digest(failure)+'.json'),failure)
        print(json.dumps(failure))
        raise SystemExit(3) from None


if __name__=='__main__':main()
