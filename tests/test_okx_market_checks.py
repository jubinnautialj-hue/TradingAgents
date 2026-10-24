"""Deterministic V2.2 tests: optional evidence cannot alter frozen plans."""
import copy
import json

import pytest

from tradingagents.dataflows.vendors import okx_market_checks as f
from tests.test_okx_research import INFO, NOW, SYMBOL, DAY

END=NOW//f.HOUR*f.HOUR


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(f.okx,'utc_ms',lambda:NOW)


def core():
    return {'status':'VALID','symbol':SYMBOL,'instrument':INFO,'snapshot_id':'original',
        'cutoff_ms':NOW,'historical':False,'interval':'1h','records':[
            {'Date':'2026-01-01','ts':END-f.HOUR,'Open':100.,'Close':110.,'Volume':2.,'VolumeQuote':210.}]}


class OKX:
    def __init__(self):self.calls=[]
    def get(self,path,params):
        self.calls.append((path,params))
        if 'long-short' in path:return [[str(END-i*f.HOUR),str(2+i/10)] for i in range(3)]
        if 'open-interest' in path:return [[str(END-i*f.HOUR),'100','1','100'] for i in range(3)]
        return [{'instId':SYMBOL,'instType':'SWAP','fundingTime':str(END-i*8*f.HOUR),
                 'realizedRate':'0.0001','fundingRate':'0.0002'} for i in range(3)]


class Binance:
    def __init__(self):self.calls=[]
    def get(self,path,params=None):
        self.calls.append((path,params))
        if path.endswith('/time'):return {'serverTime':NOW}
        if path.endswith('exchangeInfo'):return {'symbols':[{'symbol':'BTCUSDT','baseAsset':'BTC','quoteAsset':'USDT',
            'marginAsset':'USDT','contractType':'PERPETUAL','status':'TRADING'}]}
        if path.endswith('openInterestHist'):return [{'symbol':'BTCUSDT','timestamp':END-i*f.HOUR,'sumOpenInterest':str(10-i)} for i in range(3)]
        if 'AccountRatio' in path:return [{'symbol':'BTCUSDT','timestamp':END-i*f.HOUR,'longShortRatio':'2'} for i in range(3)]
        if path.endswith('fundingRate'):return [{'symbol':'BTCUSDT','fundingTime':END-i*8*f.HOUR,'fundingRate':'0.00015'} for i in reversed(range(3))]
        if path.endswith('klines'):return [[END-f.HOUR,'100','120','90','111','10',END-1,'1050',100,'6','630']]
        raise AssertionError(path)


def sample(side='sell',position='long',ts=NOW-1000):
    return {'arg':{'channel':'liquidation-orders'},'data':[{'instId':SYMBOL,'instType':'SWAP',
        'details':[{'side':side,'posSide':position,'sz':'3','bkPx':'100','ts':str(ts)}]}]}


@pytest.mark.parametrize('side,pos,expected',[('sell','long','LONG'),('buy','short','SHORT')])
def test_okx_liquidation_units_direction(side,pos,expected):
    row=f.liquidations(sample(side,pos),'OKX',INFO,NOW)[0]
    assert row['side_liquidated']==expected
    assert row['base_quantity']==pytest.approx(.03)
    assert row['sample_notional_usdt']==pytest.approx(3.)
    assert 'mark_price_proxy' in row['valuation_method']


def test_binance_snapshot_uses_filled_not_original_quantity():
    msg={'e':'forceOrder','o':{'s':'BTCUSDT','S':'SELL','q':'100','z':'2','ap':'100','T':NOW-1}}
    rows=f.liquidations(msg,'BINANCE',INFO,NOW)
    assert rows[0]['sample_notional_usdt']==200 and rows[0]['side_liquidated']=='LONG'
    msg['o']['z']='0';assert f.liquidations(msg,'BINANCE',INFO,NOW)==[]
    msg['o'].update(z='1',st=2);assert f.liquidations(msg,'BINANCE',INFO,NOW)==[]


