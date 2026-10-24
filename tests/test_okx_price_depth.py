"""V2.3 rules, continuity and native integration; no real network in unit tests."""
import asyncio
import copy
import json

import pandas as pd
import pytest

from tests.test_okx_research import INFO, NOW, DAY, SYMBOL
from tradingagents.dataflows.vendors import okx_price_depth as f


@pytest.fixture(autouse=True)
def clock(monkeypatch):monkeypatch.setattr(f.okx,'utc_ms',lambda:NOW)


def bundle():
    step=3600000;end=NOW//step*step
    rows=[{'ts':end-(250-i)*step,'Open':100.,'High':102.,'Low':98.,'Close':100.,'Volume':1.} for i in range(250)]
    return {'status':'VALID','records':rows,'interval':'1h','source':'OKX validated','snapshot_id':'core','cutoff_ms':NOW,
        'latest_close_ms':end,'symbol':SYMBOL,'instrument':INFO,'historical':False}


def stub_rules(monkeypatch,b,trend='UP',atr=10):
    t=b['records'][-1]['ts']
    highs=[105,110] if trend=='UP' else [115,110];lows=[85,90] if trend=='UP' else [95,90]
    original={'confirmed_pivots':{k:[{'price':v,'confirmed_at_ms':t-3600000} for v in values] for k,values in [('highs',highs),('lows',lows)]},'events':{'existing':True}}
    monkeypatch.setattr(f,'analyze_timeframe',lambda *a:copy.deepcopy(original))
    monkeypatch.setattr(f.okx,'indicator_frame',lambda *a:pd.DataFrame({'atr':[10.]*249+[float(atr)]}))
    return original


@pytest.mark.parametrize('trend,prev,close,kind,direction',[
    ('UP',109,111,'BOS','UP'),('DOWN',109,111,'CHoCH','UP'),
    ('DOWN',91,89,'BOS','DOWN'),('UP',91,89,'CHoCH','DOWN'),
    ('UP',111,109,'BREAKOUT_FAILED_NEXT_BAR','UP'),('DOWN',89,91,'BREAKOUT_FAILED_NEXT_BAR','DOWN')])
def test_confirmed_breaks_and_failures(monkeypatch,trend,prev,close,kind,direction):
    b=bundle();stub_rules(monkeypatch,b,trend);b['records'][-2]['Close']=prev;b['records'][-1].update(Close=close,High=max(112,close),Low=min(88,close))
    out=f.price_action(b,{})
    event=next(e for e in out['signals'] if e['type']==kind and e['direction']==direction)
    assert event['confirmed_at_ms']==b['latest_close_ms'] and event['invalidation']
    if kind in {'BOS','CHoCH'}:assert event['next_observable_bar_open_ms']==event['confirmed_at_ms']


def test_pivot_confirmed_during_current_bar_cannot_trigger(monkeypatch):
    b=bundle();o=stub_rules(monkeypatch,b)
    o['confirmed_pivots']['highs']=[{'price':101.,'confirmed_at_ms':b['latest_close_ms']}]
    b['records'][-1]['Close']=103
    monkeypatch.setattr(f,'analyze_timeframe',lambda *a:o)
    r=f.price_action(b,{})
    assert r['resistance'] is None and not any(e['type'] in {'BOS','CHoCH'} for e in r['signals'])


def test_sweep_is_price_clue_not_proven_execution(monkeypatch):
    b=bundle();stub_rules(monkeypatch,b);b['records'][-1].update(High=111,Close=109)
    e=next(x for x in f.price_action(b,{})['signals'] if x['type']=='PRICE_SWEEP_REJECTION_CLUE')
    assert 'not proof' in e['scope'] and e['confirmed_at_ms']==b['latest_close_ms']


@pytest.mark.parametrize('atr,state',[(13,'EXPANDING'),(7,'CONTRACTING'),(10,'NORMAL')])
def test_volatility_expansion_contraction(monkeypatch,atr,state):
    b=bundle();stub_rules(monkeypatch,b,atr=atr)
    b['records'][-1].update(High=140,Low=60)
    r=f.price_action(b,{})
    assert r['volatility_state']==state and r['abnormal_true_range']


@pytest.mark.parametrize('trend,environment',[(True,'TRENDING'),(False,'RANGING')])
def test_path_efficiency(monkeypatch,trend,environment):
    b=bundle();stub_rules(monkeypatch,b)
    for i,row in enumerate(b['records'][-21:]):row['Close']=100+i if trend else 100+i%2
    assert f.price_action(b,{})['market_environment']==environment


