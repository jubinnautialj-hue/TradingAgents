"""D4-B.1.1: synthetic credentials and mock transports only; no external I/O."""
import importlib
import json
import time

import pytest
import requests

from tradingagents import guarded_research as g
from tradingagents.llm_clients import chatgpt_auth as auth
from tradingagents.safety_budget import Ledger, Policy, SafetyStop
from tests.test_chatgpt_auth import _jwks, _renewal, _signed_in
from tests.test_guarded_research import audited, FixtureModel


def test_native_token_url_is_the_only_path_source(monkeypatch):
    assert g.endpoint("POST", auth.TOKEN_URL) == "AUTH"
    assert g.endpoint("GET", auth.JWKS_URL) == "PUBLIC_GET"
    old = auth.TOKEN_URL
    monkeypatch.setattr(auth, "TOKEN_URL", "https://auth.openai.com/fixture-canonical-renewal")
    assert g.endpoint("POST", auth.TOKEN_URL) == "AUTH"
    with pytest.raises(SafetyStop): g.endpoint("POST", old)


@pytest.mark.parametrize("url", [
    "http://auth.openai.com/api/accounts/oauth/token",
    "https://evil.example/api/accounts/oauth/token",
    "https://auth.openai.com.evil.example/api/accounts/oauth/token",
    "https://sub.auth.openai.com/api/accounts/oauth/token",
    "https://auth.openai.com:444/api/accounts/oauth/token",
    "https://user@auth.openai.com/api/accounts/oauth/token",
    "https://auth.openai.com/oauth/token",
    "https://auth.openai.com/api/accounts/oauth/token/",
    "https://auth.openai.com/api/accounts/oauth/token-extra",
    "https://auth.openai.com/api/accounts/oauth/%74oken",
    "https://auth.openai.com/other/../api/accounts/oauth/token",
    "https://auth.openai.com/api/accounts/oauth/token?query=1",
    "https://auth.openai.com/api/accounts/oauth/token#fragment",
    "https://auth.openai.com/api/accounts/oauth/revoke",
    "https://api.openai.com/arbitrary/responses",
    "https://chatgpt.com/arbitrary/responses",
    "https://auth.openai.com\\@evil.example/api/accounts/oauth/token",
])
def test_noncanonical_posts_denied(url):
    with pytest.raises(SafetyStop): g.endpoint("POST", url)


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_other_token_methods_denied(method):
    with pytest.raises(SafetyStop): g.endpoint(method, auth.TOKEN_URL)


@pytest.mark.parametrize("url", ["http://auth.openai.com/token", "https://evil.example/token"])
def test_untrusted_canonical_constant_cannot_expand_authority(monkeypatch, url):
    monkeypatch.setattr(auth, "TOKEN_URL", url)
    with pytest.raises(SafetyStop): g.endpoint("POST", url)


class SyntheticAuthAdapter(requests.adapters.BaseAdapter):
    """Records routes only, never prepared request objects, headers or form data."""
    def __init__(self, *, status=200, redirect_at=None, location="https://evil.example/collect", error=False, malformed=False):
        self.status, self.redirect_at, self.location, self.error = status, redirect_at, location, error
        self.malformed = malformed
        self.routes = []

    def send(self, request, **kwargs):
        self.routes.append((request.method, request.url))
        is_keys = request.url == auth.JWKS_URL
        assert request.url in {auth.JWKS_URL, auth.TOKEN_URL}, "Unexpected forwarding destination"
        if self.error and not is_keys: raise requests.ConnectionError("SYNTHETIC_CONNECTION_FAILURE")
        response = requests.Response()
        response.status_code = 200 if is_keys else self.status
        response._content = json.dumps(_jwks() if is_keys else {} if self.malformed else _renewal(None)).encode()
        response._content_consumed = True
        response.url, response.request = request.url, request
        if self.redirect_at == ("keys" if is_keys else "token"):
            response.status_code = self.status
            response.headers["Location"] = self.location
        return response

    def close(self): pass


@pytest.fixture
def synthetic_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "synthetic-only" / "auth.json")
    monkeypatch.setattr(auth, "_retry_after", 0.0)
    _signed_in(expires_at=time.time() - 10)

    def install(**kwargs):
        adapter = SyntheticAuthAdapter(**kwargs)
        monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: adapter)
        return adapter
    return install


class RenewingFixtureModel(FixtureModel):
    def _generate(self, *args, **kwargs):
        auth.credential()()  # Original refresh, signed-ID validation, lock and atomic write.
        return super()._generate(*args, **kwargs)