@pytest.mark.parametrize('kind',['future','conflict','nan','overflow'])
def test_bad_liquidation(kind):
    msg=sample();r=msg['data'][0]['details'][0]
    r.update({'future':{'ts':str(NOW+1)},'conflict':{'posSide':'short'},'nan':{'sz':'NaN'},'overflow':{'sz':'1e308','bkPx':'1e308'}}[kind])
    with pytest.raises(ValueError):f.liquidations(msg,'OKX',INFO,NOW)


@pytest.mark.parametrize('rows',[[[END,1],[END,2]],[[END+f.HOUR,1],[END,2]],[[END,1],[END-2*f.HOUR,2]],[[END,1]]])
def test_series_missing_future_duplicate(rows):
    with pytest.raises(ValueError):f.series(rows,NOW,ratio=True)


def test_full_checks_matching_hour_oi_funding_and_no_core_mutation():
    b=core();before=copy.deepcopy(b);a=OKX();z=Binance()
    r=f.collect_checks(b,{},client=a,binance=z)
    assert b==before and r['status']=='PARTIAL' and not r['execution_enabled']
    assert all(p['status']=='VALID' for p in r['components'].values())
    p=r['components']['prices_volume']['data']
    assert p['binance_delta_base']==2 and p['binance_delta_usdt']==210
    assert p['binance_vs_okx_close_bps']==pytest.approx((111/110-1)*10000)
    assert r['comparison']['aligned_settlement']['difference_fraction']==pytest.approx(.00005)
    assert r['comparison']['binance_price_oi']['oi_change_fraction']==pytest.approx(10/9-1)
    assert all('topLongShort' not in path for path,_ in z.calls)


def test_historical_does_not_request_current_data():
    a=OKX();b=Binance();r=f.collect_checks({**core(),'historical':True},{},client=a,binance=b)
    assert r['status']=='DATA_UNAVAILABLE' and not a.calls and not b.calls
    assert f.prepare_liquidations(SYMBOL,'2001-01-01',{})['status']=='DATA_UNAVAILABLE'


def test_invalid_core_no_new_requests():
    a=OKX();b=Binance();f.collect_checks({**core(),'status':'PARTIAL'},{},client=a,binance=b)
    assert not a.calls and not b.calls


def test_identity_failure_not_silent_spot_fallback():
    class Wrong(Binance):
        def get(self,path,params=None):
            if path.endswith('exchangeInfo'):return {'symbols':[]}
            return super().get(path,params)
    r=f.collect_checks(core(),{},client=OKX(),binance=Wrong())
    assert r['components']['binance_identity']['status']=='DATA_UNAVAILABLE'
    assert 'prices_volume' not in r['components']


def test_stale_tamper_and_context_report(tmp_path):
    from tradingagents.agents.context import get_instrument_context_from_state
    from tradingagents.reporting import write_report_tree
    r=f.collect_checks(core(),{},client=OKX(),binance=Binance());original=copy.deepcopy(r)
    view=f.evidence_view(r,NOW+300001)
    assert view['status']=='STALE_DATA' and view['comparison'] is None
    assert all('data' not in p for p in view['components'].values()) and r==original
    state={'company_of_interest':SYMBOL,'instrument_context':'OKX','market_checks_evidence':r,
           'trade_date':DAY,'final_rating':'REVIEW','final_trade_decision':'WAIT','execution_enabled':False}
    assert 'heatmaps' in get_instrument_context_from_state(state)
    write_report_tree(state,SYMBOL,tmp_path)
    saved=json.loads((tmp_path/'market_checks_snapshot.json').read_text())
    assert saved['original']==r and not saved['execution_enabled']
    r['ttl_seconds']=999;assert f.evidence_view(r)['status']=='RESEARCH_INVALID'


