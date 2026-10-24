import copy
import json

import pytest

from tradingagents import okx_signal_audit as f
from tests.test_okx_traded_comparison import fixture
from tests.test_okx_feedback import record
from tradingagents.memory.okx_simulation import OKXSimulationMemory


def test_existing_simulation_outcomes_and_small_sample_not_probability():
    inputs,ledger,_=fixture();asof=inputs['end']+1000
    r=f.ledger_statistics(ledger,asof,source_available_at_ms=asof)
    assert r['trade_outcomes']['trades']==len(ledger['trades'])
    assert r['trade_outcomes']['predictive_success_probability'] is None
    assert r['trade_outcomes']['sample_status']=='INSUFFICIENT_FOR_INFERENCE'
    assert r['probability_calibration']['status']=='NOT_COMPUTED'
    assert r['news_relationship'].startswith('NOT_IDENTIFIABLE')
    assert r['trade_outcomes']['simulated_trade_net_sum_usdt']==pytest.approx(sum(t['net_pnl'] for t in ledger['trades']))


def test_future_archive_and_labels_cannot_be_read():
    inputs,ledger,_=fixture();cutoff=ledger['trades'][0]['resolved_at_ms']-1
    with pytest.raises(ValueError):f.ledger_statistics(ledger,cutoff,source_available_at_ms=cutoff+1)
    r=f.ledger_statistics(ledger,cutoff,source_available_at_ms=cutoff)
    assert r['trade_outcomes']['trades']==0


@pytest.mark.parametrize('kind',['duplicate','ownership','future','real'])
def test_bad_ledger_blocked(kind):
    inputs,l,_=fixture();l=copy.deepcopy(l)
    if kind=='duplicate':l['decisions'].append(copy.deepcopy(l['decisions'][0]))
    if kind=='ownership':l['trades'][0]['snapshot_id']='foreign'
    if kind=='future':l['decisions'][0]['evidence_latest_ms']=l['decisions'][0]['cutoff_ms']+1
    if kind=='real':l['execution_enabled']=True
    with pytest.raises(ValueError):f.ledger_statistics(l,inputs['end']+1000,source_available_at_ms=inputs['end'])


def test_memory_reuses_availability_filter_no_rejected_profit_or_future_reflection(tmp_path):
    memory=OKXSimulationMemory(tmp_path);r=record(decision='REJECT');memory.store_feedback(r)
    before=(tmp_path/'okx_simulation/memory.md').read_bytes()
    assert f.memory_statistics(tmp_path,r.available_at_ms-1)['records']==0
    result=f.memory_statistics(tmp_path,r.available_at_ms)
    assert result['decision_counts']=={'REJECT':1}
    assert result['predictive_success_probability'] is None
    assert (tmp_path/'okx_simulation/memory.md').read_bytes()==before


def snapshot_report(path,events=None):
    from unittest.mock import patch
    from tests.test_okx_research import CONFIG,NOW,DAY,SYMBOL,FakeClient
    from tradingagents.dataflows.vendors import okx
    path.mkdir()
    with patch.object(okx,'utc_ms',return_value=NOW):
        core=okx.build_bundle(SYMBOL,DAY,{**CONFIG,'okx_multi_timeframe':True,'okx_interval':'4h'},FakeClient())
    assert core['status']=='VALID'
    (path/'multitimeframe_snapshot.json').write_text(json.dumps(core))
    if events is not None:
        from tradingagents.dataflows.vendors.okx_events import bind_events
        raw=bind_events({'events':events,'sources':{'test':{'status':'VALID'}},'lookback_days':14},
                        {'status':'VALID','historical':False,'cutoff_ms':core['cutoff_ms'],'snapshot_id':core['snapshot_id']})
        (path/'events_snapshot.json').write_text(json.dumps({'original':raw}))
    return f.freeze_report(path)


def test_fixed_snapshot_diff_no_fake_ab_performance(tmp_path):
    a=snapshot_report(tmp_path/'a');b=snapshot_report(tmp_path/'b',[])
    d=f.compare_snapshots(a,b)
    assert d['changed_artifacts']==['events_snapshot.json'] and not d['added_event_ids']
    assert 'Not a controlled' in d['scope']
    assert f.freeze_report(tmp_path/'a')==a
    f.write(tmp_path/'fixed.json',a);f.write(tmp_path/'fixed.json',a)
    with pytest.raises(ValueError):f.write(tmp_path/'fixed.json',b)


def test_comparison_requires_same_cutoff_and_detects_tampering(tmp_path):
    from tradingagents.dataflows.vendors.okx_orderflow import digest
    a=snapshot_report(tmp_path/'a');b=copy.deepcopy(a);b['cutoff_ms']+=1
    b['snapshot_id']=digest({k:v for k,v in b.items() if k!='snapshot_id'})
    with pytest.raises(ValueError):f.compare_snapshots(a,b)
    a['cutoff_ms']=999
    with pytest.raises(ValueError):f.compare_snapshots(a,b)


def test_fixed_snapshot_rejects_future_event(tmp_path):
    from tradingagents.dataflows.vendors.okx_orderflow import digest
    snapshot_report(tmp_path/'a',[])
    p=tmp_path/'a/events_snapshot.json';v=json.loads(p.read_text());r=v['original']
    r['events']=[{'event_id':'future','published_at_ms':r['cutoff_ms']+1,'first_seen_ms':r['cutoff_ms']-1}]
    r['snapshot_id']=digest({k:v for k,v in r.items() if k!='snapshot_id'})
    p.write_text(json.dumps(v))
    with pytest.raises(ValueError):f.freeze_report(tmp_path/'a')
