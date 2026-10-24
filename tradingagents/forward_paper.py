"""F3 isolated paper evaluation. Public GET only; no account/order capability.

Historical D and sealed F1/F2 remain read-only. All production entry decisions
must have been durably scheduled BEFORE the prospective opening boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import subprocess
import sys
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents.dataflows import perpetual_plan as plans
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.plan_lifecycle import HOUR, evaluate_signal
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv
from tradingagents.forward_validation import Registry, atomic, digest, read
from tradingagents import forward_tracking as f2, observation_eligibility as observation
from tradingagents.okx_diagnostics import verify_funding_evidence

ROOT = Path(__file__).resolve().parents[1]
VERSION = "forward-paper-v1"
CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class PaperProtocol(ResearchModel):
    version: Literal["forward-paper-v1"] = VERSION
    initial_equity: Literal[10000] = 10000
    notional_usdt: Literal[1000] = 1000
    taker_fee_bps: Literal[5] = 5
    maker_fee_bps: None = None
    slippage_bps: Literal[2] = 2
    max_holding_bars: Literal[12] = 12
    opening_delivery_ms: Literal[15000] = 15000
    revalidation_ttl_ms: Literal[60000] = 60000
    fill_rule: Literal["PRECOMMITTED_FUTURE_HOUR_OPEN_OBSERVED_ON_TIME"] = "PRECOMMITTED_FUTURE_HOUR_OPEN_OBSERVED_ON_TIME"
    first_bar_target_rule: Literal["CLOSE_CONFIRMED_OR_UNRESOLVED"] = "CLOSE_CONFIRMED_OR_UNRESOLVED"
    same_bar_rule: Literal["STOP_FIRST"] = "STOP_FIRST"
    funding_rule: Literal["REALIZED_PAGINATED_BRACKETS_TRADE_OPEN_PROXY"] = "REALIZED_PAGINATED_BRACKETS_TRADE_OPEN_PROXY"
    simulation_admitted: Literal[False] = False
    allow_new_risk: Literal[False] = False
    execution_enabled: Literal[False] = False


PROTOCOL = PaperProtocol()
POLICY = PROTOCOL.model_dump()
POLICY_HASH = digest(POLICY)


class PaperEvent(ResearchModel):
    version: Literal["forward-paper-v1"] = VERSION
    event_id: str
    recorded_ms: int = Field(gt=0)
    registration_id: str
    kind: str
    policy_hash: str
    previous_hash: str | None
    payload: dict
    event_hash: str
    simulation_admitted: Literal[False] = False
    allow_new_risk: Literal[False] = False
    execution_enabled: Literal[False] = False


class PaperState(ResearchModel):
    paper_trade_id: str
    registration_id: str
    research_id: str
    namespace: Literal["PRODUCTION", "FIXTURE"]
    group: str
    pair_id: str | None
    identity: dict
    plan: dict
    info: dict
    trigger: dict
    paper_evaluation_eligible: bool
    status: Literal["SCHEDULED", "CANCELLED", "MISSED", "OPEN", "EXIT_PENDING", "SETTLED", "UNRESOLVED"]
    planned_entry_ms: int
    revalidation: dict
    cursor_ms: int | None = None
    position: dict = Field(default_factory=dict)
    exit: dict = Field(default_factory=dict)
    funding: dict = Field(default_factory=dict)
    backfill_used: bool = False
    reason: str = ""
    simulation_admitted: Literal[False] = False
    allow_new_risk: Literal[False] = False
    execution_enabled: Literal[False] = False

    @model_validator(mode='after')
    def lifecycle_consistency(self):
        if self.status in {'OPEN','EXIT_PENDING','SETTLED','UNRESOLVED'} and (not self.position or not self.paper_evaluation_eligible):
            raise ValueError('Position without paper qualification')
        if self.status in {'EXIT_PENDING','SETTLED'} and not self.exit:
            raise ValueError('Missing price-exit evidence')
        if self.status=='SETTLED' and ('net_pnl' not in self.exit or 'charges' not in self.funding):
            raise ValueError('Settlement without realized cost evidence')
        return self


class PaperJournal(ResearchModel):
    policy: PaperProtocol
    code_hash: str
    namespace: Literal['PRODUCTION','FIXTURE']
    events: list[PaperEvent] = Field(max_length=20000)


class PaperReport(ResearchModel):
    version: Literal['forward-paper-v1']
    namespace: Literal['PRODUCTION','FIXTURE']
    execution_enabled: Literal[False]
    simulation_admitted: Literal[False]
    allow_new_risk: Literal[False]
    research: dict
    paper: dict
    reliability: dict
    pairing: dict
    trades: list[PaperState]
    limitations: list[str]
    status: str | None = None
    recorded_ms: int | None = None
    records_limit_reached: bool | None = None
    candidates_limit_reached: bool | None = None


def next_open(completed_ms):
    """Strictly future, including when completion lands exactly on an hour."""
    return (completed_ms // HOUR + 1) * HOUR


class Ledger:
    """Single local atomic journal; global lock covers portfolio admission too."""
    def __init__(self, root, namespace="PRODUCTION"):
        if namespace not in {"PRODUCTION", "FIXTURE"}: raise ValueError("Invalid namespace")
        self.root = Path(root) / namespace.lower()
        self.root.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.path = self.root / "ledger.json"

    def load(self):
        if not self.path.exists(): return []
        if self.path.stat().st_size>32*1024*1024: raise ValueError('Paper journal resource cap exceeded; explicit archival required')
        value = read(self.path)
        PaperJournal.model_validate(value)
        if value["policy"] != POLICY or value["code_hash"] != CODE_HASH or value["namespace"] != self.namespace:
            raise ValueError("Paper code/protocol/namespace changed; explicit new ledger required")
        events = value["events"]
        seen = set()
        for i, raw in enumerate(events):
            event = PaperEvent.model_validate(raw)
            if event.event_id in seen or event.policy_hash != POLICY_HASH or event.previous_hash != (events[i-1]["event_hash"] if i else None):
                raise ValueError("Conflicting paper event chain")
            if event.event_hash != digest({k:v for k,v in raw.items() if k != "event_hash"}):
                raise ValueError("Paper event fingerprint mismatch")
            if i and event.recorded_ms < events[i-1]["recorded_ms"]: raise ValueError("Paper clock rollback")
            if isinstance(event.payload.get("state"),dict):
                state = PaperState.model_validate(event.payload["state"])
                if state.namespace != self.namespace or state.registration_id != event.registration_id:
                    raise ValueError("Cross-sample paper state")
            seen.add(event.event_id)
        return events

    def append(self, events, rid, kind, payload, now):
        identity = digest({"rid":rid,"kind":kind,"payload":payload})
        if any(e["event_id"] == identity for e in events): return
        if events and now < events[-1]["recorded_ms"]: raise ValueError("Paper clock rollback")
        raw = dict(version=VERSION,event_id=identity,registration_id=rid,kind=kind,
                   recorded_ms=now,policy_hash=POLICY_HASH,previous_hash=events[-1]["event_hash"] if events else None,
                   payload=payload,simulation_admitted=False,allow_new_risk=False,execution_enabled=False)
        raw["event_hash"] = digest(raw)
        PaperEvent.model_validate(raw)
        journal={"policy":POLICY,"code_hash":CODE_HASH,"namespace":self.namespace,"events":[*events,raw]}
        if len(journal['events'])>20000 or len(json.dumps(journal,allow_nan=False).encode())>32*1024*1024:
            raise ValueError('Paper journal resource cap exceeded; explicit archival required')
        atomic(self.path,journal)
        events.append(raw)

    @staticmethod
    def states(events):
        result = {}
        for e in events:
            if isinstance(e["payload"].get("state"),dict): result[e["registration_id"]] = PaperState.model_validate(e["payload"]["state"])
        return result


def admission(record, trigger, now):
    """Registry/EventStore verification MUST precede this pure assessment."""
    reasons = list(observation.assess(record, now).reasons)
    if record.instrument!='BTC-USDT-SWAP': reasons.append('PAPER_FUNDING_AUDIT_BTC_ONLY')
    if record.allow_new_risk is not False or record.original_result.get("allow_new_risk") is not False:
        reasons.append("ORIGINAL_RISK_NOT_FALSE")
    if trigger is None: reasons.append("NO_VERIFIED_F2_CONDITION")
    else:
        t = f2.ConditionTrigger.model_validate(trigger)
        if (t.registration_id != record.registration_id or t.research_id != record.research_id
            or t.snapshot_id != record.snapshot_id or t.result_hash != record.result_hash or t.protocol_hash != record.protocol_hash):
            reasons.append("CONDITION_OWNERSHIP_MISMATCH")
        if t.collection_mode != "CONTEMPORANEOUS": reasons.append("BACKFILLED_CONDITION")
        if not record.registered_ms <= t.signal_confirmed_ms <= t.observed_available_ms <= t.decision_available_ms <= now:
            reasons.append("INVALID_CONDITION_TIME")
        if now >= t.hypothesis_expires_ms: reasons.append("CONDITION_EXPIRED")
        raw=record.original_result
        expected={'direction':'LONG' if record.action=='OPEN_LONG_CANDIDATE' else 'SHORT',
                  'entry':raw.get('entry_reference'),'stop':raw.get('stop_loss'),
                  'target':(raw.get('take_profit') or [None])[0]}
        if t.original_conditions.get('plan')!=expected or t.original_conditions.get('signal_timing')!=raw.get('signal_timing'):
            reasons.append('FROZEN_CONDITIONS_MISMATCH')
    return sorted(set(reasons))


def config_for(record):
    # Preserve original research parameters; explicit defaults are existing defaults.
    return {**record.config, "okx_derivatives_ttl_seconds":record.original_result["signal_timing"]["market_data_ttl_seconds"]}


class Market:
    def __init__(self, client, limits):
        self.client, self.limits = client, limits
        self.collector = f2.Collector(client, limits)

    def now(self):
        now = int(self.client.get("/api/v5/public/time")[0]["ts"])
        if abs(now-okx.utc_ms()) > 30000: raise ValueError("Server/local clock mismatch")
        return now

    def bars(self, symbol, start, end):
        return self.collector.get(symbol,start,end)

    def refresh(self, record, trigger, now):
        core = record.quality_evidence["market_snapshot"]
        validation = plans.revalidate_live(core,config_for(record),self.client)
        checked = validation.get("checked_at_ms",0)
        if checked < now or checked > okx.utc_ms()+30000: raise ValueError("Invalid revalidation clock")
        if plans.compare_revalidation(core,validation,config_for(record)):
            raise ValueError("Revalidation drift or unavailable original evidence")
        return {"validation":validation,"completed_ms":okx.utc_ms(),"checked_ms":checked,
                "expires_ms":min(validation["expires_at_ms"],checked+PROTOCOL.revalidation_ttl_ms),
                "source":"Existing revalidate_live + compare_revalidation"}

    def opening(self, symbol, target):
        started = okx.utc_ms()
        rows = self.client.get("/api/v5/market/candles",{"instId":symbol,"bar":"1H","limit":"2"})
        acquired = okx.utc_ms()
        matches = [r for r in rows if len(r)==9 and int(r[0])==target]
        if len(matches)!=1 or started < target or acquired > target+PROTOCOL.opening_delivery_ms:
            raise ValueError("Opening not observed on time")
        r = matches[0]
        if str(r[8])!="0" or deriv.number(r[5],nonnegative=True)<=0: raise ValueError("Opening not verifiable")
        # Only open, ts, traded-volume existence are consumed. Never intrabar future HLC.
        return {"reference":deriv.number(r[1],positive=True),"boundary_ms":target,
                "request_started_ms":started,"observed_ms":acquired,"source":"/api/v5/market/candles",
                "fingerprint":digest(r),"collection_mode":"CONTEMPORANEOUS","unclosed_bar_used_only_for_open":True}

    def funding(self, symbol, start, end):
        # Existing verified pagination, no fabricated fixed 8-hour schedule.
        if symbol != "BTC-USDT-SWAP": raise ValueError("Funding audit currently BTC-only")
        server = self.now(); cursor = server+1; pages=[]
        initial = cursor
        for _ in range(self.limits.max_pages):
            rows = self.client.get(deriv.FUNDING_HISTORY,{"instId":symbol,"after":str(cursor),"limit":"100"})
            if not rows: break
            pages.append({"after":cursor,"rows":rows,"hash":digest(rows)})
            cursor = min(int(r["fundingTime"]) for r in rows)
            if cursor <= start: break
        proof = {"path":deriv.FUNDING_HISTORY,"initial_cursor":initial,"pages":pages,"server_ms":server}
        rates = deriv.parse_funding_history([r for p in pages for r in p["rows"]],symbol,server)["records"]
        verify_funding_evidence(rates,proof,start,end)
        return {"records":rates,"evidence":proof,"available_ms":okx.utc_ms(),"source":deriv.FUNDING_HISTORY}


def revalidation_ready(proof, trigger, now, *, before=None):
    if not proof or proof.get("completed_ms",now+1)>now or proof.get("checked_ms",0)<trigger["signal_confirmed_ms"]:
        return False
    if before is not None and proof["completed_ms"]>=before: return False
    validation = proof.get("validation",{})
    content={k:v for k,v in validation.items() if k!="validation_id"}
    return (validation.get("status")=="VALID" and validation.get("validation_id")==digest(content)
            and now<proof.get("expires_ms",0)<=min(validation.get("expires_at_ms",0),proof["checked_ms"]+PROTOCOL.revalidation_ttl_ms)
            and validation.get("checked_at_ms")==proof["checked_ms"]
            and proof["completed_ms"]>=proof["checked_ms"])


def create_state(record, trigger, proof, now, namespace="PRODUCTION"):
    if admission(record,trigger,now): raise ValueError("Paper admission rejected")
    if not revalidation_ready(proof,trigger,now): raise ValueError("Revalidation incomplete")
    candidate = f2.candidate_from_record(record,observation.assess(record,now))
    target = next_open(max(now,proof["completed_ms"],trigger["decision_available_ms"]))
    if target >= record.expires_ms: raise ValueError("No future opening before expiry")
    group = digest({"config":record.config,"protocol":POLICY_HASH})
    return PaperState(paper_trade_id=digest({"registration":record.registration_id,"paper_policy":POLICY_HASH}),
        registration_id=record.registration_id,research_id=record.research_id,namespace=namespace,group=group,pair_id=record.pair_id,
        identity={"registration_hash":record.content_hash,"result_hash":record.result_hash,"snapshot_id":record.snapshot_id,
                  "f1_protocol_hash":record.protocol_hash,"f2_code_hash":f2.CODE_HASH,"observation_policy_hash":observation.POLICY_HASH,
                  "paper_protocol_hash":POLICY_HASH,"research_config":record.config,"research_git_commit":record.git_commit,
                  "paper_git_commit":subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                  "paper_code_hash":CODE_HASH,"research_cutoff_ms":record.cutoff_ms,"registered_ms":record.registered_ms,
                  "origin":record.origin,"research_completed_ms":record.completed_ms,"instrument":record.instrument,
                  "strategy_source_hashes":record.source_hashes},
        plan=candidate.plan,info=record.quality_evidence["market_snapshot"]["instrument"],trigger=trigger,
        paper_evaluation_eligible=True,status="SCHEDULED",planned_entry_ms=target,revalidation=proof)


def check_structure(state, bars, now):
    clock = state.trigger["original_conditions"]["signal_timing"]
    # Unchanged C.1 replay of the complete original confirmation prefix.
    result = evaluate_signal(state.plan,clock,bars,now)
    return result.state == "ENTRY_ELIGIBLE"


def fill(state, opening, now, equity=10000):
    """Pure, precommitted, bounded on-time observation; never backfill."""
    s = state.model_copy(deep=True); target = s.planned_entry_ms
    if s.status != "SCHEDULED": raise ValueError("Not scheduled")
    if not s.paper_evaluation_eligible: raise ValueError('No independent paper qualification')
    if not 0<s.identity.get("schedule_persisted_ms",0)<target:
        raise ValueError("Schedule was not durably armed before opening")
    if not target <= opening["request_started_ms"] <= opening["observed_ms"] <= now <= target+PROTOCOL.opening_delivery_ms:
        raise ValueError("Missed planned opening")
    if opening.get("collection_mode")!="CONTEMPORANEOUS" or opening.get("boundary_ms")!=target:
        raise ValueError("Backfilled or wrong opening")
    if not revalidation_ready(s.revalidation,s.trigger,now,before=target): raise ValueError("Stale or late pre-entry validation")
    if now>=s.trigger["hypothesis_expires_ms"]: raise ValueError("Plan expired")
    okx.validate_instrument(s.info,"BTC-USDT-SWAP")
    plans.validate_prices(s.plan,s.info)
    sign = 1 if s.plan["direction"]=="LONG" else -1
    reference = deriv.number(opening["reference"],positive=True)
    price = plans.round_tick(reference*(1+sign*PROTOCOL.slippage_bps/10000),s.info["tickSz"],sign>0)
    stop,target_price = s.plan["stop"]["price"],s.plan["target"]["price"]
    if not (stop<price<target_price if sign==1 else target_price<price<stop): raise ValueError("Opening gap outside frozen geometry")
    lot = Decimal(s.info["lotSz"]); mult = deriv.multiplier(s.info)
    size = (Decimal(str(PROTOCOL.notional_usdt/price/mult))/lot).to_integral_value(rounding=ROUND_FLOOR)*lot
    base = float(size)*mult; fee = base*price*PROTOCOL.taker_fee_bps/10000
    if size<Decimal(s.info["minSz"]) or base*price+fee>equity: raise ValueError("Minimum quantity or unlevered budget")
    s.status="OPEN";s.cursor_ms=s.planned_entry_ms
    s.position={"direction":s.plan["direction"],"sign":sign,"contracts":float(size),"base_quantity":base,
                "entry_ms":opening["observed_ms"],"reference_boundary_ms":s.planned_entry_ms,
                "entry_reference":reference,"entry_price":price,"entry_fee":fee,"entry_slippage":base*abs(price-reference),
                "opening_observation":opening,"mae_upper_bound_usdt":0.,"mfe_upper_bound_usdt":0.}
    return s


def advance_bar(state, bar, available_ms):
    """D stop-first / adverse tick/slippage primitives; entry-bar timing explicit."""
    s=state.model_copy(deep=True);p=s.position;ts=bar["ts"]
    if s.status!="OPEN" or ts!=s.cursor_ms or ts%HOUR or bar["confirm"]!="1" or ts+HOUR>available_ms:
        raise ValueError("Missing, duplicate, future or unconfirmed execution bar")
    o,h,l,c=[deriv.number(bar[k],positive=True) for k in ("open","high","low","close")]
    if not l<=min(o,c)<=max(o,c)<=h: raise ValueError("Invalid OHLC")
    s.backfill_used |= available_ms-(ts+HOUR)>90000
    p.setdefault('bar_evidence',[]).append({'bar':dict(bar),'available_ms':available_ms,
        'fingerprint':digest(bar),'collection_mode':'BACKFILL' if available_ms-(ts+HOUR)>90000 else 'CONTEMPORANEOUS',
        'source':'/api/v5/market/history-candles'})
    long=p["sign"]==1;stop=s.plan["stop"]["price"];target=s.plan["target"]["price"]
    p["mae_upper_bound_usdt"]=max(p["mae_upper_bound_usdt"],max(0,(p["entry_price"]-l if long else h-p["entry_price"]))*p["base_quantity"])
    p["mfe_upper_bound_usdt"]=max(p["mfe_upper_bound_usdt"],max(0,(h-p["entry_price"] if long else p["entry_price"]-l))*p["base_quantity"])
    p.setdefault("closed_marks",[]).append({"ts":ts+HOUR,"available_ms":available_ms,
        "gross_unrealized_pnl":p['sign']*p['base_quantity']*(c-p['entry_price'])})
    hit_stop=l<=stop if long else h>=stop
    hit_target=h>=target if long else l<=target
    ref=reason=None
    if hit_stop: ref=min(o,stop) if long else max(o,stop);reason="STOP_FIRST_AMBIGUOUS" if hit_target else "STOP"
    elif hit_target and ts<s.planned_entry_ms+PROTOCOL.max_holding_bars*HOUR:
        # Actual observation happens seconds after boundary. First-bar high may
        # predate entry: do not claim a favorable target without closing evidence.
        if ts==s.planned_entry_ms and not (c>=target if long else c<=target):
            s.status="UNRESOLVED";s.reason="ENTRY_BAR_TARGET_ORDER_UNKNOWN";return s
        ref=target;reason="TARGET"
    if ts>=s.planned_entry_ms+PROTOCOL.max_holding_bars*HOUR:
        ref=o;reason="MAX_HOLDING"
    if ref is not None:
        exit_price=plans.round_tick(ref*(1-p["sign"]*PROTOCOL.slippage_bps/10000),s.info["tickSz"],p["sign"]<0)
        s.exit={"exit_reference":ref,"exit_price":exit_price,"reason":reason,
                "economic_boundary_ms":ts if reason=="MAX_HOLDING" else ts+HOUR,
                "known_at_ms":available_ms,"gross_pnl_after_slippage":p["sign"]*p["base_quantity"]*(exit_price-p["entry_price"]),
                "exit_fee":p["base_quantity"]*exit_price*PROTOCOL.taker_fee_bps/10000,
                "exit_slippage":p["base_quantity"]*abs(exit_price-ref)}
        s.status="EXIT_PENDING"
    s.cursor_ms=ts+HOUR
    return s


def settle(state, funding, bars, *, as_of_ms):
    s=state.model_copy(deep=True)
    if s.status!="EXIT_PENDING": raise ValueError("No closed trade to settle")
    p=s.position;exit_ms=s.exit["economic_boundary_ms"]
    start=s.planned_entry_ms
    if not s.exit["known_at_ms"]<=funding["available_ms"]<=as_of_ms or funding["evidence"]["server_ms"]>funding["available_ms"]:
        raise ValueError("Future or pre-outcome funding evidence")
    verify_funding_evidence(funding["records"],funding["evidence"],start,exit_ms)
    by_ts={b["ts"]:b for b in bars}
    if len(by_ts)!=len(bars) or any(b['confirm']!='1' or b['ts']+HOUR>as_of_ms for b in bars):
        raise ValueError("Duplicate, future or unconfirmed funding proxy bars")
    cashflow=0.;charges=[]
    for r in funding["records"]:
        ts=r["ts"]
        # D: carried into boundary before scheduled open exit; same-boundary
        # intrabar exits are known at close but not carried into next settlement.
        eligible=p["entry_ms"]<ts and (ts<=exit_ms if s.exit["reason"]=="MAX_HOLDING" else ts<exit_ms)
        if not eligible: continue
        if ts not in by_ts: raise ValueError("Funding trade-open proxy missing")
        price=deriv.number(by_ts[ts]["open"],positive=True)
        amount=deriv.funding_cashflow(p["base_quantity"],price,r["realized_rate"],p["direction"])
        cashflow+=amount;charges.append({"ts":ts,"realized_rate":r["realized_rate"],"trade_open_proxy":price,"cashflow_usdt":amount})
    s.funding={**funding,"charges":charges,"cashflow_usdt":cashflow,"price_scope":"TRADE_OPEN_PROXY_NOT_EXACT_SETTLEMENT_MARK"}
    s.exit.update(net_pnl=s.exit["gross_pnl_after_slippage"]-p["entry_fee"]-s.exit["exit_fee"]+cashflow,
                  fees_usdt=p["entry_fee"]+s.exit["exit_fee"],slippage_usdt=p["entry_slippage"]+s.exit["exit_slippage"],
                  result_known_at_ms=max(funding["available_ms"],s.exit["known_at_ms"]))
    s.status="SETTLED";return s


def feedback(states, as_of_ms, namespace="PRODUCTION"):
    """Read-only future Memory/Reflection interface; no automatic learning."""
    return [{"kind":"FORWARD_PAPER_SETTLED_FEEDBACK","paper_trade_id":s.paper_trade_id,
             "research_id":s.research_id,"registration_id":s.registration_id,"identity":s.identity,
             "result":s.exit,"outcome_known_at_ms":s.exit["result_known_at_ms"],"namespace":namespace,
             "strategy_validated":False,"simulation_admitted":False,"execution_enabled":False}
            for s in states if s.namespace==namespace and s.status=="SETTLED" and s.exit["result_known_at_ms"]<=as_of_ms]


def compare_pair(left,right):
    """Only a prospective design match; never a historical replay upgrade."""
    checks=[left.namespace==right.namespace=='PRODUCTION',bool(left.pair_id) and left.pair_id==right.pair_id,
            left.registration_id!=right.registration_id,left.group!=right.group,
            left.identity.get('origin')==right.identity.get('origin')=='PROSPECTIVE',
            left.identity.get('paper_protocol_hash')==right.identity.get('paper_protocol_hash')==POLICY_HASH,
            left.identity.get('instrument')==right.identity.get('instrument'),
            left.identity.get('research_cutoff_ms')==right.identity.get('research_cutoff_ms'),
            left.planned_entry_ms==right.planned_entry_ms]
    for name in ('llm_provider','quick_think_llm','deep_think_llm'):
        checks.append(left.identity['research_config'].get(name)==right.identity['research_config'].get(name))
    return {'pair_id':left.pair_id,'registrations':[left.registration_id,right.registration_id],
            'status':'PROSPECTIVE_DESIGN_MATCH' if all(checks) else 'NOT_COMPARABLE',
            'evaluation_window':[left.planned_entry_ms,left.planned_entry_ms+PROTOCOL.max_holding_bars*HOUR],
            'configurations':[left.identity.get('research_config'),right.identity.get('research_config')],
            'net_pnl_comparison':None,'independent_market_samples':1,
            'limitation':'Design match is not evidence of version superiority or a continuous portfolio counterfactual'}


def report(records, events, namespace="PRODUCTION"):
    states=[s for s in Ledger.states(events).values() if s.namespace==namespace]
    settled=[s for s in states if s.status=="SETTLED"]
    pnls=[s.exit["net_pnl"] for s in settled];wins=[v for v in pnls if v>0];losses=[v for v in pnls if v<0]
    groups={}
    for s in states: groups.setdefault(s.group,[]).append(s)
    curves={};max_dd={};closed_curves={};closed_dd={}
    for group,items in groups.items():
        # Realized-only equity; do not mislabel as full mark-to-market drawdown.
        equity=float(PROTOCOL.initial_equity);peak=equity;dd=0.;curve=[]
        for s in sorted([s for s in items if s.status=="SETTLED"],key=lambda s:(s.exit["result_known_at_ms"],s.paper_trade_id)):
            equity+=s.exit["net_pnl"];peak=max(peak,equity);dd=max(dd,(peak-equity)/peak)
            curve.append({"known_at_ms":s.exit["result_known_at_ms"],"equity":equity})
        curves[group]=curve;max_dd[group]=dd if curve else None
        # Reconstruct closed-candle net MTM ONLY after actual funding becomes
        # known. It is an ex-post evaluation, never input to an earlier decision.
        equity=float(PROTOCOL.initial_equity);peak=equity;dd=0.;netcurve=[]
        for s in sorted([s for s in items if s.status=='SETTLED'],key=lambda s:s.position['entry_ms']):
            for m in s.position.get('closed_marks',[]):
                if m['ts']>=s.exit['economic_boundary_ms']:continue
                funding=sum(c['cashflow_usdt'] for c in s.funding['charges'] if c['ts']<=m['ts'])
                marked=equity+m['gross_unrealized_pnl']-s.position['entry_fee']+funding
                peak=max(peak,marked);dd=max(dd,(peak-marked)/peak)
                netcurve.append({'market_ms':m['ts'],'equity':marked,'reconstruction_known_at_ms':s.exit['result_known_at_ms']})
            equity+=s.exit['net_pnl'];peak=max(peak,equity);dd=max(dd,(peak-equity)/peak)
            netcurve.append({'market_ms':s.exit['economic_boundary_ms'],'equity':equity,'reconstruction_known_at_ms':s.exit['result_known_at_ms']})
        closed_curves[group]=netcurve;closed_dd[group]=dd if netcurve else None
    prospective=[r for r in records if r.origin=="PROSPECTIVE"]
    completed_wait=sum(r.workflow_status=="COMPLETED" and r.action=="WAIT" for r in prospective)
    failed=sum(r.workflow_status!="COMPLETED" for r in prospective)
    valid=sum(r.plan_valid and r.workflow_status=="COMPLETED" for r in prospective)
    observed=sum(observation.assess(r,r.registered_ms).research_observation_qualified for r in prospective)
    reviews={}
    for e in events:
        if e['kind']=='OBSERVATION_REVIEW':reviews[e['registration_id']]=e['payload']
    triggered=sum(v.get('conditions',0)>0 for v in reviews.values())
    pairs={}
    for s in states:
        if s.pair_id: pairs.setdefault(s.pair_id,[]).append(s)
    paired=[compare_pair(v[i],v[j]) for v in pairs.values() for i in range(len(v)) for j in range(i+1,len(v)) if v[i].group!=v[j].group]
    return {"version":VERSION,"namespace":namespace,"execution_enabled":False,"simulation_admitted":False,"allow_new_risk":False,
        "research":{"total":len(records),"prospective":len(prospective),"completed_wait":completed_wait,"failed":failed,
                    "valid_candidates":valid,"observation_qualified":observed,"observation_rate":observed/valid if valid else None,
                    "paper_scheduled":len(states),"condition_triggered":triggered,"condition_trigger_denominator":observed,
                    "condition_trigger_rate":triggered/observed if observed else None,
                    "observed_expired":sum(v.get('state')=='EXPIRED' for v in reviews.values()),
                    "observed_invalidated":sum(v.get('state')=='INVALIDATED' for v in reviews.values()),
                    "f2_reviewed":len(reviews),"f2_paused":sum(v.get('paused',False) for v in reviews.values())},
        "paper":{"trades":sum(bool(s.position) for s in states),"settled":len(settled),"unsettled":sum(bool(s.position) and s.status!="SETTLED" for s in states),
                 "wins":len(wins),"losses":len(losses),"win_rate":len(wins)/len(pnls) if pnls else None,
                 "average_win":sum(wins)/len(wins) if wins else None,"average_loss":sum(losses)/len(losses) if losses else None,
                 "net_pnl":sum(pnls) if pnls else None,"gross_pnl_after_slippage":sum(s.exit["gross_pnl_after_slippage"] for s in settled) if settled else None,
                 "gross_pnl_before_costs":sum(s.exit["gross_pnl_after_slippage"]+s.exit['slippage_usdt'] for s in settled) if settled else None,
                 "profit_factor":sum(wins)/-sum(losses) if losses else None,
                 "payoff_ratio":(sum(wins)/len(wins))/(-sum(losses)/len(losses)) if wins and losses else None,
                 "fees_usdt":sum(s.exit["fees_usdt"] for s in settled),"slippage_usdt":sum(s.exit["slippage_usdt"] for s in settled),
                 "funding_cashflow_usdt":sum(s.funding["cashflow_usdt"] for s in settled),
                 "realized_only_drawdown_by_group":max_dd,"equity_curves":curves,
                 "settled_history_closed_candle_drawdown_by_group":closed_dd,
                 "settled_history_closed_candle_equity":closed_curves,
                 "complete_net_equity_available":not any(s.status in {'OPEN','EXIT_PENDING','UNRESOLVED'} for s in states)},
        "reliability":{"errors":sum(e["kind"]=="PAUSED" for e in events),"admission_rejections":sum(e["kind"]=="ADMISSION_REJECTED" for e in events),
                       "missed":sum(s.status=="MISSED" for s in states),"cancelled":sum(s.status=="CANCELLED" for s in states),
                       "unresolved":sum(s.status=="UNRESOLVED" for s in states),"backfilled_position_histories":sum(s.backfill_used for s in states)},
        "pairing":{"independent_market_samples":len({r.pair_id for r in prospective if r.pair_id}),"pairs":paired,
                   "performance_comparison":None,"scope":"Same F1 pair_id is necessary, not sufficient; no claimed version superiority"},
        "trades":[s.model_dump() for s in states],
        "limitations":["No execution, leverage, margin or liquidation proof","Opening reference observed within 15s after precommitted boundary; fill time is actual receipt, NOT backdated",
                        "First-entry-bar target without close confirmation is unresolved; conservative first-bar stop may include pre-entry extremes",
                        "MAE/MFE are candle upper bounds, not tick path","Realized-only drawdown; unresolved/open exposure omitted from drawdown, not comprehensive portfolio risk",
                        "Funding uses disclosed trade-open proxy; settlement waits for actual event brackets","Small samples provide no predictive win probability",
                        "Historical D timing unchanged; F3 future-hour timing differs","No automatic Memory/Reflection writes"]}


def _verified_inputs(registry, f2root, record, now, *, existing=False):
    registry.events(record.registration_id)
    if registry.tracking_status(record.registration_id)!="QUEUED": raise ValueError("F1 administrative exclusion")
    # Existing settled/open positions must survive expiry; assessment at original
    # registration verifies qualification without pretending it is still fresh.
    assessment=observation.assess(record,record.registered_ms if existing else now)
    if not assessment.observation_eligible: return [],assessment.reasons,{}
    candidate=f2.candidate_from_record(record,existing_expiry=existing)
    path=Path(f2root)/"production"/(record.registration_id+".events.json")
    if not path.exists(): return [],["NO_F2_CHAIN"],{}
    saved=read(path)
    grace=saved["policy"]["delivery_grace_seconds"]
    # Read-only: EventStore constructor would create directories; this existing
    # production directory is already present and never appended by F3.
    store=f2.EventStore(f2root,delivery_grace_seconds=grace)
    events=store.load(candidate)
    triggers=[t.model_dump() for t in store.triggers(candidate)]
    return triggers,[],{'state':f2.state_of(events),'conditions':len(triggers),
                       'paused':bool(events and events[-1]['kind']=='PAUSED'),
                       'chain_hash':events[-1]['event_hash'] if events else None}


def scan(registry_root, f2root, output, limits=None, *, market=None):
    limits=limits or f2.ScanLimits(max_requests=40,max_seconds=180)
    for source in (registry_root,f2root):
        if not Path(source).is_dir(): raise ValueError("Existing F1/F2 directories required")
        a,b=Path(source).resolve(),Path(output).resolve()
        if a==b or a in b.parents or b in a.parents: raise ValueError("F3 output must be isolated from F1/F2")
    ledger=Ledger(output)
    with locked(ledger.path,wait=False) as held:
        if not held: return {"status":"BUSY","execution_enabled":False}
        events=ledger.load();states=ledger.states(events)
        registry=Registry(registry_root)
        records=[];paths=sorted(Path(registry_root).glob("*.json"))
        active_ids={rid for rid,s in states.items() if s.status in {'SCHEDULED','OPEN','EXIT_PENDING'}}
        paths.sort(key=lambda path:(path.stem not in active_ids,path.name))
        for p in paths:
            if len(p.stem)==64:
                if len(records)>=limits.max_records: break
                records.append(registry.load(p.stem))
        market=market or Market(f2.PublicBudgetClient(limits),limits)
        now=market.now()
        processed=0
        cursor_path=ledger.root/'scan_cursor.json'
        cursor=read(cursor_path) if cursor_path.exists() else {'active':0,'other':0}
        active=sorted([r for r in records if r.registration_id in active_ids],key=lambda r:r.registration_id)
        other=sorted([r for r in records if r.registration_id not in active_ids],key=lambda r:(r.registered_ms,r.registration_id))
        def rotate(items,offset):
            if not items:return []
            index=offset%len(items);return items[index:]+items[:index]
        queue=rotate(active,cursor['active'])+rotate(other,cursor['other'])
        for r in queue:
            if processed>=limits.max_candidates: break
            processed+=1;rid=r.registration_id;s=states.get(rid)
            try:
                triggers,reasons,review=_verified_inputs(registry,f2root,r,now,existing=s is not None)
                if review: ledger.append(events,rid,'OBSERVATION_REVIEW',review,now)
                if s and (s.identity["registration_hash"]!=r.content_hash or not any(t["event_id"]==s.trigger["event_id"] for t in triggers)):
                    raise ValueError("Sealed research/condition identity changed")
                if not s:
                    if reasons or not triggers:
                        ledger.append(events,rid,"ADMISSION_REJECTED",{"reasons":reasons or ["NO_ENTRY_CONDITION"]},now);continue
                    trigger=triggers[0];reasons=admission(r,trigger,now)
                    if review.get('state') in {'EXPIRED','INVALIDATED'}: reasons.append('F2_TERMINAL_HYPOTHESIS')
                    if reasons:
                        ledger.append(events,rid,"ADMISSION_REJECTED",{"reasons":reasons},now);continue
                    group=digest({"config":r.config,"protocol":POLICY_HASH})
                    if any(t.group==group and t.status in {"SCHEDULED","OPEN","EXIT_PENDING","UNRESOLVED"} for t in states.values()):
                        ledger.append(events,rid,"ADMISSION_REJECTED",{"reasons":["SINGLE_ACTIVE_EXPOSURE_GROUP"]},now);continue
                    proof=market.refresh(r,trigger,now);now=okx.utc_ms()
                    if now//HOUR*HOUR>trigger['signal_confirmed_ms']:
                        batch=market.bars(r.instrument,r.first_bar_open_ms,now//HOUR*HOUR)
                        if evaluate_signal(trigger['original_conditions']['plan'],trigger['original_conditions']['signal_timing'],batch['bars'],now).state!='ENTRY_ELIGIBLE':
                            raise ValueError('Structure no longer eligible at paper admission')
                    s=create_state(r,trigger,proof,now)
                    ledger.append(events,rid,"SCHEDULED",{"state":s.model_dump()},now);states[rid]=s
                    # The first atomic commit MUST be known durable before the
                    # boundary. Crash before ARM is fail-closed, never inferred.
                    persisted=okx.utc_ms()
                    s.identity["schedule_persisted_ms"]=persisted
                    if persisted>=s.planned_entry_ms:
                        s.status="MISSED";s.reason="SCHEDULE_COMMIT_TOO_LATE"
                    ledger.append(events,rid,"ARMED",{"state":s.model_dump()},persisted);states[rid]=s
                    continue
                if s.status in {"SETTLED","MISSED","CANCELLED","UNRESOLVED"}: continue
                target=s.planned_entry_ms
                if s.status=="SCHEDULED":
                    if now>=s.trigger["hypothesis_expires_ms"]:
                        s=s.model_copy(deep=True);s.status="CANCELLED";s.reason="PLAN_EXPIRED"
                    elif now>target+PROTOCOL.opening_delivery_ms:
                        s=s.model_copy(deep=True);s.status="MISSED";s.reason="MISSED_PLANNED_OPEN_NO_BACKFILL"
                    else:
                        first=r.first_bar_open_ms;end=now//HOUR*HOUR
                        batch=market.bars(r.instrument,first,end)
                        if not check_structure(s,batch["bars"],now):
                            s=s.model_copy(deep=True);s.status="CANCELLED";s.reason="STRUCTURE_INVALIDATED"
                        elif now<target:
                            if target-now<=PROTOCOL.revalidation_ttl_ms:
                                proof=market.refresh(r,s.trigger,now);now=okx.utc_ms()
                                if proof["completed_ms"]>=target: raise ValueError("Revalidation missed planned opening")
                                s=s.model_copy(deep=True);s.revalidation=proof
                            else: continue
                        else:
                            opening=market.opening(r.instrument,target);now=okx.utc_ms()
                            equity=PROTOCOL.initial_equity+sum(t.exit.get("net_pnl",0) for t in states.values() if t.group==s.group and t.status=="SETTLED")
                            s=fill(s,opening,now,equity)
                elif s.status=="OPEN":
                    end=now//HOUR*HOUR
                    if end<=s.cursor_ms: continue
                    batch=market.bars(r.instrument,s.cursor_ms,end)
                    for b in batch["bars"]:
                        s=advance_bar(s,b,batch["fetched_at_ms"])
                        if s.status!="OPEN": break
                if s.status=="EXIT_PENDING":
                    try:
                        funding=market.funding(r.instrument,s.planned_entry_ms,s.exit["economic_boundary_ms"])
                        # Include settlement-hour opens (also at scheduled exits).
                        end=max(s.cursor_ms,s.exit["economic_boundary_ms"]+HOUR)
                        if end>now//HOUR*HOUR: raise ValueError("Funding price proxy bar not yet closed")
                        batch=market.bars(r.instrument,s.planned_entry_ms,end)
                        s=settle(s,funding,batch["bars"],as_of_ms=okx.utc_ms())
                    except (ValueError,KeyError,okx.VendorUnavailableError,f2.BudgetExhausted):
                        # Persist price closure even when funding is not available.
                        s.reason="WAITING_FOR_VERIFIED_REALIZED_FUNDING"
                now=max(now,okx.utc_ms())
                ledger.append(events,rid,s.status,{"state":s.model_dump()},now);states[rid]=s
            except (ValueError,KeyError,TypeError,OSError,okx.VendorUnavailableError,f2.BudgetExhausted) as exc:
                now=max(now,okx.utc_ms())
                message=str(exc).lower()
                code=('BUDGET_EXHAUSTED' if isinstance(exc,f2.BudgetExhausted) else
                      'INTEGRITY_FAILURE' if any(w in message for w in ('fingerprint','identity','hash','tamper')) else
                      'DATA_GAP_OR_UNAVAILABLE' if any(w in message for w in ('missing','coverage','gap','unconfirmed')) else
                      'REVALIDATION_UNAVAILABLE' if 'revalid' in message else 'DATA_OR_PROTOCOL_UNAVAILABLE')
                ledger.append(events,rid,"PAUSED",{"error_type":type(exc).__name__,"reason":code},now)
        result=report(records,events)
        used_active=min(processed,len(active));used_other=max(0,processed-used_active)
        atomic(cursor_path,{'active':cursor['active']+used_active,'other':cursor['other']+used_other})
        result.update(status="COMPLETED",recorded_ms=okx.utc_ms(),records_limit_reached=len(records)>=limits.max_records,
                      candidates_limit_reached=processed<len(records))
        runs=Path(output)/"runs";runs.mkdir(parents=True,exist_ok=True)
        save_report(runs,result)
        return result


def save_report(directory,result):
    PaperReport.model_validate(result)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    stem=digest(result)
    atomic(directory/(stem+'.json'),result)
    body=('# F3 forward paper evaluation\n\n'
          f"Namespace: {result['namespace']}; execution_enabled=False; simulation_admitted=False.\n\n"
          f"Research: {result['research']['total']}; settled paper trades: {result['paper']['settled']}.\n\n"
          'All prices are hypothetical references; no exchange orders. See JSON for denominators, costs and limitations.\n\n'
          +'\n'.join('- '+line for line in result['limitations'])+'\n')
    (directory/(stem+'.md')).write_text(body,encoding='utf-8')
    text=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    (directory/(stem+'.html')).write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>F3 paper evaluation</title><h1>F3 paper evaluation — no real trading</h1><pre>'+html.escape(text)+'</pre></html>',encoding='utf-8')
    return str(directory/(stem+'.json'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["scan","_worker","report"])
    parser.add_argument("--registry",required=True,type=Path)
    parser.add_argument("--f2",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--max-seconds",type=int,default=180)
    parser.add_argument("--max-requests",type=int,default=40)
    args=parser.parse_args()
    limits=f2.ScanLimits(max_seconds=args.max_seconds,max_requests=args.max_requests)
    if args.command=="scan":
        command=[sys.executable,"-m","tradingagents.forward_paper","_worker","--registry",str(args.registry),"--f2",str(args.f2),"--output",str(args.output),"--max-seconds",str(limits.max_seconds),"--max-requests",str(limits.max_requests)]
        try: raise SystemExit(subprocess.run(command,timeout=limits.max_seconds,check=False).returncode)
        except subprocess.TimeoutExpired:
            print(json.dumps({"status":"TIME_BUDGET_EXHAUSTED","execution_enabled":False}));raise SystemExit(4)
    try:
        if args.command=="report":
            ledger=Ledger(args.output)
            result=report(list(Registry(args.registry).registrations()),ledger.load())
        else: result=scan(args.registry,args.f2,args.output,limits)
        print(json.dumps(result,indent=2,allow_nan=False))
    except (ValueError,KeyError,OSError,okx.VendorUnavailableError,f2.BudgetExhausted) as exc:
        print(json.dumps({"status":"PAPER_UNAVAILABLE","error_type":type(exc).__name__,"execution_enabled":False}));raise SystemExit(3)


if __name__=="__main__":main()
