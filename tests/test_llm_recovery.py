"""D.4.2 mock-only transport, durable role recovery and native graph checks."""
import copy
import json

import httpx2
import openai
import pytest
from langchain_core.messages import AIMessage

from tradingagents.llm_clients.reliability import LLMUnavailable, classify_failure
from tradingagents.llm_clients.recovery import RecoveryController, RecoveringModel, RoleJournal
from tradingagents import okx_agent_comparison as research, okx_llm_recovery as recovery
from tests.test_okx_agent_comparison import payload, Model


def api_error(code=None, status=None, retry=None):
    body = {"code": code, "message": "SECRET never persist", "type": "unknown SECRET"}
    request = httpx2.Request("POST", "https://example.invalid")
    if status:
        response = httpx2.Response(status, request=request, headers={"retry-after": str(retry or 0)})
        cls = openai.RateLimitError if status == 429 else openai.APIStatusError
        return cls("SECRET never persist", response=response, body=body)
    return openai.APIError("SECRET never persist", request=request, body=body)


@pytest.mark.parametrize("code,status,kind", [
    ("insufficient_quota", 429, "QUOTA_EXHAUSTED"),
    ("credit_balance_exhausted", 429, "QUOTA_EXHAUSTED"),
    ("rate_limit_exceeded", 429, "RATE_LIMIT"),
    ("context_length_exceeded", 400, "CONTEXT_LIMIT"),
    ("invalid_api_key", 401, "AUTHENTICATION_OR_PERMISSION"),
    (None, 503, "TRANSIENT_SERVICE_ERROR"),
    (None, None, "MODEL_SERVICE_ERROR"),
    ("server_error", None, "TRANSIENT_SERVICE_ERROR"),
])
def test_safe_specific_classification(code, status, kind):
    failure = classify_failure(api_error(code, status))
    assert failure.kind == kind
    assert "SECRET" not in json.dumps(failure.summary())


class Script:
    max_retries = 0
    def __init__(self, *events): self.events, self.calls = list(events), 0
    def invoke(self, prompt):
        self.calls += 1
        event = self.events.pop(0)
        if isinstance(event, BaseException): raise event
        return AIMessage(content=event)


def facade(tmp_path, script, *, controller=None):
    journal = RoleJournal(tmp_path/"journal.json", {"test": 1})
    control = controller or RecoveryController(sleep=lambda _: None, jitter=lambda: 0)
    return RecoveringModel(script, journal, control, "quick"), journal, control


def test_exponential_retry_and_durable_complete_cache(tmp_path):
    sleeps = []
    model, journal, control = facade(tmp_path, Script(httpx2.ConnectError("SECRET"), api_error(None, 503), "complete"),
        controller=RecoveryController(sleep=sleeps.append, min_interval=0, jitter=lambda: 0))
    assert model.invoke("same").content == "complete"
    assert sleeps == [1, 0, 2, 0]
    assert len(journal.data["roles"][0]["attempts"]) == 3
    resumed = RoleJournal(journal.path, {"test": 1})
    backend = Script()
    assert RecoveringModel(backend, resumed, control, "quick").invoke("same").content == "complete"
    assert backend.calls == 0 and "SECRET" not in journal.path.read_text()


def test_retry_after_and_budget(tmp_path):
    model, journal, _ = facade(tmp_path, Script(api_error("rate_limit_exceeded", 429, 120), "not called"))
    with pytest.raises(LLMUnavailable, match="RETRY_AFTER_EXCEEDS_BUDGET"): model.invoke("test")
    assert len(journal.data["roles"][0]["attempts"]) == 1


@pytest.mark.parametrize("error", [
    httpx2.ReadTimeout("SECRET"), httpx2.RemoteProtocolError("SECRET"),
    LLMUnavailable("STREAM_INTERRUPTED", partial=True),
    LLMUnavailable("TRANSIENT_SERVICE_ERROR", partial=True),
    api_error(None, None), api_error("context_length_exceeded", 400),
])
def test_ambiguous_or_partial_never_automatically_retried(tmp_path, error):
    script = Script(error, "must not be called")
    model, journal, control = facade(tmp_path, script)
    with pytest.raises(LLMUnavailable): model.invoke("test")
    assert script.calls == 1 and journal.data["roles"][0]["status"] == "FAILED"
    with pytest.raises(LLMUnavailable, match="UNSAFE_AUTOMATIC_REPLAY"):
        RecoveringModel(script, RoleJournal(journal.path, {"test": 1}), control, "quick").invoke("test")
    assert script.calls == 1