def test_native_renewal_then_model_accounting(audited, synthetic_auth):
    ledger, rid, session, meter = audited
    adapter = synthetic_auth()
    with session.guards(meter): RenewingFixtureModel().invoke("SYNTHETIC_ONLY")
    state = ledger.status()
    assert adapter.routes == [("GET", auth.JWKS_URL), ("POST", auth.TOKEN_URL)]
    assert [w["endpoint_kind"] for w in state["wires"].values()] == ["PUBLIC_GET", "AUTH", "MODEL"]
    assert state["http"] == meter.value["http_dispatches"] == 3
    assert state["llm_calls"] == meter.value["llm_calls"] == 1
    assert state["tokens"] == meter.value["reported_total_tokens"] == 100
    assert not state["paused"]
    assert auth._read()["tokens"]["expires_at"] > time.time() + 300
    ledger.finish_research(rid, 100, success=True)
    assert ledger.status()["runs"][rid]["status"] == "COMPLETED"
    journal = ledger.path.read_text()
    for secret in ("refresh_token", "access_token", "id_token", "Authorization", "SYNTHETIC_ONLY", "a@example.com"):
        assert secret not in journal


@pytest.mark.parametrize("status", [400, 401, 403, 408, 429, 500, 503])
def test_auth_failure_pauses_without_model_dispatch(audited, synthetic_auth, status):
    ledger, _, session, meter = audited
    adapter = synthetic_auth(status=status)
    before = auth.AUTH_FILE.read_bytes()
    with session.guards(meter), pytest.raises(SafetyStop): RenewingFixtureModel().invoke("fixture")
    assert len(adapter.routes) == 2
    assert ledger.status()["paused"] and ledger.status()["total_usage"] == "UNKNOWN"
    assert all(w["endpoint_kind"] != "MODEL" for w in ledger.status()["wires"].values())
    assert auth.AUTH_FILE.read_bytes() == before


def test_auth_connection_failure_does_not_retry_or_refresh_ledger(audited, synthetic_auth):
    ledger, _, session, meter = audited
    adapter = synthetic_auth(error=True)
    with session.guards(meter), pytest.raises(auth.ChatGPTUnreachable): RenewingFixtureModel().invoke("fixture")
    before = ledger.path.read_bytes()
    with pytest.raises(SafetyStop): ledger.start_research()
    assert ledger.path.read_bytes() == before and len(adapter.routes) == 2


@pytest.mark.parametrize("still_valid", [False, True])
def test_unreadable_auth_success_cannot_use_native_fallback_to_bypass_pause(audited, synthetic_auth, still_valid):
    ledger, _, session, meter = audited
    adapter = synthetic_auth(malformed=True)
    _signed_in(expires_at=time.time() + (60 if still_valid else -10))
    before = auth.AUTH_FILE.read_bytes()
    with session.guards(meter), pytest.raises((auth.ChatGPTUnreachable, SafetyStop)):
        RenewingFixtureModel().invoke("fixture")
    assert len(adapter.routes) == 2 and "AUTH_RENEWAL_FAILED" in ledger.status()["paused"]
    assert not any(w["endpoint_kind"] == "MODEL" for w in ledger.status()["wires"].values())
    assert auth.AUTH_FILE.read_bytes() == before


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("stage", ["keys", "token"])
def test_native_requests_redirect_never_forwarded(audited, synthetic_auth, status, stage):
    ledger, _, session, meter = audited
    adapter = synthetic_auth(status=status, redirect_at=stage)
    before = auth.AUTH_FILE.read_bytes()
    with session.guards(meter), pytest.raises(SafetyStop, match="PROTECTED_REDIRECT_DENIED"):
        RenewingFixtureModel().invoke("fixture")
    assert len(adapter.routes) == (1 if stage == "keys" else 2)
    assert auth.AUTH_FILE.read_bytes() == before and ledger.status()["paused"]


@pytest.mark.parametrize("location", [auth.TOKEN_URL, "http://auth.openai.com/token"])
def test_same_host_and_downgrade_redirects_also_denied(audited, synthetic_auth, location):
    ledger, _, session, meter = audited
    adapter = synthetic_auth(status=307, redirect_at="token", location=location)
    with session.guards(meter), pytest.raises(SafetyStop): RenewingFixtureModel().invoke("fixture")
    assert len(adapter.routes) == 2 and ledger.status()["paused"]