@pytest.mark.parametrize('kind',['missing','future','warmup'])
def test_bad_history_blocked(monkeypatch,kind):
    b=bundle();stub_rules(monkeypatch,b)
    if kind=='missing':b['records'][20]['ts']+=1
    if kind=='future':b['records'][-1]['ts']=NOW
    if kind=='warmup':b['records']=b['records'][-20:]
    with pytest.raises(ValueError):f.price_action(b,{})


def snapshot(seq=1,ts=NOW-1000):
    return {'seqId':seq,'prevSeqId':-1,'ts':str(ts),'checksum':0,
        'asks':[['101','100','0','1'],['102','100','0','1']],
        'bids':[['100','100','0','1'],['99','100','0','1']]}


def update(seq=2,prev=1,ts=NOW):return {'seqId':seq,'prevSeqId':prev,'ts':str(ts),'asks':[],'bids':[]}


def test_book_snapshot_update_delete_heartbeat_units():
    t=f.BookTape(INFO);t.apply('snapshot',snapshot(),NOW)
    r=update();r['asks']=[['101','0','0','0']];t.apply('update',r,NOW)
    assert len(t.sides['asks'])==1 and t.points[0]['ask_top20_base']==pytest.approx(2.)
    t.apply('update',update(seq=2,prev=2),NOW)
    assert len(t.points)==2 and t.summary([])['status']=='VALID'
    assert t.summary([])['possible_absorption_clue'] is None


@pytest.mark.parametrize('kind',['gap','reset','future','retrograde','crossed','tick','nan','duplicate','nonempty_heartbeat'])
def test_untrusted_update_not_committed(kind):
    t=f.BookTape(INFO);t.apply('snapshot',snapshot(),NOW);before=copy.deepcopy(t.points);r=update()
    if kind=='gap':r['prevSeqId']=0
    if kind=='reset':r['seqId']=0
    if kind=='future':r['ts']=str(NOW+1001)
    if kind=='retrograde':r['ts']=str(NOW-1001)
    if kind=='crossed':r['bids']=[['103','1','0','1']]
    if kind=='tick':r['asks']=[['101.001','1','0','1']]
    if kind=='nan':r['asks']=[['101','NaN','0','1']]
    if kind=='duplicate':r['asks']=[['101','1','0','1']]*2
    if kind=='nonempty_heartbeat':r.update(seqId=1,asks=[['101','1','0','1']])
    with pytest.raises(ValueError):t.apply('update',r,NOW)
    assert t.points==before and t.seq==1


def test_update_without_snapshot_and_repeat_snapshot_rejected():
    t=f.BookTape(INFO)
    with pytest.raises(ValueError):t.apply('update',update(),NOW)
    t.apply('snapshot',snapshot(),NOW)
    with pytest.raises(ValueError):t.apply('snapshot',snapshot(),NOW)


def test_displayed_large_size_not_identified_cancellation():
    t=f.BookTape(INFO);t.apply('snapshot',snapshot(),NOW);r=update();r['asks']=[['101','100000','0','1']];t.apply('update',r,NOW)
    assert t.changes and 'not identified' in t.changes[0]['scope']
    summary=t.summary([{'ts':NOW,'base':1.,'side':'buy'}])
    assert summary['observed_delta_base']==1 and 'not reconciled' in summary['trade_coverage']


def test_cutoff_expiry_tamper_and_report(monkeypatch,tmp_path):
    from tradingagents.reporting import write_report_tree
    b=bundle();stub_rules(monkeypatch,b)
    s={'status':'VALID','segments':[{'end_received_ms':NOW+1,'points':[],'trades':[]}]}
    raw=f.collect_price_depth(b,{},s)
    assert raw['dynamic_book']['status']=='RESEARCH_INVALID' and raw['status']=='PARTIAL'
    stale=f.evidence_view(raw,NOW+300001)
    assert stale['status']=='STALE_DATA' and not stale['timeframes']
    state={'company_of_interest':SYMBOL,'trade_date':DAY,'price_depth_evidence':raw,
        'final_rating':'REVIEW','final_trade_decision':'WAIT','execution_enabled':False}
    write_report_tree(state,SYMBOL,tmp_path)
    assert json.loads((tmp_path/'price_depth_snapshot.json').read_text())['original']==raw
    assert 'Price action / dynamic book / volatility' in (tmp_path/'complete_report.html').read_text(encoding='utf-8')
    raw['ttl_seconds']=999;assert f.evidence_view(raw)['status']=='RESEARCH_INVALID'