@pytest.mark.parametrize("error", [api_error("insufficient_quota", 429), api_error("invalid_api_key", 401)])
def test_terminal_stops_following_cases(tmp_path, error):
    model, _, control = facade(tmp_path, Script(error))
    with pytest.raises(LLMUnavailable): model.invoke("first")
    backend = Script("must not be called")
    next_journal = RoleJournal(tmp_path/"next.json", {"test": 2})
    with pytest.raises(LLMUnavailable): RecoveringModel(backend, next_journal, control, "quick").invoke("second")
    assert backend.calls == 0


def test_crash_started_is_not_replayed(tmp_path):
    class Crash:
        def invoke(self, prompt): raise KeyboardInterrupt()
    model, journal, control = facade(tmp_path, Crash())
    with pytest.raises(KeyboardInterrupt): model.invoke("test")
    assert json.loads(journal.path.read_text())["roles"][0]["attempts"][0]["status"] == "STARTED"
    with pytest.raises(LLMUnavailable, match="UNSAFE_AUTOMATIC_REPLAY"):
        RecoveringModel(Script(), RoleJournal(journal.path, {"test": 1}), control, "quick").invoke("test")


def test_total_attempt_limit_survives_resume(tmp_path):
    backend = Script(*[httpx2.ConnectError("SECRET") for _ in range(4)])
    model, journal, control = facade(tmp_path, backend)
    with pytest.raises(LLMUnavailable): model.invoke("same")
    assert backend.calls == 3
    with pytest.raises(LLMUnavailable, match="RETRY_BUDGET_EXHAUSTED"):
        RecoveringModel(backend, RoleJournal(journal.path, {"test": 1}), control, "quick").invoke("same")
    assert backend.calls == 3


def test_prompt_change_and_checkpoint_tamper_blocked(tmp_path):
    model, journal, control = facade(tmp_path, Script("complete"))
    model.invoke("same")
    resumed = RoleJournal(journal.path, {"test": 1})
    with pytest.raises(ValueError, match="signature|mismatch"):
        RecoveringModel(Script(), resumed, control, "quick").invoke("changed")
    resumed.data["roles"][0]["result"]["output"] = "tampered"
    with pytest.raises(ValueError, match="changed"):
        RecoveringModel(Script(), resumed, control, "quick").invoke("same")


def test_native_role_prefix_replayed_without_network_then_resumes(tmp_path):
    original = research.run_case(payload(), Model(), Model(), {})
    # Failure after seven complete roles; only those roles can be reused.
    failed = copy.deepcopy(original)
    failed.update(nodes=recovery.ROLES[:7], calls=failed["calls"][:7])
    imported = recovery.complete_prefix(failed)
    journal = RoleJournal(tmp_path/"native.json", {"test": "native"}, imported=imported)
    model = Model()
    control = RecoveryController(sleep=lambda _: None, min_interval=0)
    result = research.run_case(payload(), RecoveringModel(model, journal, control, "quick"),
                               RecoveringModel(model, journal, control, "deep"), {})
    assert result["status"] == "COMPLETED", result
    assert len(model.prompts) == 2 and len(result["nodes"]) == 9
    assert result["execution_enabled"] is False and result["allow_new_risk"] is False
    assert len(journal.data["roles"]) == 2


def test_complete_node_and_call_mismatch_blocked():
    result = research.run_case(payload(), Model(), Model(), {})
    result["nodes"] = result["nodes"][:-1]
    with pytest.raises(ValueError): recovery.complete_prefix(result)


def test_partial_flag_cannot_be_lost_by_wrapping():
    failure = classify_failure(LLMUnavailable("TRANSIENT_SERVICE_ERROR"), partial=True)
    assert failure.partial


