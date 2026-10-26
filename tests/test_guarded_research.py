"""The real LangChain/HTTP interception path, using only MockTransport."""
import json
import copy
from unittest.mock import patch

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents import auto_research as native
from tradingagents import guarded_research as g
from tradingagents.llm_clients.reliability import LLMUnavailable
from tradingagents.safety_budget import Ledger, Policy, SafetyStop
from tests.test_forward_validation import setup as f1_setup
from tests.test_perpetual_plan import choice, CONFIG, research  # pytest fixture
from tests.test_okx_research import NOW


class FixtureModel(BaseChatModel):
    model_name: str = "gpt-6.1-sol"
    total: int | None = 100
    http_status: int = 200
    partial: bool = False
    wire_count: int = 1
    response_id: str = "resp_fixture"

    @property
    def _llm_type(self): return "D4B_TEST_ONLY"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        import langchain_openai.chat_models.base as sdk
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
                self.http_status, headers={"x-request-id": "PRIVATE_REQUEST_ID"}))) as client:
            for _ in range(self.wire_count):
                r = client.post("https://api.openai.com/v1/responses", headers={"Authorization": "PRIVATE_TOKEN"},
                                json={"input": "PRIVATE_PROMPT"})
                r.raise_for_status()
        if self.partial: raise LLMUnavailable("STREAM_INCOMPLETE", partial=True)
        raw = {"input_tokens": 80, "output_tokens": 20,
               "input_tokens_details": {"cached_tokens": 30}, "output_tokens_details": {"reasoning_tokens": 7}}
        if self.total is not None: raw["total_tokens"] = self.total
        usage = sdk._create_usage_metadata_responses(raw)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="PRIVATE_RESPONSE", usage_metadata=usage,
                              response_metadata={"id": self.response_id, "status": "completed"}))])


@pytest.fixture
def audited(tmp_path):
    ledger = Ledger(tmp_path / "global"); ledger.initialize(Policy())
    rid = ledger.start_research(); session = g.Session(ledger, rid, health=lambda: None)
    meter = session.meter_class()(tmp_path / "meter.json", 24, 240, 900)
    return ledger, rid, session, meter


def test_real_invoke_interception_with_no_network(audited):
    l, r, s, meter = audited
    with s.guards(meter): assert FixtureModel().invoke("PRIVATE_PROMPT").content == "PRIVATE_RESPONSE"
    assert meter.value["reported_total_tokens"] == l.status()["tokens"] == 100
    assert l.status()["http"] == l.status()["llm_calls"] == 1
    assert not l.status()["paused"]
    l.finish_research(r, meter.value["reported_total_tokens"], success=True)
    text = l.path.read_text()
    for private in ("PRIVATE_PROMPT", "PRIVATE_RESPONSE", "PRIVATE_TOKEN", "PRIVATE_REQUEST_ID", "Authorization", "resp_fixture"):
        assert private not in text


@pytest.mark.parametrize("model", [FixtureModel(total=None), FixtureModel(total=101), FixtureModel(wire_count=2)])
def test_missing_inconsistent_or_retry_ambiguous_usage_stops(audited, model):
    l, _, s, meter = audited
    with s.guards(meter):
        with pytest.raises(SafetyStop): model.invoke("test")
    assert l.status()["paused"]


def test_partial_stream_cannot_complete(audited):
    l, _, s, meter = audited
    with s.guards(meter):
        with pytest.raises(LLMUnavailable): FixtureModel(partial=True).invoke("test")
    call = next(iter(l.status()["calls"].values()))
    assert call["status"] == "UNKNOWN" and call["completion_status"] == "PARTIAL"


def test_http_403_no_blind_retries(audited):
    l, _, s, meter = audited
    with s.guards(meter):
        with pytest.raises(httpx.HTTPStatusError): FixtureModel(http_status=403).invoke("test")
        with pytest.raises(SafetyStop): FixtureModel().invoke("test")
    assert l.status()["http"] == 1


def test_duplicate_response_detected_across_invokes(audited):
    l, _, s, meter = audited
    with s.guards(meter):
        FixtureModel().invoke("test")
        with pytest.raises(SafetyStop): FixtureModel().invoke("test")
    assert l.status()["tokens"] == 100 and l.status()["paused"]


def test_public_stream_denied_internal_sse_path_not_replaced(audited):
    l, _, s, meter = audited
    with s.guards(meter):
        with pytest.raises(SafetyStop): list(FixtureModel().stream("test"))
    assert l.status()["http"] == 0 and l.status()["llm_calls"] == 0


def test_context_patches_restore_legacy(audited):
    _, _, s, meter = audited; old = BaseChatModel._generate_with_cache
    with s.guards(meter): assert BaseChatModel._generate_with_cache is not old
    assert BaseChatModel._generate_with_cache is old


def test_btc_configuration_and_original_cli_reuse():
    cfg = g.watchlist(Policy())
    assert cfg.enabled() == ["BTC-USDT-SWAP"]
    assert cfg.limits.min_interval_seconds == 86400
    assert cfg.limits.llm_per_task == 24 and cfg.limits.http_total == 240
    assert not cfg.execution_enabled