def test_historical_does_not_substitute_live_book(monkeypatch):
    b=bundle();stub_rules(monkeypatch,b);b['historical']=True
    r=f.collect_price_depth(b,{}, {'status':'VALID'})
    assert r['dynamic_book']['status']=='DATA_UNAVAILABLE'
    assert f.prepare_depth(SYMBOL,'2001-01-01',{})['status']=='DATA_UNAVAILABLE'


def test_continuity_gap_suppresses_segment_and_reconnects_fresh(monkeypatch):
    from websockets.asyncio import client
    from tests.test_okx_market_checks import local_loop
    attempts=[]
    class WS:
        def __init__(self,attempt):
            self.rows=[('snapshot',snapshot()),('update',update())]
            if attempt==0:self.rows.append(('update',update(seq=4,prev=999)))
        async def send(self,text):assert json.loads(text)['args'][0]['channel']=='books'
        async def recv(self):
            if not self.rows:await asyncio.sleep(10)
            action,row=self.rows.pop(0)
            return json.dumps({'arg':{'channel':'books','instId':SYMBOL},'action':action,'data':[row]})
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    def connect(*a,**k):
        index=len(attempts);attempts.append(1);return WS(index)
    monkeypatch.setattr(client,'connect',connect)
    r=asyncio.run(f.sample_book(INFO,2),loop_factory=lambda:local_loop(monkeypatch))
    assert len(attempts)==2 and r['reconnects']==1
    assert r['segments'][0]['summary']['status']=='RESEARCH_INVALID'
    assert r['segments'][1]['summary']['status']=='VALID' and r['status']=='PARTIAL'
    assert not r['continuous_across_segments']


def test_native_graph_and_default_switch(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    model,b,config=graph_setup.__wrapped__(monkeypatch,tmp_path)
    raw=f.collect_price_depth(b,{}, {'status':'DATA_UNAVAILABLE'})
    assert raw['timeframes']['1h']['status']=='VALID'
    calls=[];monkeypatch.setattr(f,'prepare_depth',lambda *a:calls.append('prepare'))
    monkeypatch.setattr(f,'collect_price_depth',lambda *a:calls.append('collect') or raw)
    g=TradingAgentsGraph(['market'],config=config);state,_=g.propagate(SYMBOL,DAY,'crypto')
    assert not calls and 'price_depth_evidence' not in state
    config['okx_price_depth_enabled']=True
    g=TradingAgentsGraph(['market'],config=config);state,_=g.propagate(SYMBOL,DAY,'crypto')
    assert calls==['prepare','collect'] and state['price_depth_evidence']==raw
    assert len(model.calls)>=9 and not state['execution_enabled']
    assert g.run_settings()['okx_price_depth_enabled']


def test_exact_bar_flow_only_and_no_stale_derived_values(monkeypatch):
    from tradingagents.dataflows.vendors.okx_orderflow import digest
    b=bundle();stub_rules(monkeypatch,b)
    flow={'status':'VALID','start_ms':b['records'][-1]['ts'],'end_ms':b['latest_close_ms'],
        'fetched_at_ms':NOW,'ttl_seconds':300,'base_unit':'BTC','issues':[],
        'markets':{'swap':{'status':'VALID','statistics':{'delta_base':2}}}}
    flow['snapshot_id']=digest(flow)
    raw=f.collect_price_depth(b,{},flow=flow)
    assert raw['same_bar_orderflow']['1h']['delta_base']==2
    stale=f.evidence_view(raw,NOW+300001)
    assert 'same_bar_orderflow' not in stale and 'orderflow_reference' not in stale
    assert stale['orderflow_joint_status']=='STALE_DATA'
    flow['start_ms']+=60000;flow['snapshot_id']=digest({k:v for k,v in flow.items() if k!='snapshot_id'})
    raw=f.collect_price_depth(b,{},flow=flow)
    assert 'same_bar_orderflow' not in raw


def test_cli_opt_in_and_core_gate(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from typer.testing import CliRunner
    from cli import main
    _,b,config=graph_setup.__wrapped__(monkeypatch,tmp_path)
    raw=f.collect_price_depth(b,{})
    calls=[];monkeypatch.setattr(f,'prepare_depth',lambda *a:calls.append('prepare'))
    monkeypatch.setattr(f,'collect_price_depth',lambda *a:calls.append('collect') or raw)
    monkeypatch.setattr(main,'DEFAULT_CONFIG',config)
    r=CliRunner().invoke(main.app,['okx-research','--date',DAY,'--data-only','--price-depth','--depth-seconds','0'])
    assert r.exit_code==0 and calls==['prepare','collect'],r.output
    assert 'execution_enabled=False' in r.output
