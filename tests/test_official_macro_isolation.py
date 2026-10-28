"""Actual guards/native collectors with isolated ledgers and blocked networking."""
import copy
import json
import socket
from datetime import UTC, datetime

import pytest
import requests
from langchain_core.messages import AIMessage

from tradingagents import guarded_research as g
from tradingagents.dataflows import okx_research
from tradingagents.dataflows.vendors import okx, okx_events as events
from tradingagents.safety_budget import Ledger, Policy, SafetyStop, MACRO_SOURCES, optional_macro_source
from tests.test_optional_cpi_isolation import response
from tests.test_guarded_research import FixtureModel
from tests.test_okx_multitimeframe import MultiClient, CFG
from tests.test_okx_research import NOW, DAY, SYMBOL
from tests.test_forward_validation import setup as f1_setup, register
from tests.test_perpetual_plan import research  # fixture dependency

POLICY='official-macro-403-v1'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*a, **k): raise AssertionError('REAL_NETWORK_FORBIDDEN')
    monkeypatch.setattr(socket.socket,'connect',reject)
    monkeypatch.setattr(socket,'create_connection',reject)


@pytest.fixture
def scoped(tmp_path):
    l=Ledger(tmp_path/'FIXTURE');l.initialize(Policy(optional_source_policy=POLICY))
    rid=l.start_research();s=g.Session(l,rid,health=lambda:None)
    return l,rid,s,s.meter_class()(tmp_path/'meter.json',24,240,900)


@pytest.mark.parametrize('name,url',MACRO_SOURCES.items())
def test_each_native_feed_403_is_audited_then_core_can_proceed(scoped,monkeypatch,name,url):
    l,rid,s,m=scoped
    monkeypatch.setattr(requests.Session,'send',lambda client,req,**kw:response(req,403 if req.url==url else 200,
        json.dumps({'code':'0','data':[{'ts':str(NOW)}]}).encode()))
    with s.guards(m):
        with pytest.raises(events.OfficialFeedUnavailable):events.OfficialFeeds().get(url)
        assert okx.OKXPublicClient().get('/api/v5/public/time')[0]['ts']==str(NOW)
    st=l.status();assert not st['paused'] and st['http']==m.value['http_dispatches']==2
    event=next(e for e in l.load()['events'] if e['kind']=='OPTIONAL_SOURCE_UNAVAILABLE')
    assert event['payload']['source_id']==name and event['payload']['source']==url
    assert event['payload']['stage']=='PUBLIC_HTTP_RESPONSE' and event['at_ms']>0
    before=l.path.read_bytes();assert Ledger(l.root).status()['http']==2 and before==l.path.read_bytes()
    with pytest.raises(SafetyStop,match='DUPLICATE_HTTP_RESULT'):
        l.finish_http(event['payload']['wire_id'],status=403)
    assert l.path.read_bytes()==before


