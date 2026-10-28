"""Explicitly isolated HTTP/model fixtures only. No live research or external I/O."""
import copy
import json
import socket
from datetime import UTC, datetime

import pytest
import requests

from tradingagents import guarded_research as g
from tradingagents.dataflows import okx_research
from tradingagents.dataflows.vendors import okx, okx_events as events, okx_multitimeframe as mtf
from tradingagents.safety_budget import Ledger, Policy, SafetyStop, optional_cpi_request
from tests.test_guarded_research import FixtureModel
from tests.test_okx_multitimeframe import MultiClient, CFG
from tests.test_okx_research import NOW, DAY, SYMBOL

CPI = 'https://www.bls.gov/feed/cpi.rss'


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*a, **k): raise AssertionError('REAL_NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture
def enabled(tmp_path):
    ledger = Ledger(tmp_path / 'FIXTURE')
    ledger.initialize(Policy(optional_source_policy='bls-cpi-403-v1'))
    rid = ledger.start_research()
    session = g.Session(ledger, rid, health=lambda: None)
    meter = session.meter_class()(tmp_path / 'meter.json', 24, 240, 900)
    return ledger, rid, session, meter


def response(request, status=200, body=b'<rss><channel></channel></rss>'):
    r = requests.Response(); r.status_code = status; r.request = request; r.url = request.url
    r._content = body; r._content_consumed = True
    return r


def test_policy_default_strict_and_explicit_schema():
    assert Policy().optional_source_policy == 'strict-v1'
    with pytest.raises(ValueError): Policy(optional_source_policy='all-public-403')
    from pathlib import Path
    assert json.loads(Path('docs/d4b_safety_budget.schema.json').read_text()) == Policy.model_json_schema()


@pytest.mark.parametrize('url', [
    'http://www.bls.gov/feed/cpi.rss', 'https://bls.gov/feed/cpi.rss',
    'https://www.bls.gov.evil.example/feed/cpi.rss', 'https://www.bls.gov:444/feed/cpi.rss',
    'https://user@www.bls.gov/feed/cpi.rss', CPI+'/', CPI+'?query=1', CPI+'#fragment',
    'https://www.bls.gov/feed/ppi.rss', 'https://www.bls.gov/feed/%63pi.rss',
    'https://www.bls.gov/other/../feed/cpi.rss', 'https://www.okx.com/api/v5/public/time',
])
def test_imprecise_sources_never_optional(url):
    assert not optional_cpi_request('GET', url)


@pytest.mark.parametrize('method', ['POST', 'HEAD', 'PUT', 'DELETE', 'get'])
def test_wrong_methods_never_optional(method):
    assert not optional_cpi_request(method, CPI)


def test_native_definition_cannot_silently_expand(monkeypatch):
    monkeypatch.setitem(events.FEEDS, 'cpi', ('https://evil.example/cpi', 'MACRO', None))
    with pytest.raises(SafetyStop, match='OPTIONAL_SOURCE_DEFINITION_CHANGED'): optional_cpi_request('GET', CPI)


def test_explicit_cpi_403_then_real_native_public_get_can_proceed(enabled, monkeypatch):
    l, rid, s, meter = enabled; sent = []
    def fake(client, request, **kwargs):
        sent.append(request.url)
        return response(request, 403 if request.url == CPI else 200,
                        json.dumps({'code':'0', 'data':[{'ts':str(NOW)}]}).encode())
    monkeypatch.setattr(requests.Session, 'send', fake)
    with s.guards(meter):
        with pytest.raises(events.CPIUnavailable): events.OfficialFeeds().get(CPI)
        assert okx.OKXPublicClient().get('/api/v5/public/time')[0]['ts'] == str(NOW)
    state = l.status()
    assert not state['paused'] and state['http'] == meter.value['http_dispatches'] == 2
    assert state['llm_calls'] == state['tokens'] == 0
    e = [e for e in l.load()['events'] if e['kind'] == 'OPTIONAL_SOURCE_UNAVAILABLE']
    assert len(e) == 1 and e[0]['payload']['source'] == CPI and e[0]['payload']['available'] is False
    # Re-read/restart has no accounting side effects, duplicate completion remains forbidden.
    before = l.path.read_bytes(); restarted = Ledger(l.root)
    assert restarted.status()['http'] == 2 and restarted.path.read_bytes() == before
    with pytest.raises(SafetyStop, match='DUPLICATE_HTTP_RESULT'):
        restarted.finish_http(e[0]['payload']['wire_id'], status=403)
    assert l.path.read_bytes() == before


def test_strict_mode_unchanged(tmp_path, monkeypatch):
    l = Ledger(tmp_path); l.initialize(Policy()); rid = l.start_research()
    s = g.Session(l, rid, health=lambda: None); m = s.meter_class()(tmp_path/'meter',24,240,900)
    monkeypatch.setattr(requests.Session, 'send', lambda client, req, **kw: response(req,403))
    with s.guards(m):
        with pytest.raises(events.CPIUnavailable): events.OfficialFeeds().get(CPI)
        with pytest.raises(SafetyStop): okx.OKXPublicClient().get('/api/v5/public/time')
    assert l.status()['paused'] and l.status()['http'] == 1


@pytest.mark.parametrize('url', ['https://www.okx.com/api/v5/public/time', 'https://www.bls.gov/feed/ppi.rss'])
def test_nonwhitelist_403_still_pauses(enabled, monkeypatch, url):
    l, _, s, m = enabled
    monkeypatch.setattr(requests.Session, 'send', lambda client, req, **kw: response(req,403))
    with s.guards(m):
        with requests.Session() as client: client.get(url, allow_redirects=False)
    assert l.status()['paused'] and l.status()['http'] == 1


@pytest.mark.parametrize('status', [401,408,409,429,500,502,503,504])
def test_other_cpi_errors_still_pause(enabled, monkeypatch, status):
    l, _, s, m = enabled
    monkeypatch.setattr(requests.Session, 'send', lambda client, req, **kw: response(req,status))
    with s.guards(m):
        with requests.Session() as client: client.get(CPI, allow_redirects=False)
    assert l.status()['paused'] and l.status()['http'] == 1


@pytest.mark.parametrize('error', [requests.ConnectionError('PRIVATE_NETWORK_DETAIL'), requests.Timeout('PRIVATE_TIMEOUT_DETAIL')])
def test_transport_failure_not_optional(enabled, monkeypatch, error):
    l, _, s, m = enabled
    def fail(*a, **k): raise error
    monkeypatch.setattr(requests.Session, 'send', fail)
    with s.guards(m):
        with pytest.raises(type(error)):
            with requests.Session() as client: client.get(CPI)
    assert l.status()['paused'] and l.status()['http'] == 1
    assert 'PRIVATE_' not in l.path.read_text()


def test_streamed_cpi_body_failure_after_200_still_pauses(enabled, monkeypatch):
    l,_,s,m=enabled
    def fake(client,req,**kw):
        r=response(req)
        def broken(*a,**k):
            yield b'<rss>'
            raise requests.ConnectionError('PRIVATE_STREAM_DETAIL')
        r.iter_content=broken
        return r
    monkeypatch.setattr(requests.Session,'send',fake)
    with s.guards(m):
        with pytest.raises(SafetyStop):events.OfficialFeeds().get(CPI)
    assert l.status()['paused'] and l.status()['http']==1
    assert not any(e['kind']=='OPTIONAL_SOURCE_UNAVAILABLE' for e in l.load()['events'])
    assert 'PRIVATE_STREAM_DETAIL' not in l.path.read_text()


def test_cpi_redirect_never_forwarded_or_degraded(enabled, monkeypatch):
    l, _, s, m = enabled; seen = []
    def fake(client, req, **kw):
        seen.append((req.url,kw.get('allow_redirects')))
        r=response(req,302);r.headers['Location']='https://evil.example/cpi';return r
    monkeypatch.setattr(requests.Session,'send',fake)
    with s.guards(m):
        with pytest.raises(SafetyStop,match='PROTECTED_REDIRECT_DENIED'):
            with requests.Session() as client: client.get(CPI,allow_redirects=True)
    assert seen == [(CPI,False)] and l.status()['paused']


def test_httpx_optional_redirect_also_stops_before_forwarding(enabled):
    import httpx
    l,_,s,m=enabled;seen=[]
    def handler(req):
        seen.append(str(req.url));return httpx.Response(302,headers={'Location':'https://evil.example/cpi'})
    with s.guards(m), httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
        with pytest.raises(SafetyStop,match='PROTECTED_REDIRECT_DENIED'):client.get(CPI)
    assert seen==[CPI] and l.status()['http']==1 and l.status()['paused']


def test_httpx_body_disconnect_after_200_is_not_downgraded(enabled):
    import httpx
    l,_,s,m=enabled
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'<rss>'
            raise httpx.ReadError('PRIVATE_STREAM_DETAIL')
    with s.guards(m), httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Broken()))) as client:
        with pytest.raises(httpx.ReadError):client.get(CPI)
    assert l.status()['paused'] and l.status()['http']==1 and 'PRIVATE_STREAM_DETAIL' not in l.path.read_text()