def test_future_received_liquidation_invalidated():
    row=f.liquidations(sample(),'OKX',INFO,NOW)[0];row['received_ms']=NOW+1
    samples={'markets':[{'events':[row],'segments':[],'status':'PARTIAL'}]}
    r=f.collect_checks(core(),{},samples,OKX(),Binance())
    assert r['liquidations']['markets'][0]['status']=='RESEARCH_INVALID'
    assert not r['liquidations']['markets'][0]['events']


def test_no_events_is_not_zero_exchange_liquidations():
    samples={'markets':[{'events':[],'segments':[{'start_ms':NOW-2000,'end_ms':NOW-1000}],
                        'status':'PARTIAL','coverage_fraction':None,'exchange_complete':False}]}
    r=f.collect_checks(core(),{},samples,OKX(),Binance())
    m=r['liquidations']['markets'][0]
    assert m['summary']['observed_events']==0 and m['coverage_fraction'] is None and not m['exchange_complete']


def test_bounded_retry_cache_and_private_routes(monkeypatch):
    responses=[429,503,200];calls=[]
    class Session:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**kw):
            assert kw['allow_redirects'] is False and 'verify' not in kw
            calls.append(1)
            class Response:
                status_code=responses.pop(0);headers={}
                def json(self):return {'serverTime':NOW}
            return Response()
    monkeypatch.setattr(f.requests,'Session',Session);monkeypatch.setattr(f.time,'sleep',lambda _:None)
    c=f.BinancePublicClient();assert c.get('/fapi/v1/time')=={'serverTime':NOW}
    c.get('/fapi/v1/time');assert len(calls)==3
    for path in ['/fapi/v1/order','/fapi/v1/forceOrders','/fapi/v2/account']:
        with pytest.raises(ValueError):c.get(path)


@pytest.mark.parametrize('status',[401,403,418,451,302])
def test_nontransient_http_no_retry(monkeypatch,status):
    calls=[]
    class Session:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**kw):
            calls.append(1)
            return type('Response',(),{'status_code':status})()
    monkeypatch.setattr(f.requests,'Session',Session);monkeypatch.setattr(f.time,'sleep',lambda _:None)
    with pytest.raises(ValueError):f.BinancePublicClient().get('/fapi/v1/time')
    assert len(calls)==1