@pytest.mark.parametrize('broken',[None,'4h'])
def test_all_macro_403_native_preflight_report_and_existing_analysts(scoped,monkeypatch,broken):
    l,rid,s,m=scoped;sent=[]
    def send(client,req,**kwargs):
        sent.append(req.url)
        if req.url in MACRO_SOURCES.values():
            assert kwargs['allow_redirects'] is False
            return response(req,403)
        if '/support/announcements' in req.url:
            return response(req,200,b'{"code":"0","data":[{"details":[]}]}')
        return response(req)
    monkeypatch.setattr(requests.Session,'send',send)
    monkeypatch.setattr(events,'datetime',type('Clock',(),{'now':staticmethod(lambda tz:datetime.fromtimestamp(NOW/1000,UTC))}))
    monkeypatch.setattr(okx,'utc_ms',lambda:NOW)
    cfg={**CFG,'okx_events_enabled':True,'okx_trade_plan_enabled':False}
    monkeypatch.setattr(okx_research,'get_config',lambda:cfg)
    client=MultiClient(broken=broken);build=okx.build_bundle
    monkeypatch.setattr(okx_research,'build_bundle',lambda sym,day,cfg:build(sym,day,cfg,client))
    with s.guards(m):out=okx_research.preflight({'company_of_interest':SYMBOL,'trade_date':DAY})
    assert len(client.candle_calls)==3 and not l.status()['paused']
    assert l.status()['http']==m.value['http_dispatches']==len(sent)==8
    assert len([e for e in l.load()['events'] if e['kind']=='OPTIONAL_SOURCE_UNAVAILABLE'])==5
    assert out['data_quality']==('RESEARCH_INVALID' if broken else 'VALID')
    ev=out['event_evidence'];assert not ev['events']
    for name in MACRO_SOURCES:
        item=ev['sources'][name]
        assert item['status']=='DATA_UNAVAILABLE' and item['http_status']==403
        assert item['failure_stage']=='PUBLIC_HTTP_RESPONSE' and item['failed_at_ms']==NOW
    assert 'HTTP_403' in events.render_events(ev)
    if broken:
        assert out['workflow_status']=='BLOCKED_DATA' and not out['allow_new_risk']
        return
    # Actual existing pinned analysts, deterministic model only; never external tools.
    from tradingagents.agents.analysts.news_analyst import create_news_analyst
    from tradingagents.agents.analysts.sentiment_analyst import create_sentiment_analyst
    class Model:
        def invoke(self,messages):
            assert 'DATA_UNAVAILABLE' in messages[1].content and 'No external tools' in messages[0].content
            return AIMessage(content='Macro sources unavailable; no numerical values or directional claim.')
        def with_structured_output(self,*a,**k):return self
    assert 'unavailable' in create_news_analyst(Model())(out)['news_report']
    assert 'unavailable' in create_sentiment_analyst(Model())(out)['sentiment_report']


@pytest.mark.parametrize('url',[
    'https://www.sec.gov/news/pressreleases.rss','https://bitcoincore.org/en/rss.xml',
    'https://www.okx.com/api/v5/public/time','https://www.bls.gov/feed/ppi.rss/',
    'https://www.bls.gov/feed/ppi.rss?x=1','https://www.bls.gov.evil.test/feed/ppi.rss'])
def test_other_403_stops(scoped,monkeypatch,url):
    l,_,s,m=scoped
    monkeypatch.setattr(requests.Session,'send',lambda client,req,**kw:response(req,403))
    with s.guards(m):requests.get(url,allow_redirects=False)
    assert l.status()['paused'] and l.status()['http']==1


@pytest.mark.parametrize('method,url',[
    ('POST',MACRO_SOURCES['ppi']),('HEAD',MACRO_SOURCES['ppi']),
    ('GET','http://www.bls.gov/feed/ppi.rss'),('GET','https://user@www.bls.gov/feed/ppi.rss'),
    ('GET','https://www.bls.gov:444/feed/ppi.rss'),('GET',MACRO_SOURCES['ppi']+'#fragment')])
def test_imprecise_routing_never_optional(method,url):
    assert optional_macro_source(method,url,POLICY) is None


@pytest.mark.parametrize('status',[301,302,307,308,401,408,409,429,500,502,503,504])
def test_other_failure_statuses_still_stop(scoped,monkeypatch,status):
    l,_,s,m=scoped;sent=[]
    def send(client,req,**kw):
        assert kw['allow_redirects'] is False
        sent.append(req.url);r=response(req,status);r.headers['Location']='https://evil.test/';return r
    monkeypatch.setattr(requests.Session,'send',send)
    with s.guards(m):
        try:requests.get(MACRO_SOURCES['ppi'])
        except SafetyStop:pass
    assert l.status()['paused'] and len(sent)==l.status()['http']==1


