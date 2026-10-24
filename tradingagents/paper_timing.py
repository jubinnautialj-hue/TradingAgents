"""R1.2 targeted, bounded warm scheduling over unchanged forward-paper-v1.

This adapter only refreshes/fills already SCHEDULED paper states. Admission,
prices, costs, fill semantics and all settlement remain native F3 functions.
"""
from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from tradingagents import forward_paper as paper, forward_tracking as tracking
from tradingagents import auto_research as r11
from tradingagents.dataflows.files import locked
from tradingagents.research_budget import Meter, guards, ResearchBudgetExceeded
from tradingagents.automation import readonly_registry,readonly_ledger,paused,expand


def scheduled(sources):
    result=[]
    for source in sources:
        ledger=readonly_ledger(source['f3'])
        for state in ledger.states(ledger.load()).values():
            if state.status=='SCHEDULED':result.append((state.planned_entry_ms,source,state.registration_id))
    return sorted(result,key=lambda row:(row[0],row[1]['name'],row[2]))


def step(cfg,source,rid,phase,market,*,scheduled_for_ms):
    """One native-ledger transaction. Re-read after restart; never infer a fill."""
    started=r11.utc_ms();ledger=paper.Ledger(source['f3'])
    if paused(cfg,'timely'):return {'status':'PAUSED','registration_id':rid}
    with locked(ledger.path,wait=False) as held:
        if not held:return {'status':'LEDGER_BUSY','source':source['name'],'registration_id':rid}
        events=ledger.load();state=ledger.states(events).get(rid)
        if state is None or state.status!='SCHEDULED':return {'status':'NOT_SCHEDULED','registration_id':rid}
        result={'source':source['name'],'registration_id':rid,'phase':phase,'scheduled_for_ms':scheduled_for_ms,
                'actual_started_ms':started,'dispatch_lag_ms':started-scheduled_for_ms,'planned_entry_ms':state.planned_entry_ms}
        try:
            registry=readonly_registry(source['registry']);record=registry.load(rid)
            conditions,reasons,_=paper._verified_inputs(registry,source['f2'],record,started,existing=True)
            if (record.instrument!='BTC-USDT-SWAP' or state.namespace!='PRODUCTION'
                or record.content_hash!=state.identity['registration_hash']
                or not any(c['event_id']==state.trigger['event_id'] and c==state.trigger for c in conditions)):
                raise ValueError('Paper source/condition admission mismatch')
            now=market.now()
            if abs(now-r11.utc_ms())>30000 or now<events[-1]['recorded_ms']:raise ValueError('Clock drift/rollback')
            target=state.planned_entry_ms
            if now>=state.trigger['hypothesis_expires_ms']:
                state=state.model_copy(deep=True);state.status='CANCELLED';state.reason='PLAN_EXPIRED'
            elif now>target+paper.PROTOCOL.opening_delivery_ms:
                state=state.model_copy(deep=True);state.status='MISSED';state.reason='MISSED_PLANNED_OPEN_NO_BACKFILL'
            else:
                reasons=paper.admission(record,state.trigger,now)
                if reasons:raise ValueError('Original paper admission no longer valid')
                end=now//paper.HOUR*paper.HOUR
                batch=market.bars(record.instrument,record.first_bar_open_ms,end)
                if not paper.check_structure(state,batch['bars'],now):
                    state=state.model_copy(deep=True);state.status='CANCELLED';state.reason='STRUCTURE_INVALIDATED'
                elif phase=='refresh':
                    if now>=target or target-now>paper.PROTOCOL.revalidation_ttl_ms:
                        raise ValueError('Refresh outside original pre-opening TTL window')
                    proof=market.refresh(record,state.trigger,now)
                    if proof['completed_ms']>=target or not paper.revalidation_ready(proof,state.trigger,r11.utc_ms(),before=target):
                        raise ValueError('Pre-entry validation late/stale/incomplete')
                    state=state.model_copy(deep=True);state.revalidation=proof
                    result['revalidation_completed_ms']=proof['completed_ms']
                elif phase=='open':
                    if now<target:raise ValueError('Opening has not happened')
                    # Native fill repeats original strict time, TTL, geometry,
                    # tick/lot, precommit and quantity/cash checks.
                    opening=market.opening(record.instrument,target)
                    if paused(cfg,'timely'):raise ValueError('Timely observation paused before fill')
                    equity=paper.PROTOCOL.initial_equity+sum(s.exit.get('net_pnl',0) for s in ledger.states(events).values() if s.group==state.group and s.status=='SETTLED')
                    state=paper.fill(state,opening,r11.utc_ms(),equity)
                    result.update(price_observed_ms=opening['observed_ms'],hypothetical_fill_ms=state.position['entry_ms'])
                else:raise ValueError('Unknown timely phase')
            result.update(status=state.status,reason=state.reason)
            ledger.append(events,rid,state.status,{'state':state.model_dump(),'scheduler_evidence':result},r11.utc_ms())
        except (ValueError,KeyError,OSError,tracking.BudgetExhausted,tracking.okx.VendorUnavailableError,ResearchBudgetExceeded) as exc:
            result.update(status='PAUSED',error_type=type(exc).__name__)
            ledger.append(events,rid,'PAUSED',{'reason':'R12_TIMELY_DATA_OR_PROTOCOL_UNAVAILABLE','error_type':type(exc).__name__,
                                           'scheduler_evidence':result},r11.utc_ms())
        return result


