"""No live clients/models. Shadow is never connected to a research entry."""
import copy
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_okx_multitimeframe import CFG, DAY, NOW, SYMBOL, MultiClient
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv, okx_multitimeframe as mtf
from tradingagents import research_shadow as shadow

SHADOW_CFG = {**CFG, "okx_derivatives_enabled": True}


@pytest.fixture
def bundle(monkeypatch):
    monkeypatch.setattr(okx, "utc_ms", lambda: NOW)
    monkeypatch.setattr(deriv, "utc_ms", lambda: NOW)
    b = okx.build_bundle(SYMBOL, DAY, SHADOW_CFG, MultiClient())
    assert b["status"] == "VALID"
    return b


def assess(b, now=NOW, **kw):
    return shadow.evaluate_shadow(b, as_of_ms=now, expected_snapshot_id=kw.get("sid", b.get("snapshot_id")),
        expected_cutoff_ms=kw.get("cutoff", b.get("cutoff_ms")), mode="FIXTURE")


def reseal(b):
    for child in b["timeframes"].values():
        child["snapshot_id"] = okx.fingerprint(child)
    b["snapshot_id"] = okx.fingerprint(b)


def test_valid_and_duplicate_are_suggestions_not_authorizations(bundle):
    original = copy.deepcopy(bundle)
    one, two = assess(bundle), assess(bundle)
    assert one == two and bundle == original
    assert one.decision == "RESEARCH_SUGGESTED"
    assert set(one.checks.values()) == {"PASS"}
    assert not one.research_authorized and not one.execution_enabled
    assert not one.allow_new_risk and not one.simulation_admitted
    assert one.model_calls == one.http_calls == 0
    assert set(one.coverage) == set(mtf.PERIODS)


def test_rule_identity_immutable_and_drift_refused(bundle, monkeypatch):
    with pytest.raises(TypeError): shadow.RULES["freshness_ms"] = 600000
    monkeypatch.setattr(shadow, "RULES", {**shadow.RULES, "freshness_ms": 600000})
    r = assess(bundle)
    assert r.decision == "UNKNOWN" and r.reason_codes == ["RULE_CONFIG_DRIFT"]


@pytest.mark.parametrize("directions", [dict.fromkeys(mtf.PERIODS, 0),
    {"1h": 1, "4h": -1, "1d": 0}, dict.fromkeys(mtf.PERIODS, -1)])
def test_mixed_conflict_flat_and_direction_never_defer(bundle, directions):
    b = okx.build_bundle(SYMBOL, DAY, SHADOW_CFG, MultiClient(directions=directions))
    assert assess(b).decision == "RESEARCH_SUGGESTED"


@pytest.mark.parametrize("status,expected", [("STALE_DATA", "DATA_BLOCKED"),
    ("RESEARCH_INVALID", "DATA_BLOCKED"), ("PARTIAL", "UNKNOWN"), ("DATA_UNAVAILABLE", "UNKNOWN"),
    ("NOT_CHECKED", "UNKNOWN"), (None, "UNKNOWN")])
def test_quality_is_not_no_opportunity(bundle, status, expected):
    bundle["status"] = status
    assert assess(bundle).decision == expected


@pytest.mark.parametrize("identity", ["id", "symbol", "cutoff", "child", "instrument"])
def test_identity_drift_rejected(bundle, identity):
    if identity == "id": r = assess(bundle, sid="a" * 64)
    elif identity == "cutoff": r = assess(bundle, cutoff=NOW - 1)
    else:
        if identity == "symbol": bundle["symbol"] = "ETH-USDT-SWAP"
        elif identity == "child": bundle["timeframes"]["1h"]["cutoff_ms"] -= 1
        else: bundle["instrument"]["ctVal"] = "999"
        reseal(bundle)
        r = assess(bundle)
    assert r.decision == "UNKNOWN" and "FAIL" in r.checks.values()


