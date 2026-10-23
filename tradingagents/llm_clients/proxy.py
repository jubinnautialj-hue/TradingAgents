"""Model-only loopback proxy routing; never changes environment/global proxies."""

from urllib.parse import urlsplit


def validate_model_proxy(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "socks5", "socks5h"}
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Model proxy must be an unauthenticated loopback HTTP/SOCKS URL with a port") from None
    return value


def subscription_proxy_kwargs() -> dict:
    # Credentials remain in the existing auth implementation; only its network
    # route changes. Explicit proxies prevent a failed tunnel falling back direct.
    from tradingagents.dataflows.config import get_config
    value = get_config().get("llm_proxy")
    if not value:
        return {}
    value = validate_model_proxy(value)
    return {"proxies": {"http": value, "https": value}}