def test_invalid_policy_rejected():
    for kwargs in ({"max_attempts": 99}, {"min_interval": float("nan")}):
        with pytest.raises(ValueError): RecoveryController(**kwargs)


def test_fresh_exploration_is_explicit_and_preserves_source(tmp_path, monkeypatch):
    from tests.test_okx_traded_comparison import frozen
    from tradingagents import okx_traded_comparison as experiment
    root, protocol = frozen(tmp_path, monkeypatch)
    class FailEighth(Model):
        def invoke(self, prompt):
            if len(self.prompts) == 7:
                raise LLMUnavailable("MODEL_SERVICE_ERROR", partial=True)
            return super().invoke(prompt)
    old = FailEighth()
    experiment.execute(root, backend_factory=lambda cfg: (old, old))
    before = recovery.file_hashes(root)
    with pytest.raises(ValueError, match="explicit"):
        recovery.recover(root, tmp_path/"new", "fixed", backend_factory=lambda _: (Model(), Model()))
    new = Model()
    result = recovery.recover(root, tmp_path/"new", "fixed", restart_incomplete=True,
        backend_factory=lambda _: (new, new), controller=RecoveryController(min_interval=0, sleep=lambda _: None))
    assert result["result"]["status"] == "COMPLETED", result
    assert result["reused_complete_roles"] == 7 and len(new.prompts) == 2
    assert result["identity"]["transport_mode"] == "MOCK_NO_NETWORK"
    assert recovery.file_hashes(root) == before
    def forbidden(*args): raise AssertionError("Completed result must not be resampled")
    assert recovery.recover(root, tmp_path/"new", "fixed", restart_incomplete=True,
                            backend_factory=forbidden) == result


def test_stream_error_keeps_safe_provider_code(monkeypatch):
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from tests.test_llm_stream_reliability import model
    def stream(*args, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
        raise api_error("insufficient_quota", 429)
    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    with pytest.raises(LLMUnavailable) as error:
        model(max_retries=0).invoke("test")
    assert error.value.kind == "QUOTA_EXHAUSTED" and error.value.partial
    assert "SECRET" not in json.dumps(error.value.summary())


def test_known_rate_limit_retries_but_ambiguous_429_does_not(tmp_path):
    backend = Script(api_error("rate_limit_exceeded", 429), "done")
    model, _, _ = facade(tmp_path/"known", backend)
    assert model.invoke("test").content == "done" and backend.calls == 2
    backend = Script(api_error(None, 429), "must not be called")
    model, _, _ = facade(tmp_path/"unknown", backend)
    with pytest.raises(LLMUnavailable): model.invoke("test")
    assert backend.calls == 1


def test_usage_and_completed_role_indices_recorded(tmp_path):
    journal = RoleJournal(tmp_path/"usage.json", {"test": "usage"})
    model = RecoveringModel(Model(), journal, RecoveryController(sleep=lambda _: None), "quick")
    model.invoke("one")
    model.invoke("two")
    assert journal.data["roles"][0]["attempts"][0]["usage"]["total_tokens"] == 15
    assert journal.data["roles"][1]["attempts"][0]["completed_roles"] == 1


def test_terminal_campaign_stop_persisted_before_next_client(tmp_path, monkeypatch):
    from tests.test_okx_traded_comparison import frozen
    from tradingagents import okx_traded_comparison as experiment
    root, _ = frozen(tmp_path, monkeypatch)
    experiment.execute(root, backend_factory=lambda _: (Model(fail=LLMUnavailable("MODEL_SERVICE_ERROR")), Model()))
    output = tmp_path/"recovery"
    backend = Model(fail=api_error("insufficient_quota", 429))
    result = recovery.recover(root, output, "fixed", restart_incomplete=True,
                             backend_factory=lambda _: (backend, backend))
    assert result["result"]["status"] == "MODEL_FAILED"
    assert json.loads((output/"campaign_stop.json").read_text())["reason"] == "QUOTA_EXHAUSTED"
    def forbidden(*a): raise AssertionError("No further client after terminal failure")
    with pytest.raises(ValueError, match="Campaign stopped"):
        recovery.recover(root, output, "fixed", restart_incomplete=True, backend_factory=forbidden)