@pytest.mark.parametrize("what", ["candle", "oi", "receipt", "server", "date", "summary", "outcome", "nested_outcome"])
def test_no_future_or_research_conclusion(bundle, what):
    b = bundle
    if what == "candle":
        b["timeframes"]["1h"]["records"][-1]["ts"] = NOW
    elif what == "oi":
        b["derivatives"]["components"]["oi_current"]["data"]["ts"] = NOW + 1
    elif what == "receipt": b["timeframes"]["1d"]["fetched_at_ms"] = NOW + 1
    elif what == "server":
        b["server_ms"] = NOW + 1
        for child in b["timeframes"].values(): child["server_ms"] = NOW + 1
    elif what == "date": b["timeframes"]["1h"]["records"][0]["Date"] = "2099-01-01T00:00:00+00:00"
    elif what == "summary": b["timeframe_analysis"]["1h"]["indicators"]["rsi"] += 1
    elif what == "outcome": b["research_result"] = {"proposed_action": "PRIVATE_SENTINEL"}
    else: b["timeframes"]["1h"]["final_trade_decision"] = "PRIVATE_SENTINEL"
    reseal(b)  # Independent checks must reject even with recomputed hashes.
    r = assess(b)
    assert r.decision == "UNKNOWN"
    expected = {"candle": "FUTURE_CANDLE", "oi": "FUTURE_OBSERVATION",
        "receipt": "RECEIPT_NOT_KNOWN_AT_ASOF", "server": "SERVER_TIME_NOT_KNOWN",
        "date": "ROW_TIME_MISMATCH", "summary": "INDICATOR_SUMMARY_MISMATCH",
        "outcome": "INPUT_NOT_MARKET_SNAPSHOT", "nested_outcome": "FORBIDDEN_RESEARCH_INPUT"}
    assert r.reason_codes == [expected[what]]
    assert "PRIVATE_SENTINEL" not in r.model_dump_json()


@pytest.mark.parametrize("age", [0, 299999, 300000, 300001])
def test_native_300_second_boundary_not_extended(bundle, age):
    # The fixture's funding receipt/as_of is slightly before NOW; use all live
    # components' own known expiry rather than pretending all began at NOW.
    earliest = min(p["expires_at_ms"] for n, p in bundle["derivatives"]["components"].items() if n in shadow.LIVE)
    at = earliest - 300000 + age
    r = assess(bundle, now=max(NOW, at))
    if age <= 300000: assert r.decision == "RESEARCH_SUGGESTED"
    else: assert r.decision == "DATA_BLOCKED"


@pytest.mark.parametrize("missing", ["derivatives", "period", "indicator", "oi_data"])
def test_missing_evidence_never_defer(bundle, missing):
    if missing == "derivatives": del bundle["derivatives"]
    elif missing == "period": del bundle["timeframes"]["1d"]
    elif missing == "indicator": del bundle["timeframe_analysis"]["1h"]["indicators"]["rsi"]
    else: del bundle["derivatives"]["components"]["oi_current"]["data"]
    reseal(bundle)
    assert assess(bundle).decision == "UNKNOWN"


def test_malformed_hash_ohlc_and_lag(bundle):
    bundle["snapshot_id"] = "0" * 64
    assert assess(bundle).decision == "UNKNOWN"
    b = copy.deepcopy(bundle)
    b["timeframes"]["1h"]["records"][-1]["High"] = -1
    reseal(b)
    assert assess(b).decision == "DATA_BLOCKED"


@pytest.mark.parametrize("clock,mode", [(True, "FIXTURE"), (-1, "FIXTURE"), (NOW, "PRODUCTION")])
def test_invalid_clock_or_production_mode_rejected(bundle, clock, mode):
    with pytest.raises(ValueError, match="CLOCK_OR_MODE"):
        shadow.evaluate_shadow(bundle, as_of_ms=clock, expected_snapshot_id=bundle["snapshot_id"],
                               expected_cutoff_ms=NOW, mode=mode)