def test_required_request_cannot_be_relabelled(enabled):
    l,rid,_,_=enabled
    with pytest.raises(SafetyStop,match='OPTIONAL_SOURCE_SCOPE_DENIED'):
        l.begin_http(rid,None,'PUBLIC_GET',source_url='https://www.okx.com/api/v5/public/time')
    with pytest.raises(SafetyStop): l.begin_http(rid,None,'AUTH',source_url=CPI)
    cid=l.begin_call(rid)
    with pytest.raises(SafetyStop): l.begin_http(rid,cid,'PUBLIC_GET',source_url=CPI)
    assert l.status()['http'] == 0


def test_model_error_and_unknown_usage_not_optional(enabled):
    l,_,s,m=enabled
    with s.guards(m):
        with pytest.raises(Exception): FixtureModel(http_status=403).invoke('fixture')
    assert l.status()['paused']


def test_unknown_token_usage_keeps_stop(enabled):
    l,_,s,m=enabled
    with s.guards(m):
        with pytest.raises(SafetyStop): FixtureModel(total=None).invoke('fixture')
    assert l.status()['paused'] and l.status()['unknown_usage']


def test_http_budget_includes_degraded_requests(tmp_path):
    l=Ledger(tmp_path);l.initialize(Policy(optional_source_policy='bls-cpi-403-v1',research_http=5));rid=l.start_research()
    for _ in range(5):
        wid=l.begin_http(rid,None,'PUBLIC_GET',source_url=CPI);l.finish_http(wid,status=403)
    with pytest.raises(SafetyStop,match='HTTP_BUDGET'): l.begin_http(rid,None,'PUBLIC_GET',source_url=CPI)
    assert l.status()['http'] == 5
    # Original worker failure reconciliation supplies the global pause on a budget exception.
    l.finish_research(rid,None,success=False)
    assert l.status()['paused']