def test_token_endpoint_without_native_renewal_context_is_denied(audited, synthetic_auth):
    ledger, _, session, meter = audited
    adapter = synthetic_auth()
    with session.guards(meter), pytest.raises(SafetyStop, match="AUTH_RENEWAL_CONTEXT_REQUIRED"):
        requests.post(auth.TOKEN_URL, data={"fixture": True})
    assert adapter.routes == [] and ledger.status()["http"] == 0


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
@pytest.mark.parametrize("location", ["https://evil.example/collect", "https://api.openai.com/v1/responses", "http://api.openai.com/v1/responses"])
def test_model_redirect_rejected_before_any_second_send(audited, module_name, location):
    module = importlib.import_module(module_name)
    ledger, rid, session, meter = audited
    routes = []
    def handler(request):
        routes.append(str(request.url))
        return module.Response(307, headers={"location": location})
    cid = ledger.begin_call(rid)
    marker = g.CALL.set(cid)
    try:
        with module.Client(transport=module.MockTransport(handler), follow_redirects=True) as client:
            with session.guards(meter), pytest.raises(SafetyStop, match="PROTECTED_REDIRECT_DENIED"):
                client.post("https://api.openai.com/v1/responses", json={"fixture": True})
    finally: g.CALL.reset(marker)
    assert routes == ["https://api.openai.com/v1/responses"]
    assert ledger.status()["http"] == 1 and ledger.status()["paused"]


def test_auth_requests_still_consume_http_budget(tmp_path, synthetic_auth):
    ledger = Ledger(tmp_path / "limited")
    ledger.initialize(Policy(daily_http=2))
    rid = ledger.start_research()
    session = g.Session(ledger, rid, health=lambda: None)
    meter = session.meter_class()(tmp_path / "meter.json", 24, 240, 900)
    adapter = synthetic_auth()
    with session.guards(meter), pytest.raises(SafetyStop, match="HTTP_BUDGET"):
        RenewingFixtureModel().invoke("fixture")
    assert len(adapter.routes) == ledger.status()["http"] == 2
    assert not any(w["endpoint_kind"] == "MODEL" for w in ledger.status()["wires"].values())
    assert ledger.status()["paused"] and ledger.status()["total_usage"] == "UNKNOWN"


def test_paused_ledger_is_not_reset_by_new_guard(audited):
    ledger, _, _, _ = audited
    with ledger.transaction() as (obj, _, __): ledger._pause(obj, "UNKNOWN_OR_FAILED_USAGE")
    before = ledger.path.read_bytes()
    restarted = Ledger(ledger.root)
    with pytest.raises(SafetyStop): restarted.start_research()
    with pytest.raises(SafetyStop): restarted.resume()
    assert ledger.path.read_bytes() == before


def test_old_implementation_binding_is_rejected_without_migration(audited, monkeypatch):
    from tradingagents import safety_budget
    ledger, _, _, _ = audited
    before = ledger.path.read_bytes()
    # Simulate the older binary inspecting a new ledger, without editing either ledger.
    monkeypatch.setattr(safety_budget, "implementation_hash", lambda: "0" * 64)
    with pytest.raises(SafetyStop, match="IMPLEMENTATION_CHANGED"): Ledger(ledger.root).load()
    assert ledger.path.read_bytes() == before


def test_installed_sdk_with_native_credential_refresh_and_complete_sse(audited, synthetic_auth):
    import httpx2
    from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI
    ledger, rid, session, meter = audited
    adapter = synthetic_auth()
    response = {"id": "resp_synthetic_renewal", "object": "response", "created_at": 0,
                "status": "completed", "model": "gpt-6.1-sol", "error": None, "incomplete_details": None,
                "output": [{"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "OK", "annotations": []}]}],
                "usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100,
                          "input_tokens_details": {"cached_tokens": 30},
                          "output_tokens_details": {"reasoning_tokens": 7}}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": [], "usage": None}},
              {"type": "response.completed", "response": response}]
    content = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)
    with httpx2.Client(transport=httpx2.MockTransport(lambda req: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=content))) as client:
        model = ChatGPTPlanChatOpenAI(model="gpt-6.1-sol", api_key=auth.credential(), use_responses_api=True,
                                     http_client=client, max_retries=0)
        with session.guards(meter): model.invoke("SYNTHETIC_ONLY")
    assert len(adapter.routes) == 2
    assert ledger.status()["http"] == 3 and ledger.status()["llm_calls"] == 1
    assert ledger.status()["tokens"] == meter.value["reported_total_tokens"] == 100
    ledger.finish_research(rid, 100, success=True)
    assert not ledger.status()["paused"]