def test_audit_private_bounded_repeated_event_identity(bundle):
    r = assess(bundle)
    with tempfile.TemporaryDirectory(prefix="market-shadow-FIXTURE-") as directory:
        a = shadow.record_shadow(r, directory); b = shadow.record_shadow(r, directory)
        path = Path(directory) / "shadow.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert a == b == rows[0]["event_id"] == rows[1]["event_id"]
        assert len(rows) == 2 and rows[0] == rows[1]
        assert all(x["event_id"] == shadow._digest(x["result"]) for x in rows)
        tampered = copy.deepcopy(rows[0]); tampered["result"]["as_of_ms"] += 1
        assert tampered["event_id"] != shadow._digest(tampered["result"])
        assert all(x["kind"] == "OFFLINE_RESEARCH_SHADOW" for x in rows)
        assert "entry_reference" not in path.read_text() and "stop_loss" not in path.read_text()
        if os.name != "nt": assert path.stat().st_mode & 0o777 == 0o600
        assert Path(str(path) + ".lock").is_file()
        with patch.object(shadow, "append_audit", side_effect=RuntimeError("MARKET_DIAGNOSTIC_CAPACITY")) as sink:
            with pytest.raises(RuntimeError, match="CAPACITY"): shadow.record_shadow(r, directory)
            sink.assert_called_once()


def test_not_a_general_audit_writer(bundle, tmp_path):
    r = assess(bundle)
    with pytest.raises(ValueError, match="TEST_AUDIT_DIRECTORY"):
        shadow.record_shadow(r, tmp_path)
    assert not list(tmp_path.iterdir())
    bad = r.model_copy(update={"reason_codes": ["PRIVATE_SENTINEL"]})
    with tempfile.TemporaryDirectory(prefix="market-shadow-FIXTURE-") as directory:
        with pytest.raises(ValueError, match="METADATA_INVALID"): shadow.record_shadow(bad, directory)
        assert not list(Path(directory).iterdir())


def test_no_production_entry_imports_or_calls(bundle, monkeypatch):
    import ast
    source = Path(shadow.__file__).read_text()
    imports = [n.module for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)]
    assert not any(any(t in (name or "") for t in ("guarded_research", "auto_research", "cli", "llm_clients")) for name in imports)
    monkeypatch.setattr(okx.OKXPublicClient, "__init__", lambda *a, **k: pytest.fail("client construction"))
    assess(bundle)


def test_new_hour_requires_new_closed_bar_even_inside_ttl(monkeypatch):
    late = NOW // 3600000 * 3600000 + 3599000
    monkeypatch.setattr(okx, "utc_ms", lambda: late)
    monkeypatch.setattr(deriv, "utc_ms", lambda: late)
    client = MultiClient(); original = client.get
    client.get = lambda path, params=None: [{"ts": str(late)}] if path == "/api/v5/public/time" else original(path, params)
    b = okx.build_bundle(SYMBOL, DAY, SHADOW_CFG, client)
    r = assess(b, now=late + 1000)
    assert r.decision == "DATA_BLOCKED"
    assert r.reason_codes == ["NEWER_CLOSED_CANDLE_REQUIRED"]


@pytest.mark.parametrize("field,value", [("rule_fingerprint", "0" * 64), ("snapshot_id", "PRIVATE_SENTINEL"),
    ("checks", {}), ("deterministic_feature_hashes", {}), ("research_authorized", True)])
def test_forged_output_refused_before_audit(bundle, field, value):
    bad = assess(bundle).model_copy(update={field: value})
    with tempfile.TemporaryDirectory(prefix="market-shadow-FIXTURE-") as directory:
        with pytest.raises(ValueError): shadow.record_shadow(bad, directory)
        assert not list(Path(directory).iterdir())


def test_reserved_defer_not_fabricated(bundle):
    bad = assess(bundle).model_copy(update={"decision": "DEFER_SUGGESTED"})
    with tempfile.TemporaryDirectory(prefix="market-shadow-FIXTURE-") as directory:
        with pytest.raises(ValueError, match="DEFER_RULE_DISABLED"): shadow.record_shadow(bad, directory)
        assert not list(Path(directory).iterdir())


def test_symlink_audit_directory_rejected(bundle):
    if os.name == "nt":
        pytest.skip("Requires POSIX symlink creation; not executed on Windows")
    with tempfile.TemporaryDirectory(prefix="market-shadow-FIXTURE-") as directory:
        target = Path(directory) / "real"; target.mkdir()
        link = Path(directory) / "market-shadow-FIXTURE-link"; link.symlink_to(target, target_is_directory=True)
        with pytest.raises(ValueError, match="TEST_AUDIT_DIRECTORY"): shadow.record_shadow(assess(bundle), link)
        assert not list(target.iterdir())