@pytest.mark.parametrize('body_failure',[False,True])
def test_transport_and_stream_failure_never_optional(scoped,monkeypatch,body_failure):
    l,_,s,m=scoped
    def send(client,req,**kw):
        if not body_failure:raise requests.ConnectionError('SENSITIVE_FIXTURE')
        r=response(req)
        def broken(*a,**k):
            yield b'<rss>'
            raise requests.ConnectionError('SENSITIVE_FIXTURE')
        r.iter_content=broken;return r
    monkeypatch.setattr(requests.Session,'send',send)
    monkeypatch.setattr(events.time,'sleep',lambda _:None)
    with s.guards(m):
        # The collector's bounded retry reaches the guard, which refuses dispatch
        # after the first failure paused the ledger. No second HTTP is sent.
        with pytest.raises(SafetyStop,match='PAUSED'):events.OfficialFeeds().get(MACRO_SOURCES['ppi'])
    assert l.status()['paused'] and l.status()['http']==1
    assert 'SENSITIVE' not in l.path.read_text()


@pytest.mark.parametrize('kind',['model_error','unknown_usage'])
def test_model_failures_not_macro(scoped,kind):
    l,_,s,m=scoped
    with s.guards(m):
        with pytest.raises(Exception):
            (FixtureModel(http_status=403) if kind=='model_error' else FixtureModel(total=None)).invoke('fixture')
    assert l.status()['paused']


def test_http_budget_continuous_macro_failures_and_restart(tmp_path):
    l=Ledger(tmp_path/'FIXTURE');l.initialize(Policy(optional_source_policy=POLICY,research_http=5));rid=l.start_research()
    for url in MACRO_SOURCES.values():
        wid=l.begin_http(rid,None,'PUBLIC_GET',source_url=url);l.finish_http(wid,status=403)
    before=l.path.read_bytes();l=Ledger(l.root);assert not l.status()['paused'] and l.status()['http']==5
    with pytest.raises(SafetyStop,match='HTTP_BUDGET'):
        l.begin_http(rid,None,'PUBLIC_GET',source_url=MACRO_SOURCES['ppi'])
    assert l.path.read_bytes()==before
    l.finish_research(rid,None,success=False)
    assert l.status()['paused'] and l.status()['http']==5
    with pytest.raises(SafetyStop):l.resume()


def test_first_global_failure_is_distinct_from_optional_failures(scoped,monkeypatch):
    l,rid,s,m=scoped
    monkeypatch.setattr(requests.Session,'send',lambda client,req,**kw:response(req,403))
    with s.guards(m):
        for url in (MACRO_SOURCES['cpi'],MACRO_SOURCES['ppi'],'https://www.sec.gov/news/pressreleases.rss'):
            requests.get(url,allow_redirects=False)
    assert g.worker_diagnostic(l,rid)
    value=json.loads(next((l.root/'diagnostics').glob('*.json')).read_text())
    first=value['first_global_pause'];assert first['reason']=='TRANSPORT_OR_SERVICE_FAILURE'
    assert first['http_failure']['stage']=='HTTP_RESPONSE' and first['http_failure']['http_status']==403
    assert l.status()['http']==3


def test_definition_changes_and_legacy_scope(monkeypatch):
    assert Policy().optional_source_policy=='strict-v1'
    assert optional_macro_source('GET',MACRO_SOURCES['ppi'],'bls-cpi-403-v1') is None
    monkeypatch.setitem(events.FEEDS,'ppi',('https://evil.test/','MACRO',None))
    with pytest.raises(SafetyStop,match='OPTIONAL_SOURCE_DEFINITION_CHANGED'):
        optional_macro_source('GET',MACRO_SOURCES['ppi'],POLICY)


def test_failed_placeholder_wait_cannot_enter_f1_pool(f1_setup):
    registry,directory,state,token,clock,save=f1_setup
    state.update(workflow_status='BLOCKED_DATA',llm_status='NOT_STARTED',data_quality='RESEARCH_INVALID')
    state['research_result'].update(proposed_action='WAIT',model_proposed_action='WAIT',plan_valid=False,
        data_quality='RESEARCH_INVALID',plan_status='RESEARCH_INVALID',signal_state='WAIT',allow_new_risk=False,execution_enabled=False)
    save()
    r=register(f1_setup)
    assert not r.plan_valid and not r.simulation_pool_eligible
    assert r.workflow_status=='BLOCKED_DATA' and registry.tracking_status(r.registration_id)=='ARCHIVED_ONLY'