@pytest.mark.parametrize("method,url", [("POST", "https://www.okx.com/api/v5/trade/order"),
                                       ("POST", "https://api.binance.com/order"), ("DELETE", "https://www.okx.com/api/v5/trade/order")])
def test_private_writes_blocked(method, url):
    with pytest.raises(SafetyStop): g.endpoint(method, url)


def test_fixture_cannot_launch_real_worker(audited):
    l, r, _, __ = audited
    with pytest.raises(SafetyStop): g.worker(l, r)
    with pytest.raises(SafetyStop): g.launch(l)


def test_unknown_orphan_recovery_is_not_retry(audited):
    l, r, _, __ = audited; l.begin_call(r)
    l.recover_interrupted()
    assert l.status()["runs"][r]["status"] == "FAILED"
    with pytest.raises(SafetyStop): l.resume()
    with pytest.raises(SafetyStop): l.start_research()


def test_no_resource_protection_no_production_entry():
    with patch.object(g.sys, "platform", "win32"):
        with pytest.raises(SafetyStop, match="LINUX_CGROUP_REQUIRED"): g.resource_check(Policy())


@pytest.mark.parametrize("mode", ["candidate", "wait", "failure"])
def test_native_worker_and_f1_end_to_end(f1_setup, tmp_path_factory, monkeypatch, mode):
    from tradingagents.forward_validation import Registry, atomic
    from tradingagents.dataflows import perpetual_plan
    ledger = Ledger(tmp_path_factory.mktemp("d4b"), clock=lambda: NOW + 2000)
    ledger.initialize(Policy()); rid = ledger.start_research()
    monkeypatch.setattr(native, "utc_ms", lambda: NOW + 2000)
    monkeypatch.setattr(native, "validate_pool", lambda cfg, root: {
        "instruments": [{"instrument": "BTC-USDT-SWAP", "status": "VALID"}],
        "meter": {"http_dispatches": 0}, "execution_enabled": False})
    state = copy.deepcopy(f1_setup[2]); bundle = state["market_snapshot"]
    if mode == "wait":
        state["portfolio_choice"] = choice("WAIT")
        value, _ = perpetual_plan.finalize_result(state, bundle, CONFIG)
        state["research_result"] = value.model_dump(mode="json")
    def invoke(argv):
        assert "--forward-register" in argv and "--trade-plan" in argv
        assert "BTC-USDT-SWAP" in argv and "ETH-USDT-SWAP" not in argv
        if mode == "failure":
            try: FixtureModel(partial=True).invoke("fixture")
            except LLMUnavailable:
                state.update(research_result=None, workflow_status="FAILED", llm_status="LLM_UNAVAILABLE",
                             llm_failure={"kind": "STREAM_INCOMPLETE"})
        else: FixtureModel().invoke("fixture")
        root = ledger.root / "batch/fixture/research"
        registry = Registry(root / "forward_validation/f1")
        token = registry.begin("BTC-USDT-SWAP", CONFIG)
        report = root / "reports/fixture"; report.mkdir(parents=True)
        (report / "complete_report.md").write_text("Fixture only")
        (report / "complete_report.html").write_text("Fixture only")
        atomic(report / "multitimeframe_snapshot.json", bundle)
        if state.get("research_result"): atomic(report / "research_result.json", state["research_result"])
        registry.register(report, state, token=token, completed_ms=NOW + 1000)
    g.worker(ledger, rid, invoker=invoke)
    records = list(Registry(ledger.root / "batch/fixture/research/forward_validation/f1").registrations())
    assert len(records) == 1 and records[0].origin == "TEST_REPLAY"
    assert records[0].allow_new_risk is False and not records[0].execution_enabled
    if mode == "failure":
        assert records[0].workflow_status == "FAILED" and ledger.status()["paused"]
    else:
        assert ledger.status()["runs"][rid]["status"] == "COMPLETED"
        assert records[0].action == ("WAIT" if mode == "wait" else "OPEN_LONG_CANDIDATE")