def warm_window(cfg,sources,*,market=None,sleep=time.sleep):
    """At most one upcoming boundary. No permanent polling, no model or orders."""
    started=r11.utc_ms();rows=scheduled(sources);limit=cfg.schedule
    eligible=[row for row in rows if row[0]-limit.warm_lead_ms<=started<=row[0]+paper.PROTOCOL.opening_delivery_ms]
    # A restart after the window may only ask native rules to record MISSED.
    overdue=[row for row in rows if started>row[0]+paper.PROTOCOL.opening_delivery_ms]
    selected=(eligible+overdue)[:limit.max_due]
    if not selected:return {'status':'NO_PENDING_OPEN_IN_WINDOW','execution_enabled':False,'due_count':len(rows)}
    root=expand(cfg.state_root)/'timely_runs';root.mkdir(parents=True,exist_ok=True)
    meter=Meter(root/(str(started)+'_'+uuid4().hex+'.meter.json'),0,limit.timely_http_cap,limit.timely_timeout)
    native_limits=tracking.ScanLimits(max_requests=limit.timely_http_cap,max_seconds=limit.timely_timeout)
    market=market or paper.Market(tracking.PublicBudgetClient(native_limits),native_limits)
    results=[];deadline=time.monotonic()+limit.timely_timeout
    with guards(meter):
        live=[row for row in selected if row[0]>=r11.utc_ms()-paper.PROTOCOL.opening_delivery_ms]
        for target in sorted({row[0] for row in live}):
            group=[row for row in live if row[0]==target]
            if paused(cfg,'timely'):return {'status':'PAUSED','results':results,'execution_enabled':False}
            refresh_at=target-limit.refresh_lead_ms
            while r11.utc_ms()<refresh_at and time.monotonic()<deadline:
                if paused(cfg,'timely'):return {'status':'PAUSED','results':results,'execution_enabled':False}
                sleep(min(.25,max(.001,(refresh_at-r11.utc_ms())/1000)))
            if time.monotonic()>=deadline:break
            if r11.utc_ms()<target:
                for _,source,rid in group:
                    results.append(step(cfg,source,rid,'refresh',market,scheduled_for_ms=refresh_at))
            while r11.utc_ms()<target+1000 and time.monotonic()<deadline:
                if paused(cfg,'timely'):return {'status':'PAUSED','results':results,'execution_enabled':False}
                sleep(min(.25,max(.001,(target+1000-r11.utc_ms())/1000)))
            if time.monotonic()<deadline:
                for _,source,rid in group:results.append(step(cfg,source,rid,'open',market,scheduled_for_ms=target+1000))
        for target,source,rid in selected:
            if (target,source,rid) in overdue and time.monotonic()<deadline and not paused(cfg,'timely'):
                results.append(step(cfg,source,rid,'open',market,scheduled_for_ms=target+1000))
    return {'status':'WINDOW_COMPLETE','started_ms':started,'completed_ms':r11.utc_ms(),'results':results,
            'unprocessed_due':max(0,len(overdue+eligible)-len(selected)),'meter':meter.value,'execution_enabled':False,
            'limitation':'No scheduler or network latency guarantee; original 15s rule unchanged'}
