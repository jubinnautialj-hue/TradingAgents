import copy
import json
from datetime import UTC,datetime

import pytest
from langchain_core.messages import AIMessage

from tradingagents.dataflows.vendors import okx_events as f
from tests.test_okx_research import INFO,NOW,DAY,SYMBOL


@pytest.fixture(autouse=True)
def clock(monkeypatch):monkeypatch.setattr(f.okx,'utc_ms',lambda:NOW)


def row(title='Policy release',published=NOW-1000,url='https://www.federalreserve.gov/release/a'):
    return f.event(title,url,published,NOW-100,'fed_policy','MACRO','BTC')


def core():return {'status':'VALID','cutoff_ms':NOW,'snapshot_id':'core','historical':False}
def prepared(rows=None):return {'events':rows if rows is not None else [row()],'sources':{'fed_policy':{'status':'VALID'}},'lookback_days':14,'execution_enabled':False}


def rss(title='Release',date='Mon, 06 Jan 2025 12:00:00 GMT',link='https://www.federalreserve.gov/release/a'):
    return f'<rss><channel><item><title>{title}</title><pubDate>{date}</pubDate><link>{link}</link></item></channel></rss>'.encode()


def test_publication_timezone_and_value_clocks_separate():
    assert f.publication_ms('2025-01-06T12:00:00Z')==f.publication_ms('Mon, 06 Jan 2025 07:00:00 -0500')
    for value in ['2025-01-06','2025-01-06T12:00:00']:
        with pytest.raises(ValueError):f.publication_ms(value)
    r=row();assert r['scheduled_at_ms'] is None and r['occurred_at_ms'] is None
    assert r['actual_value'] is None and r['consensus_value'] is None


def test_feed_parser_official_url_rejects_bad_timestamp_or_host():
    rows,bad=f.parse_feed(rss(),'fed_policy','BTC',NOW)
    assert len(rows)==1 and not bad
    for body in [rss(date='2025-01-06'),rss(link='https://attacker.example/a')]:
        rows,bad=f.parse_feed(body,'fed_policy','BTC',NOW)
        assert not rows and bad==1
    with pytest.raises(ValueError):f.parse_feed(b'<!DOCTYPE rss><rss/>','fed_policy','BTC',NOW)


@pytest.mark.parametrize('base,title,expected',[
    ('NEAR','Announcement in the near future',False),('NEAR','NEAR upgrade',True),
    ('BTC','BTCUSDT contract update',True),('ETH','Ethereum security fix',True),('BTC','SOL unlock',False)])
def test_asset_matching(base,title,expected):assert f.relevant(title,base,'PROJECT') is expected


def test_asset_specific_official_feed_does_not_require_token_in_title():
    rows,_=f.parse_feed(rss(title='Network upgrade',link='https://blog.ethereum.org/en/upgrade'),'eth_blog','ETH',NOW)
    assert len(rows)==1 and rows[0]['related_instruments']==['ETH-USDT-SWAP']


def test_unpublished_unobserved_and_old_items_not_available():
    events=[row(),row(published=NOW+1,url='https://www.federalreserve.gov/future'),
        row(published=NOW-15*f.DAY,url='https://www.federalreserve.gov/old')]
    unseen=row(url='https://www.federalreserve.gov/unseen');unseen['first_seen_ms']=NOW+1;events.append(unseen)
    raw=f.bind_events(prepared(events),core())
    assert len(raw['events'])==1 and raw['rejected_time_or_window_count']==3


def test_deduplicate_same_url_and_headline_not_independent_votes():
    rows=[row(),row(url='https://www.federalreserve.gov/b'),row(title='Policy release!!!',url='https://www.federalreserve.gov/c')]
    raw=f.bind_events(prepared(rows),core())
    assert len(raw['events'])==1 and raw['duplicate_count']==2


def test_event_budget_and_unavailable_core():
    rows=[row(title=f'Event {i}',url=f'https://www.federalreserve.gov/{i}') for i in range(20)]
    raw=f.bind_events(prepared(rows),core());assert len(raw['events'])==12 and raw['omitted_budget_count']==8
    invalid=f.bind_events(prepared(),{'status':'DATA_UNAVAILABLE'})
    assert invalid['status']=='DATA_UNAVAILABLE' and not invalid['events']


def test_historical_no_live_fetch_and_no_substitution():
    class NoNetwork:
        def get(self,*a,**k):pytest.fail('Historical feeds must not be fetched')
    raw=f.prepare_events(SYMBOL,'2001-01-01',{},feeds=NoNetwork(),client=NoNetwork())
    assert raw['status']=='DATA_UNAVAILABLE'
    raw=f.bind_events(prepared(),{**core(),'historical':True})
    assert not raw['events']


def test_expiry_and_fingerprint():
    raw=f.bind_events(prepared(),core());before=copy.deepcopy(raw)
    assert f.evidence_view(raw,NOW+900001)['status']=='STALE_DATA'
    assert not f.evidence_view(raw,NOW+900001)['events'] and raw==before
    raw['events'][0]['title']='Changed'
    assert f.evidence_view(raw)['status']=='RESEARCH_INVALID'


def test_prepare_current_official_feed_and_ptime(monkeypatch):
    monkeypatch.setattr(f,'datetime',type('Clock',(),{'now':staticmethod(lambda tz:datetime(2025,1,6,tzinfo=UTC))}))
    class Feeds:
        def get(self,url):return rss()
    class Client:
        def get(self,path,params):return [{'details':[{'title':'BTCUSDT maintenance','url':'https://www.okx.com/help/a',
            'pTime':str(NOW+1),'businessPTime':str(NOW-1000)}]}]
    raw=f.prepare_events(SYMBOL,'2025-01-06',{},feeds=Feeds(),client=Client())
    bound=f.bind_events(raw,core())
    assert not any(r['source']=='okx_announcements' for r in bound['events'])