@pytest.mark.parametrize('broken', [None,'4h'])
def test_optional_metadata_and_three_timeframes_in_preflight(enabled,monkeypatch,broken):
    l,_,s,m=enabled
    cfg={**CFG,'okx_events_enabled':True,'okx_trade_plan_enabled':False}
    monkeypatch.setattr(okx,'utc_ms',lambda:NOW)
    monkeypatch.setattr(requests.Session,'send',lambda client,req,**kw:response(req,403 if req.url==CPI else 200))
    client=MultiClient(broken=broken)
    build=okx.build_bundle
    monkeypatch.setattr(okx_research,'get_config',lambda:cfg)
    monkeypatch.setattr(okx_research,'build_bundle',lambda sym,day,cfg:build(sym,day,cfg,client))
    monkeypatch.setattr(events,'datetime',type('Clock',(),{'now':staticmethod(lambda tz:datetime.fromtimestamp(NOW/1000,UTC))}))
    with s.guards(m): out=okx_research.preflight({'company_of_interest':SYMBOL,'trade_date':DAY})
    assert len(client.candle_calls)==3
    assert out['data_quality']==('RESEARCH_INVALID' if broken else 'VALID')
    if broken: assert not out['allow_new_risk'] and out['workflow_status']=='BLOCKED_DATA'
    cpi=out['event_evidence']['sources']['cpi']
    assert cpi['http_status']==403 and cpi['failure_category']=='HTTP_APPLICATION_ERROR' and cpi['failed_at_ms']==NOW
    assert cpi['status']=='DATA_UNAVAILABLE' and not out['event_evidence']['events']
    assert not l.status()['paused'] and l.status()['http']==m.value['http_dispatches']