def test_model_partial_response_still_pauses(scoped):
    l,_,s,m=scoped
    with s.guards(m):
        with pytest.raises(Exception):FixtureModel(partial=True).invoke('fixture')
    assert l.status()['paused'] and l.status()['unknown_usage']


def test_httpx_redirect_does_not_send_to_target(scoped):
    import httpx
    l,_,s,m=scoped;sent=[]
    def handler(req):
        sent.append(str(req.url));return httpx.Response(302,headers={'location':'https://evil.test/'})
    with s.guards(m):
        with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
            with pytest.raises(SafetyStop,match='PROTECTED_REDIRECT_DENIED'):client.get(MACRO_SOURCES['ppi'])
    assert sent==[MACRO_SOURCES['ppi']] and l.status()['http']==1 and l.status()['paused']


def test_caller_cannot_tag_core_or_model_as_macro(scoped):
    l,rid,_,_=scoped
    for kind,url,call in [('PUBLIC_GET','https://www.okx.com/api/v5/public/time',None),
            ('AUTH',MACRO_SOURCES['ppi'],None),('MODEL',MACRO_SOURCES['ppi'],'arbitrary')]:
        with pytest.raises(SafetyStop,match='OPTIONAL_SOURCE_SCOPE_DENIED'):
            l.begin_http(rid,call,kind,source_url=url)
    assert l.status()['http']==0


def test_auth_403_is_never_macro(scoped,monkeypatch):
    from tradingagents.llm_clients import chatgpt_auth
    l,_,s,m=scoped
    monkeypatch.setattr(requests.Session,'send',lambda client,req,**kw:response(req,403))
    marker=g.RENEWAL.set(True)
    try:
        with s.guards(m):
            with pytest.raises(SafetyStop,match='AUTH_RESPONSE_REJECTED'):
                requests.post(chatgpt_auth.TOKEN_URL)
    finally:g.RENEWAL.reset(marker)
    assert l.status()['paused'] and l.status()['http']==1 and l.status()['tokens']==0
    assert not any(e['kind']=='OPTIONAL_SOURCE_UNAVAILABLE' for e in l.load()['events'])


def test_token_headroom_stops_new_call_under_macro_policy(tmp_path):
    l=Ledger(tmp_path/'FIXTURE');l.initialize(Policy(optional_source_policy=POLICY,
        research_tokens=150,daily_tokens=150,campaign_tokens=150,headroom_tokens=100))
    rid=l.start_research();s=g.Session(l,rid,health=lambda:None)
    m=s.meter_class()(tmp_path/'meter',24,240,900)
    with s.guards(m):
        FixtureModel().invoke('fixture')
        with pytest.raises(SafetyStop,match='TOKEN_HEADROOM'):FixtureModel().invoke('fixture')
    assert l.status()['tokens']==100 and l.status()['llm_calls']==l.status()['http']==1
    l.finish_research(rid,100,success=False)
    assert l.status()['paused']


def test_macro_worker_diagnostic_is_bounded_and_contains_no_private_data(scoped):
    l,rid,_,_=scoped
    folder=l.root/'batch/fixture/research/reports/fixture';folder.mkdir(parents=True)
    report=folder/'research_result.json'
    report.write_text(json.dumps({'PRIVATE_PROMPT':'PRIVATE_RESPONSE','evidence':{'events':{'sources':{
        name:{'source':url,'http_status':403,'failed_at_ms':NOW,'raw_body':'PRIVATE_TOKEN'}
        for name,url in MACRO_SOURCES.items()}}}}))
    assert g.worker_diagnostic(l,rid,registration={'report_json':str(report)})
    text=next((l.root/'diagnostics').glob('*.json')).read_text()
    assert len(json.loads(text)['source_failures'])==5 and 'PRIVATE_' not in text