def test_news_social_branches_never_call_legacy_external_sources(monkeypatch):
    from tradingagents.agents.analysts.news_analyst import create_news_analyst
    from tradingagents.agents.analysts.sentiment_analyst import create_sentiment_analyst
    class Model:
        def invoke(self,messages):
            assert 'No external tools' in messages[0].content
            return AIMessage(content='Pinned publication only; expectations unavailable')
        def with_structured_output(self,*a,**k):return self
    def refuse(*a,**k):pytest.fail('Legacy external source called')
    monkeypatch.setattr('tradingagents.agents.analysts.sentiment_analyst.fetch_reddit_posts',refuse)
    monkeypatch.setattr('tradingagents.agents.analysts.sentiment_analyst.fetch_stocktwits_messages',refuse)
    s={'event_research_mode':True,'event_evidence':f.bind_events(prepared(),core())}
    assert create_news_analyst(Model())(s)['news_report']
    assert create_sentiment_analyst(Model())(s)['sentiment_report']


def test_html_json_native_graph_and_off_compatibility(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.reporting import write_report_tree
    model,_,config=graph_setup.__wrapped__(monkeypatch,tmp_path)
    calls=[];monkeypatch.setattr(f,'prepare_events',lambda *a:calls.append(1) or prepared())
    g=TradingAgentsGraph(['market'],config=config);state,_=g.propagate(SYMBOL,DAY,'crypto')
    assert not calls and 'event_evidence' not in state
    config['okx_events_enabled']=True
    # Keep native graph/roles but deterministic text for these two existing analysts.
    from tradingagents.agents.analysts import event_context
    monkeypatch.setattr(event_context,'pinned_event_report',lambda llm,s,role:{'messages':[AIMessage(content='Pinned event')],
        'news_report' if role=='News Analyst' else 'sentiment_report':'Pinned event'})
    g=TradingAgentsGraph(['market','news','social'],config=config);state,_=g.propagate(SYMBOL,DAY,'crypto')
    assert len(calls)==1 and state['news_report']=='Pinned event' and state['sentiment_report']=='Pinned event'
    assert not state['execution_enabled'] and len(model.calls)>=9
    write_report_tree(state,SYMBOL,tmp_path/'report',settings=g.run_settings())
    assert (tmp_path/'report/events_snapshot.json').exists()
    assert 'Official publications / catalysts' in (tmp_path/'report/complete_report.html').read_text(encoding='utf-8')


def test_event_graph_has_no_news_tool_subnode(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    _,_,config=graph_setup.__wrapped__(monkeypatch,tmp_path);config['okx_events_enabled']=True
    graph=TradingAgentsGraph(['market','news','social'],config=config)
    nodes=graph.graph.get_graph(xray=True).nodes
    assert not any('News Analyst:tools' in str(k) for k in nodes)


@pytest.mark.parametrize('status,attempts',[(403,1),(429,2),(503,2)])
def test_feed_failures_bounded_no_bypass(monkeypatch,status,attempts):
    calls=[]
    class Response:
        status_code=status
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Session:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,url,**kwargs):
            assert kwargs['allow_redirects'] is False and kwargs['stream'] is True
            calls.append(url);return Response()
    monkeypatch.setattr(f.requests,'Session',Session);monkeypatch.setattr(f.time,'sleep',lambda _:None)
    with pytest.raises(ValueError):f.OfficialFeeds().get(f.FEEDS['cpi'][0])
    assert len(calls)==attempts
    with pytest.raises(ValueError):f.OfficialFeeds().get('https://attacker.example/feed')


def test_cli_events_same_quality_and_safe_report_metadata(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from cli import main
    from typer.testing import CliRunner
    _,_,config=graph_setup.__wrapped__(monkeypatch,tmp_path)
    monkeypatch.setattr(main,'DEFAULT_CONFIG',config)
    monkeypatch.setattr(f,'prepare_events',lambda *a:prepared())
    result=CliRunner().invoke(main.app,['okx-research','--date',DAY,'--events','--data-only'])
    assert result.exit_code==0,result.output
    assert 'execution_enabled=False' in result.output
    report=next((tmp_path/'results/reports').glob('*/events_snapshot.json'))
    metadata=json.loads(report.read_text())['research_metadata']
    assert metadata['prompt_version']=='okx-pinned-publication-v1'
    assert 'backend_url' not in metadata['settings'] and 'api_key' not in metadata['settings']


def test_parallel_news_failure_uses_existing_failure_boundary(monkeypatch,tmp_path):
    from tests.test_okx_graph import graph_setup
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.agents.analysts import event_context
    import openai
    import httpx
    _,_,config=graph_setup.__wrapped__(monkeypatch,tmp_path);config['okx_events_enabled']=True
    monkeypatch.setattr(f,'prepare_events',lambda *a:prepared())
    def fail(*a):raise openai.APIConnectionError(request=httpx.Request('POST','https://example.invalid'))
    monkeypatch.setattr(event_context,'pinned_event_report',fail)
    state,_=TradingAgentsGraph(['market','news','social'],config=config).propagate(SYMBOL,DAY,'crypto')
    assert state['llm_status']=='LLM_UNAVAILABLE' and not state['allow_new_risk'] and not state['execution_enabled']
