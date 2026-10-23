"""Model proxy routing stays separate from public market data and credentials."""

import json
from unittest.mock import patch

import pytest

from tradingagents import default_config
from tradingagents.dataflows.config import run_config
from tradingagents.llm_clients.factory import build_llm_kwargs, create_llm_client
from tradingagents.llm_clients.proxy import subscription_proxy_kwargs, validate_model_proxy

PROXY = "socks5h://127.0.0.1:10809"


@pytest.mark.parametrize("value", [PROXY, "http://localhost:10809", "socks5://[::1]:10809"])
def test_loopback_proxy(value):
    assert validate_model_proxy(value) == value


@pytest.mark.parametrize("value", ["https://example.com:443", "socks5h://user:password@127.0.0.1:10809",
                                   "http://127.0.0.1", "http://127.0.0.1:80/path", "garbage"])
def test_invalid_proxy_does_not_echo_input(value):
    with pytest.raises(ValueError) as raised:
        validate_model_proxy(value)
    assert value not in str(raised.value)


def test_only_openai_model_family_changes():
    assert build_llm_kwargs({"llm_provider": "chatgpt", "llm_proxy": PROXY})["model_proxy"] == PROXY
    assert build_llm_kwargs({"llm_provider": "google", "llm_proxy": PROXY}) == {}
    assert build_llm_kwargs({"llm_provider": "openai", "llm_proxy": None}) == {}


def test_explicit_client_routes_override_system_proxy(monkeypatch):
    # A conflicting system/env proxy must not win over the model-only route.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    with patch("tradingagents.llm_clients.chatgpt_auth.credential", return_value=lambda: "test-placeholder"):
        llm = create_llm_client("chatgpt", "gpt-6.1-sol", model_proxy=PROXY).get_llm()
    for client in (llm.root_client._client, llm.root_async_client._client):
        assert client.trust_env is False
        # Version-compatible SDK client transport; no network/auth invocation.
        import httpx2
        transport = client._transport_for_url(httpx2.URL("https://api.openai.com/v1/responses"))
        assert transport._pool._proxy_url.port == 10809


def test_auth_proxy_uses_run_config_without_changing_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    with run_config({"llm_proxy": PROXY}):
        assert subscription_proxy_kwargs() == {"proxies": {"http": PROXY, "https": PROXY}}
    import os
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"


def test_saved_routing_and_env_disable(tmp_path, monkeypatch):
    path = tmp_path / "model_network.json"
    path.write_text(json.dumps({"llm_proxy": PROXY}), encoding="utf-8")
    monkeypatch.setenv("TRADINGAGENTS_MODEL_NETWORK_FILE", str(path))
    monkeypatch.delenv("TRADINGAGENTS_LLM_PROXY", raising=False)
    assert default_config.build_default_config()["llm_proxy"] == PROXY
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROXY", "")
    assert default_config.build_default_config()["llm_proxy"] is None


def test_invalid_profile_not_silently_bypassed(tmp_path, monkeypatch):
    path = tmp_path / "model_network.json"
    path.write_text("invalid json", encoding="utf-8")
    monkeypatch.setenv("TRADINGAGENTS_MODEL_NETWORK_FILE", str(path))
    monkeypatch.delenv("TRADINGAGENTS_LLM_PROXY", raising=False)
    with pytest.raises(ValueError, match="not silently bypassed"):
        default_config.build_default_config()
