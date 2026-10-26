"""No model, network, private account, or production archive calls."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from tradingagents import safety_budget as b
from tradingagents.guarded_research import normalize_usage


@pytest.fixture
def budget(tmp_path):
    clock = [int(datetime(2026, 10, 26, 0, 0, tzinfo=UTC).timestamp() * 1000)]
    ledger = b.Ledger(tmp_path / "global", clock=lambda: clock[0])
    ledger.initialize(b.Policy(), namespace="FIXTURE")
    return ledger, clock


def usage(n=100):
    return normalize_usage({"input_tokens": n - 20, "output_tokens": 20, "total_tokens": n,
                            "input_tokens_details": {"cached_tokens": 30},
                            "output_tokens_details": {"reasoning_tokens": 7}})


def complete(ledger, rid, n=100, response_hash=None):
    cid = ledger.begin_call(rid)
    wid = ledger.begin_http(rid, cid, "MODEL")
    ledger.finish_http(wid, status=200)
    ledger.finish_call(cid, usage(n), response_hash=response_hash)
    return cid


def test_counts_and_reconciliation(budget):
    l, _ = budget; r = l.start_research(); complete(l, r); complete(l, r, 200)
    l.finish_research(r, 300, success=True)
    s = l.status()
    assert (s["tokens"], s["llm_calls"], s["http"]) == (300, 2, 2)
    assert not s["paused"] and s["cost"] == "UNKNOWN"


def test_cached_and_reasoning_not_added_twice(budget):
    l, _ = budget; r = l.start_research(); cid = complete(l, r)
    assert l.status()["tokens"] == 100
    u = l.status()["calls"][cid]["usage"]
    assert u["cached_input_tokens"] == 30 and u["reasoning_tokens"] == 7


@pytest.mark.parametrize("raw", [{}, {"input_tokens": 1}, {"input_tokens": 1, "output_tokens": 2},
                                  {"input_tokens": 1, "output_tokens": 2, "total_tokens": 4},
                                  {"input_tokens": True, "output_tokens": 2, "total_tokens": 3}])
def test_missing_or_wrong_usage_not_valid(raw):
    assert normalize_usage(raw)["valid"] is False


def test_unknown_details_not_zero():
    u = normalize_usage({"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})
    assert u["valid"] and u["cached_input_tokens"] is None and u["reasoning_tokens"] is None


@pytest.mark.parametrize("state", ["FAILED", "PARTIAL", "UNKNOWN"])
def test_failed_call_pauses_across_restart_and_day(budget, state):
    l, clock = budget; r = l.start_research(); c = l.begin_call(r)
    w = l.begin_http(r, c, "MODEL"); l.finish_http(w, status=200)
    l.finish_call(c, None, status=state)
    clock[0] += b.DAY
    restored = b.Ledger(l.root, clock=lambda: clock[0])
    assert restored.status()["unknown_usage"]
    with pytest.raises(b.SafetyStop): restored.start_research()
    with pytest.raises(b.SafetyStop): restored.resume()


def test_normalized_disagreement_pauses(budget):
    l, _ = budget; r = l.start_research(); c = l.begin_call(r)
    w = l.begin_http(r, c, "MODEL"); l.finish_http(w, status=200)
    l.finish_call(c, usage(), reconciliation=False)
    assert l.status()["paused"]


def test_run_aggregate_disagreement_pauses(budget):
    l, _ = budget; r = l.start_research(); complete(l, r)
    l.finish_research(r, 101, success=True)
    assert "RESEARCH_FAILURE_OR_RECONCILIATION" in l.status()["paused"]


def test_response_duplicate_does_not_double_tokens(budget):
    l, _ = budget; r = l.start_research(); complete(l, r, response_hash="a" * 64)
    complete(l, r, response_hash="a" * 64)
    assert l.status()["tokens"] == 100 and l.status()["paused"]


def test_duplicate_completion_rejected(budget):
    l, _ = budget; r = l.start_research(); c = complete(l, r)
    with pytest.raises(b.SafetyStop): l.finish_call(c, usage())
    assert l.status()["tokens"] == 100


def test_no_transport_cached_result_not_billed_as_new(budget):
    l, _ = budget; r = l.start_research(); c = l.begin_call(r)
    l.finish_call(c, None)
    assert l.status()["unknown_usage"] and l.status()["tokens"] == 0


def test_cross_midnight_conservative_days_campaign_once(budget):
    l, clock = budget; clock[0] += b.DAY - 1000
    r = l.start_research(); c = l.begin_call(r); w = l.begin_http(r, c, "MODEL")
    clock[0] += 2000
    l.finish_http(w, status=200); l.finish_call(c, usage())
    s = l.status(); assert s["tokens"] == 100
    assert [v["tokens"] for v in s["days"].values()] == [100, 100]


def test_three_starts_24h_and_no_extra_cutoff_or_failed_retry(budget):
    l, clock = budget
    for i in range(3):
        r = l.start_research(); complete(l, r); l.finish_research(r, 100, success=True)
        with pytest.raises(b.SafetyStop): l.start_research()
        clock[0] += b.DAY
    with pytest.raises(b.SafetyStop): l.start_research()
    assert len(l.status()["runs"]) == 3


def test_research_failure_consumes_ticket_and_stops(budget):
    l, _ = budget; r = l.start_research(); l.finish_research(r, None, success=False)
    assert len(l.status()["runs"]) == 1
    with pytest.raises(b.SafetyStop): l.start_research()


def test_process_crash_pending_stays_unknown(budget):
    l, _ = budget; r = l.start_research(); l.begin_call(r)
    other = b.Ledger(l.root, clock=l.clock)
    with pytest.raises(b.SafetyStop): other.begin_call(r)
    with pytest.raises(b.SafetyStop): other.start_research()


def test_concurrent_launch_one_ticket(budget):
    l, _ = budget
    def start(_):
        try: return l.start_research()
        except b.SafetyStop: return None
    with ThreadPoolExecutor(max_workers=6) as pool: results = list(pool.map(start, range(6)))
    assert sum(x is not None for x in results) == 1


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 502, 503, 504])
def test_http_fault_stops_next_dispatch(budget, status):
    l, _ = budget; r = l.start_research(); c = l.begin_call(r)
    w = l.begin_http(r, c, "MODEL"); l.finish_http(w, status=status)
    with pytest.raises(b.SafetyStop): l.begin_http(r, c, "MODEL")


def test_timeout(budget):
    l, clock = budget; r = l.start_research(); clock[0] += 900000
    with pytest.raises(b.SafetyStop, match="TASK_TIMEOUT"): l.begin_call(r)


def test_manual_pause_resume_keeps_counters(budget):
    l, _ = budget; r = l.start_research(); complete(l, r); l.finish_research(r, 100, success=True)
    l.pause(); assert l.status()["paused"] == ["MANUAL"]
    l.resume(); assert not l.status()["paused"] and l.status()["tokens"] == 100


def test_hash_tamper_or_reset_rejected(budget):
    l, _ = budget; raw = json.loads(l.path.read_text()); raw["approved"] = True
    l.path.write_text(json.dumps(raw))
    with pytest.raises(b.SafetyStop): l.status()
    with pytest.raises(b.SafetyStop): l.initialize(b.Policy())


def test_status_read_only(budget):
    l, _ = budget; before = {p.name: p.read_bytes() for p in l.root.iterdir()}
    l.status(); assert before == {p.name: p.read_bytes() for p in l.root.iterdir()}


def test_approval_default_off_and_safety_literals(tmp_path):
    with pytest.raises(b.SafetyStop): b.Ledger(tmp_path).initialize(b.Policy(), namespace="PRODUCTION", approved=True)
    for key in ("execution_enabled", "allow_new_risk", "simulation_admitted"):
        with pytest.raises(ValueError): b.Policy.model_validate({key: True})


def test_daily_call_budget_and_restart(tmp_path):
    l = b.Ledger(tmp_path); l.initialize(b.Policy(daily_calls=1))
    r = l.start_research(); complete(l, r)
    with pytest.raises(b.SafetyStop, match="CALL_BUDGET"): b.Ledger(tmp_path).begin_call(r)


def test_daily_http_counts_validation_requests(tmp_path):
    l = b.Ledger(tmp_path); l.initialize(b.Policy(daily_http=1))
    r = l.start_research(); w = l.begin_http(r, None, "PUBLIC_GET"); l.finish_http(w, status=200)
    with pytest.raises(b.SafetyStop, match="HTTP_BUDGET"): l.begin_http(r, None, "PUBLIC_GET")
    assert l.status()["http"] == 1


def test_post_response_overshoot_is_recorded_and_stops_not_hard_cap(tmp_path):
    l = b.Ledger(tmp_path); l.initialize(b.Policy(research_tokens=150, daily_tokens=150, campaign_tokens=150, headroom_tokens=10))
    r = l.start_research(); complete(l, r, 200)
    assert l.status()["tokens"] == 200 and "TOKEN_THRESHOLD_REACHED" in l.status()["paused"]
    assert l.status()["warnings"]
    with pytest.raises(b.SafetyStop): l.begin_call(r)


def test_headroom_blocks_before_soft_threshold(tmp_path):
    l = b.Ledger(tmp_path); l.initialize(b.Policy(research_tokens=150, headroom_tokens=60))
    r = l.start_research(); complete(l, r, 100)
    with pytest.raises(b.SafetyStop, match="TOKEN_HEADROOM"): l.begin_call(r)


def test_campaign_budget_preserved_cross_day(tmp_path):
    clock = [1792972800000]
    l = b.Ledger(tmp_path, clock=lambda: clock[0]); l.initialize(b.Policy(campaign_tokens=150, headroom_tokens=10))
    r = l.start_research(); complete(l, r); l.finish_research(r, 100, success=True)
    clock[0] += b.DAY
    r = l.start_research(); complete(l, r)
    assert l.status()["tokens"] == 200 and l.status()["paused"]


def test_clock_backwards_and_cannot_move_ledger_to_reset(budget, tmp_path):
    l, clock = budget; l.start_research(); clock[0] -= 1
    with pytest.raises(b.SafetyStop): l.pause()
    new = tmp_path / "moved"; new.mkdir(); (new / "journal.json").write_bytes(l.path.read_bytes())
    with pytest.raises(b.SafetyStop): b.Ledger(new).status()


def test_independent_processes_cannot_claim_two_researches(tmp_path):
    import subprocess
    import sys
    l = b.Ledger(tmp_path); l.initialize(b.Policy())
    script = "from tradingagents.safety_budget import Ledger,SafetyStop; import sys\ntry: print(Ledger(sys.argv[1]).start_research())\nexcept SafetyStop: print('BLOCKED')"
    procs = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    answers = [p.communicate(timeout=40)[0].strip() for p in procs]
    assert answers.count("BLOCKED") == 1 and len(l.status()["runs"]) == 1


def test_finished_research_cannot_receive_more_calls(budget):
    l, _ = budget; r = l.start_research(); complete(l, r); l.finish_research(r, 100, success=True)
    with pytest.raises(b.SafetyStop, match="RESEARCH_NOT_ACTIVE"): l.begin_call(r)


def test_resource_pause_needs_explicit_recheck(budget):
    l, _ = budget; l.resource_pause()
    with pytest.raises(b.SafetyStop): l.resume()
    l.resume(resource_rechecked=True)
    assert not l.status()["paused"] and not l.status()["runs"]


def test_late_start_cannot_extend_72h_campaign(budget):
    l, clock = budget; clock[0] += 72 * 3600000 - 899999
    with pytest.raises(b.SafetyStop, match="INSUFFICIENT_CAMPAIGN_WINDOW"): l.start_research()
    assert not l.status()["runs"]
