"""Offline descriptive audits and fixed-input comparisons; no strategy fitting.

Reuses the existing ledger outcome selector and isolated simulation Memory.
Never supplies retrospective results to the live research Graph or Reflection.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter,defaultdict
from pathlib import Path

from tradingagents.okx_backtest import feedback
from tradingagents.memory.okx_simulation import OKXSimulationMemory, as_ms
from tradingagents.dataflows.vendors.okx_orderflow import digest

FILES=('multitimeframe_snapshot.json','events_snapshot.json','orderflow_snapshot.json',
       'market_checks_snapshot.json','price_depth_snapshot.json','research_result.json')


def read(path):
    path=Path(path)
    if path.stat().st_size>32*1024*1024:raise ValueError('Audit input byte budget exceeded')
    return json.loads(path.read_text(encoding='utf-8'))


def write(path,value):
    path=Path(path);body=json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n'
    if path.exists():
        if path.read_text(encoding='utf-8')!=body:raise ValueError('Existing audit differs; choose a new output path')
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as out:out.write(body)


def trade_summary(rows):
    pnl=[float(t['net_pnl']) for t in rows]
    if not all(math.isfinite(v) for v in pnl):raise ValueError('Nonfinite simulated outcome')
    return {'trades':len(rows),'wins':sum(v>0 for v in pnl),'losses':sum(v<0 for v in pnl),
        'flat':sum(v==0 for v in pnl),'simulated_trade_net_sum_usdt':sum(pnl),
        'predictive_success_probability':None,
        'sample_status':'INSUFFICIENT_FOR_INFERENCE' if len(rows)<30 else 'DESCRIPTIVE_ONLY_NO_OUT_OF_SAMPLE_CLAIM'}


def ledger_statistics(result,as_of,*,source_available_at_ms):
    as_of=as_ms(as_of)
    if source_available_at_ms>as_of:raise ValueError('Archive not available at evaluation cutoff')
    if result.get('execution_enabled') is not False or not result.get('simulated'):
        raise ValueError('Only offline simulated ledgers accepted')
    decisions=[d for d in result.get('decisions',[]) if d['cutoff_ms']<=as_of]
    index={d['snapshot_id']:d for d in decisions}
    if len(index)!=len(decisions):raise ValueError('Duplicate decision snapshot ownership')
    actions=Counter();reasons=Counter();groups=defaultdict(list)
    for d in decisions:
        if d.get('evidence_latest_ms',d['cutoff_ms'])>d['cutoff_ms']:raise ValueError('Future decision evidence')
        if d.get('diagnostic',{}).get('data_quality') is False:action='DATA_INVALID'
        elif d.get('candidate'):
            if d['candidate']['direction'] not in {'LONG','SHORT'}:raise ValueError('Unknown candidate action; SELL is not OPEN_SHORT')
            action='OPEN_'+d['candidate']['direction']+'_CANDIDATE'
        else:action='WAIT'
        actions[action]+=1;reasons[d.get('reason','UNRECORDED')]+=1
    trades=feedback(result,as_of)
    for t in trades:
        owner=index.get(t['snapshot_id'])
        if not owner or not owner.get('candidate') or owner['candidate']['direction']!=t['direction'] or owner['cutoff_ms']!=t['research_cutoff_ms'] or t['entry_ms']<owner['cutoff_ms']:
            raise ValueError('Trade/candidate ownership mismatch')
        groups['direction:'+t['direction']].append(t)
        groups['regime:'+str(t.get('market_regime','UNRECORDED'))].append(t)
    traded={t['snapshot_id'] for t in trades}
    events=[e for e in result.get('events',[]) if e['ts']<=as_of]
    return {'version':'okx-signal-descriptive-v1','evaluation_as_of_ms':as_of,'source_available_at_ms':source_available_at_ms,
        'strategy_version':result['strategy_version'],'strategy_fingerprint':result.get('strategy_fingerprint'),
        'configuration_fingerprint':result.get('configuration_fingerprint'),'ledger_hash':digest(result),
        'runtime_versions':result.get('runtime_versions'),'model_version':'NO_LLM_RULE_BASELINE',
        'candidate_snapshot_ids':[d['snapshot_id'] for d in decisions if d.get('candidate')],
        'data_partition':'PREVIOUSLY_INSPECTED_DEVELOPMENT_DATA','execution_enabled':False,
        'decision_count':len(decisions),'action_counts':dict(actions),'reason_counts':dict(reasons),
        'decision_data_quality':dict(Counter(str(d.get('diagnostic',{}).get('data_quality','UNRECORDED')) for d in decisions)),
        'wait_fraction_descriptive':actions.get('WAIT',0)/len(decisions) if decisions else None,
        'candidate_without_resolved_trade':sum(bool(d.get('candidate')) and d['snapshot_id'] not in traded for d in decisions),
        'lifecycle_counts':dict(Counter(e['type'] for e in events)),
        'trade_outcomes':trade_summary(trades),'groups':{k:trade_summary(v) for k,v in groups.items()},
        'volatility_environment':'UNRECORDED_IN_ORIGINAL_STRATEGY; no retrospective relabelling',
        'news_relationship':'NOT_IDENTIFIABLE; original ledger has no publication-time event features',
        'directional_signal_quality':'No fixed-horizon market-return labels; simulated fills are a different target',
        'probability_calibration':{'status':'NOT_COMPUTED','reason':'No issued probabilities and matching point-in-time prediction/label definition; no Brier/ECE or fitted success rates'},
        'limitations':['Counts are not independent strategy samples or future probabilities',
            'WAIT, data failure, unfilled candidates and trade outcomes remain separate',
            'Counterfactual Agent filtering requires continuous replay; this is the unchanged baseline ledger',
            'Historical contract/funding/cost assumptions remain those of the source ledger',
            'No strategy optimization, new blind-window evaluation or live Memory update']}


def memory_statistics(root,as_of):
    if not (Path(root)/'okx_simulation/memory.md').is_file():raise ValueError('Simulation Memory unavailable; not an empty validated sample')
    rows=OKXSimulationMemory(root).load_feedback(as_of=as_of,include_diagnostics=True)
    return {'version':'okx-feedback-descriptive-v1','evaluation_as_of_ms':as_ms(as_of),'execution_enabled':False,
        'records':len(rows),'research_status':dict(Counter(r.research_status for r in rows)),
        'decision_counts':dict(Counter(r.decision or 'NO_COMPLETE_DECISION' for r in rows)),
        'outcome_counts':dict(Counter(r.outcome_status for r in rows)),
        'decision_outcome_pairs':dict(Counter(str(r.decision or 'NO_COMPLETE_DECISION')+':'+r.outcome_status for r in rows)),
        'source_feedback_ids':[r.feedback_id for r in rows],
        'snapshot_ids':[r.snapshot_id for r in rows],'model_versions':dict(Counter(r.model_version for r in rows)),
        'strategy_versions':dict(Counter(r.strategy_version for r in rows)),
        'predictive_success_probability':None,
        'scope':'Original D.5 available_at filter; baseline outcomes for REJECT are counterfactual, not Agent portfolio profit. No Reflection/strategy mutation.'}


def freeze_report(directory):
    directory=Path(directory);artifacts={}
    for name in FILES:
        p=directory/name
        if p.exists():artifacts[name]=read(p)
    if 'multitimeframe_snapshot.json' not in artifacts:raise ValueError('Validated multi-timeframe report snapshot required')
    core=artifacts['multitimeframe_snapshot.json']
    if core.get('execution_enabled') is not False or core.get('status')!='VALID':raise ValueError('Valid research-only core required')
    from tradingagents.dataflows.vendors.okx import fingerprint,BARS
    if not core.get('multi_timeframe') or core['snapshot_id']!=fingerprint(core):raise ValueError('Core fingerprint mismatch')
    if set(core.get('timeframes',{}))!={'1h','4h','1d'}:raise ValueError('Required timeframe missing')
    for interval,child in core['timeframes'].items():
        if child['snapshot_id']!=fingerprint(child) or child['cutoff_ms']!=core['cutoff_ms'] or child['status']!='VALID':
            raise ValueError('Child snapshot identity/quality mismatch')
        if any(r['ts']+BARS[interval][1]>core['cutoff_ms'] for r in child['records']):raise ValueError('Future archived candle')
    if sum(len(json.dumps(v)) for v in artifacts.values())>16*1024*1024:raise ValueError('Combined snapshot budget exceeded')
    events=artifacts.get('events_snapshot.json',{}).get('original')
    if events:
        if events['core_snapshot_id']!=core['snapshot_id'] or events['cutoff_ms']!=core['cutoff_ms']:
            raise ValueError('Event/core snapshot mismatch')
        from tradingagents.dataflows.vendors.okx_events import evidence_view
        if evidence_view(events,events['fetched_at_ms'])['status']=='RESEARCH_INVALID':raise ValueError('Event fingerprint mismatch')
        if any(e['published_at_ms']>core['cutoff_ms'] or e['first_seen_ms']>core['cutoff_ms'] for e in events['events']):
            raise ValueError('Future publication in fixed snapshot')
    value={'version':'okx-fixed-research-input-v1','instrument':core['symbol'],'cutoff_ms':core['cutoff_ms'],
        'core_snapshot_id':core['snapshot_id'],'execution_enabled':False,'artifacts':artifacts,
        'artifact_hashes':{k:digest(v) for k,v in artifacts.items()},
        'scope':'Exact archived inputs and outputs, not a new historical prediction or model replay; original publisher revision history unavailable'}
    value['snapshot_id']=digest(value)
    return value


def compare_snapshots(left,right):
    for value in [left,right]:
        if digest({k:v for k,v in value.items() if k!='snapshot_id'})!=value.get('snapshot_id'):raise ValueError('Fixed snapshot hash mismatch')
        if {k:digest(v) for k,v in value['artifacts'].items()}!=value['artifact_hashes']:raise ValueError('Artifact content hash mismatch')
    for key in ['instrument','cutoff_ms','core_snapshot_id']:
        if left[key]!=right[key]:raise ValueError('Comparison requires exactly the same core and cutoff')
    a,b=left['artifact_hashes'],right['artifact_hashes']
    event_ids=lambda s:{e['event_id'] for e in s['artifacts'].get('events_snapshot.json',{}).get('original',{}).get('events',[])}
    return {'version':'okx-fixed-evidence-diff-v1','execution_enabled':False,'left':left['snapshot_id'],'right':right['snapshot_id'],
        'cutoff_ms':left['cutoff_ms'],'core_snapshot_id':left['core_snapshot_id'],
        'changed_artifacts':[n for n in sorted(set(a)|set(b)) if a.get(n)!=b.get(n)],
        'added_event_ids':sorted(event_ids(right)-event_ids(left)),'removed_event_ids':sorted(event_ids(left)-event_ids(right)),
        'left_action':left['artifacts'].get('research_result.json',{}).get('proposed_action'),
        'right_action':right['artifacts'].get('research_result.json',{}).get('proposed_action'),
        'scope':'Evidence/output diff only. Not a controlled A/B model experiment or proof of accuracy/profit improvement.'}


def main():
    p=argparse.ArgumentParser(description=__doc__);commands=p.add_subparsers(dest='command',required=True)
    for name in ['ledger','memory']:
        s=commands.add_parser(name);s.add_argument('--input' if name=='ledger' else '--root',required=True);s.add_argument('--as-of',required=True);s.add_argument('--output',required=True)
    s=commands.add_parser('freeze');s.add_argument('--report-dir',required=True);s.add_argument('--output',required=True)
    s=commands.add_parser('compare');s.add_argument('--left',required=True);s.add_argument('--right',required=True);s.add_argument('--output',required=True)
    a=p.parse_args()
    if a.command=='ledger':result=ledger_statistics(read(a.input),a.as_of,source_available_at_ms=int(Path(a.input).stat().st_mtime*1000))
    elif a.command=='memory':result=memory_statistics(a.root,a.as_of)
    elif a.command=='freeze':result=freeze_report(a.report_dir)
    else:result=compare_snapshots(read(a.left),read(a.right))
    write(a.output,result);print(a.output)


if __name__=='__main__':main()
