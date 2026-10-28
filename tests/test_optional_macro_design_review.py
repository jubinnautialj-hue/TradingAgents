"""Design-only reference contract. NOT a production policy or launch entry.

No networking, SDK calls, production ledger changes or guard monkey-patching.
Native safety/integration behavior is covered by the unchanged regression suite.
"""
from urllib.parse import urlsplit
import pytest

from tradingagents.dataflows.vendors import okx_events as feeds
from tradingagents import guarded_research as guarded
from tradingagents.safety_budget import Policy, optional_cpi_request
from tests.test_okx_events import core, prepared


# Deliberately independent reviewed literals: a changed native URL must fail review.
REVIEWED = {
    'fed_policy': 'https://www.federalreserve.gov/feeds/press_monetary.xml',
    'fed_speeches': 'https://www.federalreserve.gov/feeds/speeches.xml',
    'cpi': 'https://www.bls.gov/feed/cpi.rss',
    'ppi': 'https://www.bls.gov/feed/ppi.rss',
    'employment': 'https://www.bls.gov/feed/empsit.rss',
}
PROPOSED = 'official-macro-403-v1'


def reference_source(method, url):
    """Test-only proposal, not imported by the application."""
    p = urlsplit(url)
    if method != 'GET' or any(c in url for c in '\\\r\n\t'):
        return None
    try:
        if (p.scheme != 'https' or p.port not in (None, 443) or p.username is not None
                or p.password is not None or p.query or p.fragment):
            return None
    except ValueError:
        return None
    return next((name for name, exact in REVIEWED.items()
                 if (p.hostname, p.path) == (urlsplit(exact).hostname, urlsplit(exact).path)), None)


def reference_degrade(method, url, status, *, policy='strict-v1', call_id=None,
                      failure=None, endpoint='PUBLIC_GET'):
    return (policy == PROPOSED and call_id is None and endpoint == 'PUBLIC_GET'
            and failure is None and status == 403 and reference_source(method, url) is not None)


@pytest.mark.parametrize('name,url', REVIEWED.items())
def test_reviewed_definitions_and_proposed_explicit_403(name, url):
    assert feeds.FEEDS[name] == (url, 'MACRO', None)
    assert reference_source('GET', url) == name
    assert not reference_degrade('GET', url, 403)
    assert reference_degrade('GET', url, 403, policy=PROPOSED)


@pytest.mark.parametrize('url', [
    'http://www.bls.gov/feed/ppi.rss', 'https://bls.gov/feed/ppi.rss',
    'https://www.bls.gov.evil.example/feed/ppi.rss',
    'https://www.bls.gov:444/feed/ppi.rss', 'https://user@www.bls.gov/feed/ppi.rss',
    'https://www.bls.gov/feed/ppi.rss/', 'https://www.bls.gov/feed/ppi.rss?x=1',
    'https://www.bls.gov/feed/ppi.rss#x', 'https://www.bls.gov/feed/%70pi.rss',
    'https://www.bls.gov/other/../feed/ppi.rss',
    *[v[0] for k,v in feeds.FEEDS.items() if k not in REVIEWED],
    'https://www.okx.com/api/v5/public/time',
    'https://auth.openai.com/api/accounts/oauth/token',
])
def test_proposed_does_not_expand_other_sources(url):
    assert not reference_degrade('GET', url, 403, policy=PROPOSED)


@pytest.mark.parametrize('method', ['POST', 'HEAD', 'PUT', 'DELETE', 'get'])
def test_no_other_method(method):
    assert not reference_degrade(method, REVIEWED['ppi'], 403, policy=PROPOSED)


@pytest.mark.parametrize('status', [200, 301, 302, 307, 308, 401, 408, 409, 429, 500, 502, 503, 504, None])
def test_only_403_is_optional_application_failure(status):
    assert not reference_degrade('GET', REVIEWED['ppi'], status, policy=PROPOSED)


@pytest.mark.parametrize('failure', ['NETWORK', 'BODY_DISCONNECT', 'BUDGET', 'UNKNOWN_USAGE', 'AUTH'])
def test_no_safety_failure_bypass(failure):
    assert not reference_degrade('GET', REVIEWED['ppi'], 403, policy=PROPOSED, failure=failure)


def test_model_context_and_auth_never_optional():
    assert not reference_degrade('GET', REVIEWED['ppi'], 403, policy=PROPOSED, call_id='model')
    assert not reference_degrade('GET', REVIEWED['ppi'], 403, policy=PROPOSED, endpoint='AUTH')


def test_native_explicit_unified_policy_preserves_legacy_cpi_scope():
    assert Policy().optional_source_policy == 'strict-v1'
    assert Policy(optional_source_policy=PROPOSED).optional_source_policy == PROPOSED
    assert not optional_cpi_request('GET', REVIEWED['ppi'])
    assert guarded.watchlist(Policy()).enhancements.events is True


def test_native_all_auxiliary_feeds_missing_still_calls_core_and_has_no_fake_events(monkeypatch):
    """Actual collector path with unavailable feed fixture; no guard bypass claimed."""
    from datetime import UTC, datetime
    from tests.test_okx_research import NOW, DAY, SYMBOL
    from tradingagents.dataflows import okx_research
    calls=[]
    class Missing:
        def get(self, url, *args):
            calls.append(url)
            raise ValueError('Fixture source unavailable')
    monkeypatch.setattr(feeds, 'datetime', type('Clock', (), {
        'now': staticmethod(lambda tz: datetime.fromtimestamp(NOW/1000, UTC))}))
    raw=feeds.prepare_events(SYMBOL, DAY, {}, feeds=Missing(), client=Missing())
    assert calls == [v[0] for v in feeds.FEEDS.values() if v[2] in (None, 'BTC')] + ['/api/v5/support/announcements']
    assert not raw['events'] and all(s['status']=='DATA_UNAVAILABLE' for s in raw['sources'].values())
    monkeypatch.setattr(okx_research, 'get_config', lambda: {'okx_events_enabled': True})
    monkeypatch.setattr(feeds, 'prepare_events', lambda *a: raw)
    def build(*a):
        calls.append('CORE_CALLED'); return {**core(), 'allow_new_risk': False}
    monkeypatch.setattr(okx_research, 'build_bundle', build)
    monkeypatch.setattr(okx_research, 'check_bundle', lambda b,*a:b)
    out=okx_research.preflight({'company_of_interest': SYMBOL, 'trade_date': DAY})
    assert calls[-1]=='CORE_CALLED'
    assert not out['event_evidence']['events']
    assert out['event_evidence']['status']=='DATA_UNAVAILABLE'
    assert not out['execution_enabled']


def test_native_configuration_off_does_not_fetch_events_or_bypass_invalid_core(monkeypatch):
    from tradingagents.dataflows import okx_research
    monkeypatch.setattr(okx_research,'get_config',lambda:{'okx_events_enabled':False})
    monkeypatch.setattr(feeds,'prepare_events',lambda *a:pytest.fail('Must not fetch disabled events'))
    monkeypatch.setattr(okx_research,'build_bundle',lambda *a:{'status':'RESEARCH_INVALID','allow_new_risk':False})
    monkeypatch.setattr(okx_research,'check_bundle',lambda b,*a:b)
    monkeypatch.setattr(okx_research,'blocked',lambda b:{'execution_enabled':False})
    out=okx_research.preflight({'company_of_interest':'BTC-USDT-SWAP','trade_date':'2026-10-28'})
    assert out['workflow_status']=='BLOCKED_DATA' and not out['allow_new_risk']
    assert 'event_evidence' not in out