@pytest.mark.parametrize("bad", [None, "memory", "cpu", "swap", "tasks", "available", "disk", "ntp", "nice"])
def test_linux_resource_contract_with_isolated_metadata(monkeypatch, bad):
    import sys
    import types
    import shutil
    import pathlib
    monkeypatch.setattr(g.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "pwd", types.SimpleNamespace(getpwnam=lambda _: types.SimpleNamespace(pw_uid=999)))
    monkeypatch.setattr(g.os, "geteuid", lambda: 999, raising=False)
    monkeypatch.setattr(g.os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(g.os, "getpriority", lambda *a: 0 if bad == "nice" else 10, raising=False)
    values = {"cgroup": "0::/fixture\n", "memory.max": "1610612736", "memory.swap.max": "0",
              "pids.max": "64", "cpu.max": "100000 100000", "meminfo": "MemAvailable: 2200000 kB\n"}
    for key, field, value in [("memory", "memory.max", "max"), ("cpu", "cpu.max", "200000 100000"),
                               ("swap", "memory.swap.max", "max"), ("tasks", "pids.max", "max"),
                               ("available", "meminfo", "MemAvailable: 1000 kB\n")]:
        if bad == key: values[field] = value
    monkeypatch.setattr(pathlib.Path, "read_text", lambda self, *a, **k: values[self.name])
    monkeypatch.setattr(shutil, "disk_usage", lambda _: types.SimpleNamespace(free=1 if bad == "disk" else 20 * 1024**3))
    monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="NTPSynchronized=no" if bad == "ntp" else "NTPSynchronized=yes"))
    if bad is None: g.resource_check(Policy())
    else:
        with pytest.raises(SafetyStop): g.resource_check(Policy())


def test_resource_failure_stops_worker_before_invocation(audited, monkeypatch):
    l, rid, _, __ = audited
    def stop(*args): raise SafetyStop("RESOURCE_FLOOR")
    monkeypatch.setattr(g.Session, "check", stop)
    with pytest.raises(SafetyStop): g.worker(l, rid, invoker=lambda _: pytest.fail("must not invoke"))
    assert l.status()["paused"] and l.status()["runs"][rid]["status"] == "FAILED"


def test_supervisor_timeout_kills_only_owned_process_group(tmp_path, monkeypatch):
    import subprocess
    l = Ledger(tmp_path); l.initialize(Policy(enabled=True, accepted_post_response_exposure=True), approved=True, namespace="PRODUCTION")
    monkeypatch.setattr(g, "resource_check", lambda _: None)
    monkeypatch.setattr(g, "reviewed_implementation", lambda: None)
    killed = []
    class Process:
        pid = 12345
        def __init__(self, *a, **k): self.calls = 0
        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1: raise subprocess.TimeoutExpired("FIXTURE", 900)
            return -9
    monkeypatch.setattr(g.subprocess, "Popen", Process)
    monkeypatch.setattr(g.os, "killpg", lambda pid, sig: killed.append(pid), raising=False)
    monkeypatch.setattr(g.signal, "SIGKILL", 9, raising=False)
    with pytest.raises(subprocess.TimeoutExpired): g.launch(l)
    assert killed == [12345] and l.status()["paused"]


@pytest.mark.parametrize("completed", [True, False])
def test_installed_responses_sdk_sse_raw_usage_path(audited, completed):
    import httpx2
    from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI
    l, _, session, meter = audited
    message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
    response = {"id": "resp_sse_fixture", "object": "response", "created_at": 0, "status": "completed",
                "model": "gpt-6.1-sol", "output": [message], "error": None, "incomplete_details": None,
                "usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100,
                          "input_tokens_details": {"cached_tokens": 30}, "output_tokens_details": {"reasoning_tokens": 7}}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": [], "usage": None}},
              {"type": "response.completed", "response": response}]
    if not completed: events = events[:1]
    text = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)
    with httpx2.Client(transport=httpx2.MockTransport(lambda req: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=text))) as client:
        model = ChatGPTPlanChatOpenAI(model="gpt-6.1-sol", api_key=lambda: "test-placeholder", use_responses_api=True,
                                     http_client=client, max_retries=0)
        with session.guards(meter):
            if completed: model.invoke("FIXTURE_ONLY")
            else:
                with pytest.raises(LLMUnavailable): model.invoke("FIXTURE_ONLY")
    if completed:
        assert l.status()["tokens"] == meter.value["reported_total_tokens"] == 100
        assert not l.status()["paused"]
    else: assert l.status()["paused"] and l.status()["total_usage"] == "UNKNOWN"


def test_policy_schema_default_and_resource_template():
    assert json.loads((g.ROOT / "docs/d4b_safety_budget.json").read_text()) == Policy().model_dump()
    assert json.loads((g.ROOT / "docs/d4b_safety_budget.schema.json").read_text()) == Policy.model_json_schema()
    text = (g.ROOT / "docs/d4b_btc_research.service.example").read_text()
    for line in ("Restart=no", "MemoryMax=1536M", "MemorySwapMax=256M", "CPUQuota=100%", "RuntimeMaxSec=900", "TasksMax=64"):
        assert line in text
    assert "ExecStart=" in text and "guarded_research run" in text


def test_native_parallel_analysts_dispatch_serially_without_usage_mix(audited):
    from concurrent.futures import ThreadPoolExecutor
    l, _, session, meter = audited
    with session.guards(meter), ThreadPoolExecutor(max_workers=3) as pool:
        answers = list(pool.map(lambda i: FixtureModel(response_id="resp_role_" + str(i)).invoke("fixture"), range(3)))
    assert len(answers) == 3 and not l.status()["paused"]
    assert l.status()["tokens"] == meter.value["reported_total_tokens"] == 300
    assert l.status()["llm_calls"] == l.status()["http"] == 3
