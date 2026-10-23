"""Credential-free model failure classification; never log exception bodies."""

from __future__ import annotations

import ssl

import openai
from langchain_core.exceptions import ModelError


class LLMUnavailable(RuntimeError):
    """An incomplete/failed model call, with safe diagnostics and no partial result."""

    def __init__(self, kind: str, *, exception_types=(), partial=False, diagnostics=None):
        self.kind = kind
        self.exception_types = list(exception_types)
        self.partial = partial
        self.diagnostics = diagnostics or {}
        self.stage = "model"
        self.research_state = None
        super().__init__(f"LLM_UNAVAILABLE: {kind}; partial_response={partial}")

    def summary(self):
        return {"status": "LLM_UNAVAILABLE", "kind": self.kind, "stage": self.stage,
                "partial_response": self.partial, "exception_types": self.exception_types,
                "diagnostics": self.diagnostics}


# Only known provider codes are persisted. Never persist arbitrary error messages,
# bodies, URLs, headers or credentials (including an unknown free-form code).
QUOTA_CODES = {"insufficient_quota", "quota_exceeded", "usage_limit_reached",
               "billing_hard_limit_reached", "credit_balance_exhausted", "spend_limit_reached"}
ACCOUNT_CODES = {"account_deactivated", "access_terminated", "permission_denied", "invalid_api_key"}
CONTEXT_CODES = {"context_length_exceeded", "max_context_length_exceeded"}
KNOWN_CODES = QUOTA_CODES | ACCOUNT_CODES | CONTEXT_CODES | {
    "rate_limit_exceeded", "rate_limit_error", "slow_down", "server_error", "internal_server_error"}


def safe_diagnostics(chain):
    """Extract a small allowlist before the SDK exception chain is discarded."""
    result = {}
    for error in chain:
        status = getattr(error, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            result.setdefault("http_status", status)
        body = getattr(error, "body", None)
        if not isinstance(body, dict):
            body = {}
        body = body.get("error", body)
        if not isinstance(body, dict):
            body = {}
        for field in ("code", "type"):
            value = getattr(error, field, None) or body.get(field)
            if isinstance(value, str) and value in KNOWN_CODES:
                result.setdefault("provider_"+field, value)
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", {})
        retry = headers.get("retry-after")
        try:
            seconds = float(retry)
            if 0 <= seconds <= 86400:
                result.setdefault("retry_after_seconds", seconds)
        except (ValueError, TypeError):
            pass
        request_id = getattr(error, "request_id", None)
        if isinstance(request_id, str):
            # A hash supports correlation without logging arbitrary header text.
            import hashlib
            result.setdefault("request_id_sha256", hashlib.sha256(request_id.encode()).hexdigest())
    return result


def classify_failure(error: Exception, *, partial=False) -> LLMUnavailable | None:
    from .chatgpt_auth import ChatGPTAuthError, ChatGPTUnreachable

    if isinstance(error, LLMUnavailable):
        error.partial = error.partial or partial
        return error
    chain, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(chain) < 12:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = [type(e).__name__ for e in chain]
    network_modules = ("httpx", "httpx2", "httpcore", "httpcore2")
    transport = [e for e in chain if type(e).__module__.split(".")[0] in network_modules]
    diagnostics = safe_diagnostics(chain)
    codes = {diagnostics.get("provider_code"), diagnostics.get("provider_type")}
    status = diagnostics.get("http_status")
    if codes & QUOTA_CODES:
        kind = "QUOTA_EXHAUSTED"
    elif codes & ACCOUNT_CODES or status in {401, 403}:
        kind = "AUTHENTICATION_OR_PERMISSION"
    elif codes & CONTEXT_CODES:
        kind = "CONTEXT_LIMIT"
    elif any(isinstance(e, ssl.SSLError) for e in chain):
        kind = "TLS_ERROR"
    elif any(isinstance(e, ChatGPTUnreachable) for e in chain):
        kind = "AUTH_REFRESH_UNAVAILABLE"
    elif any(isinstance(e, (ChatGPTAuthError, openai.AuthenticationError, openai.PermissionDeniedError)) for e in chain):
        kind = "AUTHENTICATION_OR_PERMISSION"
    elif any(isinstance(e, openai.RateLimitError) for e in chain):
        kind = "RATE_LIMIT"
    elif status in {408, 409, 429, 500, 502, 503, 504}:
        kind = "RATE_LIMIT" if status == 429 else "TRANSIENT_SERVICE_ERROR"
    elif codes & {"server_error", "internal_server_error"}:
        kind = "TRANSIENT_SERVICE_ERROR"
    elif any(type(e).__name__ in {"ConnectTimeout", "ConnectError", "PoolTimeout"} for e in transport):
        kind = "CONNECT_FAILURE"
    elif any(type(e).__name__ in {"RemoteProtocolError", "ReadError", "WriteError"} for e in transport):
        kind = "STREAM_INTERRUPTED" if partial else "TRANSPORT_INTERRUPTED"
    elif any("Timeout" in type(e).__name__ for e in transport) or isinstance(error, openai.APITimeoutError):
        kind = "STREAM_TIMEOUT" if partial else "REQUEST_TIMEOUT"
    elif isinstance(error, openai.APIConnectionError):
        kind = "CONNECTION_ERROR"
    elif isinstance(error, (openai.APIError, ModelError)):
        kind = "MODEL_SERVICE_ERROR"
    else:
        return None  # Do not disguise programming/parsing bugs as network failures.
    return LLMUnavailable(kind, exception_types=names, partial=partial, diagnostics=diagnostics)