def test_native_graph_opt_in_and_default_compatibility(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    model,_,config=graph_setup.__wrapped__(monkeypatch,tmp_path)
    calls=[];r=f.collect_checks(core(),{},client=OKX(),binance=Binance())
    monkeypatch.setattr(f,'prepare_liquidations',lambda *a: calls.append('prepare'))
    monkeypatch.setattr(f,'collect_checks',lambda *a: calls.append('collect') or r)
    graph=TradingAgentsGraph(['market'],config=config);state,_=graph.propagate(SYMBOL,DAY,'crypto')
    assert not calls and 'market_checks_evidence' not in state
    config['okx_market_checks_enabled']=True
    graph=TradingAgentsGraph(['market'],config=config);state,_=graph.propagate(SYMBOL,DAY,'crypto')
    assert calls==['prepare','collect'] and state['market_checks_evidence']==r
    assert len(model.calls)>=9 and not state['execution_enabled']
    assert graph.run_settings()['okx_market_checks_enabled']


def test_funding_actual_intervals_not_fixed_eight_hours():
    rows=[{'symbol':'BTCUSDT','fundingTime':END-i*4*f.HOUR,'fundingRate':'.001'} for i in range(3)]
    assert f.funding(rows,NOW,'BINANCE','BTCUSDT')['observed_intervals_hours']==[4,4]
    rows[0]['fundingTime']=NOW+1
    with pytest.raises(ValueError):f.funding(rows,NOW,'BINANCE','BTCUSDT')


def test_ws_dedup_reconnect_segments_no_full_coverage_claim(monkeypatch):
    import asyncio
    from websockets.asyncio import client
    attempts=[]
    class WS:
        def __init__(self):self.queue=[{'event':'subscribe'},sample(),sample()]
        async def send(self,payload):
            assert json.loads(payload)=={'op':'subscribe','args':[{'channel':'liquidation-orders','instType':'SWAP'}]}
        async def recv(self):
            if not self.queue:raise ConnectionError('mock close')
            return json.dumps(self.queue.pop(0))
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    def connect(url,**kwargs):
        assert url=='wss://ws.okx.com:8443/ws/v5/public'
        attempts.append(1);return WS()
    monkeypatch.setattr(client,'connect',connect)
    r=asyncio.run(f._listen('OKX',INFO,2), loop_factory=lambda: local_loop(monkeypatch))
    assert len(attempts)==2 and r['reconnects']==1 and len(r['segments'])==2
    assert len(r['events'])==1 and r['status']=='PARTIAL'
    assert r['coverage_fraction'] is None and not r['exchange_complete']


def test_ws_rejection_bounded_no_fake_completed_stream(monkeypatch):
    import asyncio
    from websockets.asyncio import client
    class WS:
        async def send(self,payload):pass
        async def recv(self):return json.dumps({'event':'error'})
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    monkeypatch.setattr(client,'connect',lambda *a,**k:WS())
    r=asyncio.run(f._listen('OKX',INFO,2), loop_factory=lambda: local_loop(monkeypatch))
    assert r['status']=='DATA_UNAVAILABLE' and not r['events'] and not r['segments']
    assert r['reconnects']==1


def test_alignment_mismatch_never_compares_funding_or_oi():
    class Mismatch(Binance):
        def get(self,path,params=None):
            rows=super().get(path,params)
            if path.endswith('openInterestHist'):
                for r in rows:r['timestamp']-=f.HOUR
            if path.endswith('fundingRate'):
                for r in rows:r['fundingTime']-=f.HOUR
            return rows
    r=f.collect_checks(core(),{},client=OKX(),binance=Mismatch())
    assert 'aligned_settlement' not in (r['comparison'] or {})
    assert 'aligned_oi' not in (r['comparison'] or {})


def test_cli_flag_preserves_disabled_default_and_quality(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from typer.testing import CliRunner
    from cli import main
    _,_,config=graph_setup.__wrapped__(monkeypatch,tmp_path)
    monkeypatch.setattr(main,'DEFAULT_CONFIG',config)
    r=f.collect_checks(core(),{},client=OKX(),binance=Binance());calls=[]
    monkeypatch.setattr(f,'prepare_liquidations',lambda *a:calls.append('prepare'))
    monkeypatch.setattr(f,'collect_checks',lambda *a:calls.append('collect') or r)
    runner=CliRunner()
    result=runner.invoke(main.app,['okx-research','--date',DAY,'--data-only','--market-checks','--liquidation-seconds','0'])
    assert result.exit_code==0,result.output
    assert calls==['prepare','collect'] and 'execution_enabled=False' in result.output


def test_optional_evidence_cannot_override_core_gate(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    model,bundle,config=graph_setup.__wrapped__(monkeypatch,tmp_path)
    config['okx_market_checks_enabled']=True
    monkeypatch.setattr(f,'prepare_liquidations',lambda *a:{})
    monkeypatch.setattr(f,'collect_checks',lambda *a:pytest.fail('Invalid core must not collect new evidence'))
    monkeypatch.setattr('tradingagents.dataflows.okx_research.build_bundle',lambda *a:{**bundle,'status':'PARTIAL','allow_new_risk':False})
    state,_=TradingAgentsGraph(['market'],config=config).propagate(SYMBOL,DAY,'crypto')
    assert not state['allow_new_risk'] and not state['execution_enabled'] and not model.calls


def local_loop(monkeypatch):
    # Windows asyncio creates a loopback socketpair before mocked WS runs.
    # Restore only that constructor scope; outbound test sockets stay blocked.
    import asyncio, socket, _socket
    with monkeypatch.context() as scoped:
        scoped.setattr(socket.socket, 'connect', _socket.socket.connect)
        return asyncio.new_event_loop()