def test_earliest_pause_is_preserved_and_valid_incomplete_still_rejected():
    class PausedClient:
        def get(self,*args): raise SafetyStop('PAUSED')
    b=mtf.build_multitimeframe(SYMBOL,DAY,CFG,PausedClient())
    original=copy.deepcopy(b)
    for _ in range(3): b=mtf.check_multitimeframe(b,CFG,SYMBOL,DAY)
    assert b==original and b['error_context'][0]=={'stage':'SERVER_TIME','exception_type':'SafetyStop','error_code':'PAUSED'}
    b=mtf.check_multitimeframe({'multi_timeframe':True,'status':'VALID','timeframes':{}},CFG)
    assert b['status']=='RESEARCH_INVALID' and not b['allow_new_risk']
    assert b['error_context'][0]['error_code']=='REQUIRED_PERIODS_MISSING'
    malformed=mtf.check_multitimeframe({'multi_timeframe':True,'status':'VALID','timeframes':{},'error_context':None},CFG)
    assert malformed['status']=='RESEARCH_INVALID' and not malformed['allow_new_risk']


def test_no_raw_exception_text_or_diagnostic_path_escape(enabled):
    l,rid,_,_=enabled
    assert g.worker_diagnostic(l,rid,exception=ValueError('PRIVATE_TOKEN PRIVATE_PROMPT PRIVATE_RESPONSE'))
    text=next((l.root/'diagnostics').glob('*.json')).read_text()
    assert 'PRIVATE_' not in text and 'DETAILS_WITHHELD' in text
    assert not g.worker_diagnostic(l,'../escape',exception=ValueError('private'))


def test_worker_result_diagnostic_selects_only_safe_metadata(enabled):
    l,rid,_,_=enabled;folder=l.root/'batch/fixture/research/reports/fixture';folder.mkdir(parents=True)
    report=folder/'research_result.json'
    report.write_text(json.dumps({'PRIVATE_PROMPT':'PRIVATE_RESPONSE','evidence':{'events':{'sources':{'cpi':{
        'source':CPI,'http_status':403,'failed_at_ms':NOW,'raw_body':'PRIVATE_TOKEN'}}}}}))
    (folder/'multitimeframe_snapshot.json').write_text(json.dumps({'status':'RESEARCH_INVALID','records':['PRIVATE_MARKET_ROWS'],
        'error_context':[{'stage':'SERVER_TIME','exception_type':'SafetyStop','error_code':'PAUSED','message':'PRIVATE_MESSAGE'}]}))
    assert g.worker_diagnostic(l,rid,registration={'report_json':str(report)})
    file=next((l.root/'diagnostics').glob('*.json'));before=file.read_bytes();value=json.loads(before)
    assert value['market_errors'][0]['error_code']=='PAUSED'
    assert value['optional_source_failure']['source']==CPI and 'PRIVATE_' not in before.decode()
    assert g.worker_diagnostic(l,rid,registration={'report_json':str(report)}) and file.read_bytes()==before


def test_old_pause_never_auto_clears(enabled):
    l,rid,_,_=enabled
    w=l.begin_http(rid,None,'PUBLIC_GET');l.finish_http(w,status=403);l.finish_research(rid,0,success=False)
    before=l.path.read_bytes()
    with pytest.raises(SafetyStop,match='UNSAFE_RESUME'):l.resume()
    assert l.path.read_bytes()==before


def test_old_implementation_is_not_silently_migrated(enabled,monkeypatch):
    from tradingagents import safety_budget as budget
    l,_,_,_=enabled;before=l.path.read_bytes()
    monkeypatch.setattr(budget,'implementation_hash',lambda:'old-implementation')
    with pytest.raises(SafetyStop,match='IMPLEMENTATION_CHANGED'):l.load()
    assert l.path.read_bytes()==before
