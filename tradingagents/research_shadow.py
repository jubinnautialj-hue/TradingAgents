"""Offline-only shadow assessment. No clients, research routing or CLI entry."""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import Field, model_validator

from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents.dataflows.audit_file import append_audit
from tradingagents.dataflows.vendors import okx, okx_derivatives as deriv, okx_multitimeframe as mtf

VERSION = "research-preshadow-v1"
RULES = MappingProxyType({"version": VERSION, "scope": "OFFLINE_CORE_AND_BOUND_DERIVATIVES_NOT_FULL_RESEARCH",
         "periods": mtf.PERIODS, "instrument": "BTC-USDT-SWAP",
         "freshness_ms": 300000, "defer_rule": "DISABLED_NO_VALIDATED_NECESSARY_CONDITION",
         "valid_result": "RESEARCH_SUGGESTED", "decisions_are_authorizations": False})
ROOT_FIELDS = frozenset({"multi_timeframe", "source", "exchange", "symbol", "interval", "status",
    "allow_new_risk", "execution_enabled", "issues", "timeframes", "cutoff_ms", "server_ms",
    "structure_rules", "fetched_at_ms", "records", "instrument", "latest_close_ms", "volume_units",
    "instrument_metadata_scope", "historical", "timeframe_analysis", "confluence", "derivatives",
    "snapshot_id", "error_context"})
LIVE = ("oi_current", "funding_current", "mark_current", "index_current", "trade_current", "orderbook")
HISTORY = ("oi_history", "funding_history", "mark_history", "index_history")
CHECKS = ("input", "identity", "core_quality", "point_in_time", "deterministic_indicators", "derivatives")
REASONS = frozenset("INPUT_NOT_MARKET_SNAPSHOT INPUT_TOO_LARGE EXPECTED_IDENTITY_INVALID SNAPSHOT_IDENTITY_MISMATCH "
    "SNAPSHOT_FINGERPRINT_MISMATCH REQUIRED_PERIODS_MISSING ANCHOR_INTERVAL_INVALID CHILD_IDENTITY_MISMATCH "
    "CANDLE_COVERAGE_INVALID FUTURE_CANDLE CANDLE_SEQUENCE_INVALID CLOSE_TIME_MISMATCH SERVER_TIME_NOT_KNOWN "
    "RECEIPT_NOT_KNOWN_AT_ASOF SHADOW_EVIDENCE_EXPIRED ANCHOR_MISMATCH STRUCTURE_RULES_INVALID "
    "INDICATOR_SUMMARY_MISMATCH DERIVATIVE_IDENTITY_OR_COVERAGE_MISSING DERIVATIVE_COMPONENTS_MISSING "
    "DERIVATIVE_INSTRUMENT_MISMATCH DERIVATIVE_DATA_MISSING EXPLICIT_INVALID_OR_STALE "
    "QUALITY_UNKNOWN_OR_INCOMPLETE FUTURE_OBSERVATION FORBIDDEN_RESEARCH_INPUT ROW_TIME_MISMATCH "
    "VALID_POINT_IN_TIME_EVIDENCE_NO_SAFE_DEFER_RULE MALFORMED_OR_NATIVE_VALIDATION_FAILED "
    "NATIVE_QUALITY_REJECTED NEWER_CLOSED_CANDLE_REQUIRED RULE_CONFIG_DRIFT".split())


class Coverage(ResearchModel):
    first_open_ms: int = Field(gt=0)
    last_close_ms: int = Field(gt=0)
    rows: int = Field(ge=250, le=400)
    receipt_ms: int = Field(gt=0)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


RULE_HASH = _digest(dict(RULES))


class ShadowResult(ResearchModel):
    version: Literal["research-preshadow-v1"] = VERSION
    rule_fingerprint: str = RULE_HASH
    mode: Literal["FIXTURE", "REPLAY"]
    decision: Literal["RESEARCH_SUGGESTED", "DEFER_SUGGESTED", "DATA_BLOCKED", "UNKNOWN"]
    as_of_ms: int = Field(gt=0)
    cutoff_ms: int | None
    instrument: Literal["BTC-USDT-SWAP"] = "BTC-USDT-SWAP"
    snapshot_id: str | None
    evidence_fingerprint: str | None
    reason_codes: list[str]
    checks: dict[str, Literal["PASS", "FAIL", "NOT_CHECKED"]]
    coverage: dict[str, Coverage] = Field(default_factory=dict)
    deterministic_feature_hashes: dict[str, str] = Field(default_factory=dict)
    shadow_only: Literal[True] = True
    research_authorized: Literal[False] = False
    model_calls: Literal[0] = 0
    http_calls: Literal[0] = 0
    execution_enabled: Literal[False] = False
    allow_new_risk: Literal[False] = False
    simulation_admitted: Literal[False] = False

    @model_validator(mode="after")
    def integrity(self):
        if self.rule_fingerprint != RULE_HASH or set(self.checks) != set(CHECKS) \
                or not self.reason_codes or not set(self.reason_codes) <= REASONS:
            raise ValueError("SHADOW_RESULT_METADATA_INVALID")
        if not set(self.coverage) <= set(mtf.PERIODS) or not set(self.deterministic_feature_hashes) <= set(mtf.PERIODS) \
                or any(not _hex(h) for h in self.deterministic_feature_hashes.values()) \
                or any(h is not None and not _hex(h) for h in (self.snapshot_id, self.evidence_fingerprint)):
            raise ValueError("SHADOW_RESULT_FEATURES_INVALID")
        if self.decision == "RESEARCH_SUGGESTED" and (set(self.checks.values()) != {"PASS"}
                or set(self.coverage) != set(mtf.PERIODS) or self.cutoff_ms is None
                or set(self.deterministic_feature_hashes) != set(mtf.PERIODS)
                or self.cutoff_ms > self.as_of_ms or not _hex(self.evidence_fingerprint)
                or self.reason_codes != ["VALID_POINT_IN_TIME_EVIDENCE_NO_SAFE_DEFER_RULE"]):
            raise ValueError("SHADOW_RESULT_NOT_COMPLETE")
        if self.decision in {"UNKNOWN", "DATA_BLOCKED"} and "FAIL" not in self.checks.values():
            raise ValueError("SHADOW_RESULT_FAILURE_NOT_EXPLAINED")
        return self


class _Rejected(Exception):
    def __init__(self, code, decision="UNKNOWN"):
        self.code, self.decision = code, decision


def _require(condition, code, decision="UNKNOWN"):
    if not condition:
        raise _Rejected(code, decision)


def _ms(value):
    return type(value) is int and value > 0


def _hex(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _quality(status):
    if status in {"RESEARCH_INVALID", "STALE_DATA"}:
        raise _Rejected("EXPLICIT_INVALID_OR_STALE", "DATA_BLOCKED")
    _require(status == "VALID", "QUALITY_UNKNOWN_OR_INCOMPLETE")


def _received(value, as_of):
    _require(_ms(value) and value <= as_of, "RECEIPT_NOT_KNOWN_AT_ASOF")
    _require(as_of - value <= RULES["freshness_ms"], "SHADOW_EVIDENCE_EXPIRED", "DATA_BLOCKED")


def _observed_times(value, cutoff):
    # Forecast settlement/expiry times are not observation times. They are not
    # features or triggers here. Only actual observation/closed-bar times count.
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"ts", "as_of_ms", "confirmed_at_ms", "close_ms"} and item is not None:
                _require(_ms(item) and item <= cutoff, "FUTURE_OBSERVATION")
            else:
                _observed_times(item, cutoff)
    elif isinstance(value, list):
        for item in value:
            _observed_times(item, cutoff)


def _no_research_input(value):
    if isinstance(value, dict):
        _require(not set(value).intersection({"research_result", "final_trade_decision", "wait_case",
                 "model_proposed_action", "proposed_action", "entry_reference", "stop_loss", "take_profit"}),
                 "FORBIDDEN_RESEARCH_INPUT")
        for item in value.values():
            _no_research_input(item)
    elif isinstance(value, list):
        for item in value:
            _no_research_input(item)


def evaluate_shadow(snapshot, *, as_of_ms, expected_snapshot_id, expected_cutoff_ms,
                    mode: Literal["FIXTURE", "REPLAY"]):
    """Pure assessment of supplied frozen data; caller pins the replay clock.

    cutoff bounds market observations; as_of bounds client receipts. Replaying
    at a past clock is a counterfactual offline check, never a forward record.
    No previous research result, plan, WAIT reason or future bar is accepted.
    """
    if not _ms(as_of_ms) or mode not in {"FIXTURE", "REPLAY"}:
        raise ValueError("SHADOW_CLOCK_OR_MODE_INVALID")
    checks = dict.fromkeys(CHECKS, "NOT_CHECKED")
    coverage, features, evidence_hash, stage = {}, {}, None, "input"
    cutoff = expected_cutoff_ms if _ms(expected_cutoff_ms) else None
    sid = expected_snapshot_id if _hex(expected_snapshot_id) else None
    decision, reasons = "UNKNOWN", []
    try:
        _require(_digest(dict(RULES)) == RULE_HASH, "RULE_CONFIG_DRIFT")
        _require(type(snapshot) is dict and not (set(snapshot) - ROOT_FIELDS), "INPUT_NOT_MARKET_SNAPSHOT")
        encoded = json.dumps(snapshot, allow_nan=False)
        _require(len(encoded.encode()) <= 4 * 1024**2, "INPUT_TOO_LARGE")
        _no_research_input(snapshot)
        evidence_hash = _digest(snapshot)
        checks[stage] = "PASS"
        stage = "identity"
        _require(cutoff is not None and sid is not None and cutoff <= as_of_ms, "EXPECTED_IDENTITY_INVALID")
        _require(snapshot.get("symbol") == "BTC-USDT-SWAP" and snapshot.get("exchange") == "OKX"
                 and snapshot.get("source") == "OKX API V5 public REST"
                 and snapshot.get("cutoff_ms") == cutoff and snapshot.get("snapshot_id") == sid
                 and snapshot.get("multi_timeframe") is True, "SNAPSHOT_IDENTITY_MISMATCH")
        _require(okx.fingerprint(snapshot) == sid, "SNAPSHOT_FINGERPRINT_MISMATCH")
        _require(set(snapshot["timeframes"]) == set(mtf.PERIODS), "REQUIRED_PERIODS_MISSING")
        okx.validate_instrument(snapshot["instrument"], "BTC-USDT-SWAP")
        checks[stage] = "PASS"
        stage = "core_quality"
        _quality(snapshot.get("status"))
        _require(snapshot["interval"] in mtf.PERIODS, "ANCHOR_INTERVAL_INVALID")
        for period, child in snapshot["timeframes"].items():
            _quality(child.get("status"))
            _require(child["symbol"] == snapshot["symbol"] and child["instrument"] == snapshot["instrument"]
                     and child["interval"] == period and child["cutoff_ms"] == cutoff
                     and child["server_ms"] == snapshot["server_ms"]
                     and okx.fingerprint(child) == child["snapshot_id"], "CHILD_IDENTITY_MISMATCH")
            okx.validate_instrument(child["instrument"], child["symbol"])
            rows = child["records"]
            _require(type(rows) is list and 250 <= len(rows) <= 400, "CANDLE_COVERAGE_INVALID", "DATA_BLOCKED")
            step = okx.BARS[period][1]
            _require(all(_ms(r["ts"]) and r["ts"] + step <= cutoff for r in rows), "FUTURE_CANDLE")
            _require(all(isinstance(r.get("Date"), str) and r["Date"].endswith(("+00:00", "Z"))
                         and int(datetime.fromisoformat(r["Date"]).timestamp() * 1000) == r["ts"]
                         for r in rows), "ROW_TIME_MISMATCH")
            _require(all(b["ts"] - a["ts"] == step for a, b in zip(rows, rows[1:])), "CANDLE_SEQUENCE_INVALID", "DATA_BLOCKED")
            raw = [[r[k] for k in ("ts", "Open", "High", "Low", "Close", "VolumeContracts", "Volume", "VolumeQuote")] + ["1"] for r in rows]
            okx.validate_candles(raw, period, cutoff, 250, 400, 0)
            _require(child["latest_close_ms"] == rows[-1]["ts"] + step, "CLOSE_TIME_MISMATCH")
            coverage[period] = {"first_open_ms": rows[0]["ts"], "last_close_ms": child["latest_close_ms"],
                                "rows": len(rows), "receipt_ms": child["fetched_at_ms"]}
        checks[stage] = "PASS"
        stage = "point_in_time"
        _require(_ms(snapshot["server_ms"]) and cutoff <= snapshot["server_ms"] <= as_of_ms, "SERVER_TIME_NOT_KNOWN")
        for child in [snapshot, *snapshot["timeframes"].values()]:
            _received(child["fetched_at_ms"], as_of_ms)
            _require(type(child["historical"]) is bool, "SNAPSHOT_IDENTITY_MISMATCH")
            if not child["historical"]:
                step = okx.BARS[child["interval"]][1]
                _require(as_of_ms // step * step <= child["latest_close_ms"],
                         "NEWER_CLOSED_CANDLE_REQUIRED", "DATA_BLOCKED")
        anchor = snapshot["timeframes"][snapshot["interval"]]
        _require(snapshot["records"] == anchor["records"] and snapshot["latest_close_ms"] == anchor["latest_close_ms"], "ANCHOR_MISMATCH")
        checks[stage] = "PASS"
        stage = "deterministic_indicators"
        _require(snapshot["structure_rules"] == mtf.structure_rules({
            "okx_pivot_window": snapshot["structure_rules"]["pivot_window"],
            "okx_structure_window": snapshot["structure_rules"]["range_window"],
            "okx_retest_atr_fraction": snapshot["structure_rules"]["retest_atr_fraction"]}), "STRUCTURE_RULES_INVALID")
        analyses = {}
        for period, child in snapshot["timeframes"].items():
            actual = mtf.analyze_timeframe(child, snapshot["structure_rules"])
            _observed_times(actual, cutoff)
            _require(actual == snapshot["timeframe_analysis"][period], "INDICATOR_SUMMARY_MISMATCH")
            features[period] = _digest(actual)
            analyses[period] = actual
        _require(mtf.confluence(analyses) == snapshot["confluence"], "INDICATOR_SUMMARY_MISMATCH")
        checks[stage] = "PASS"
        stage = "derivatives"
        evidence = snapshot.get("derivatives")
        _require(type(evidence) is dict and evidence["instrument"] == "BTC-USDT-SWAP"
                 and evidence["research_cutoff_ms"] == cutoff, "DERIVATIVE_IDENTITY_OR_COVERAGE_MISSING")
        parts = evidence["components"]
        _require(set(parts) == set(LIVE + HISTORY), "DERIVATIVE_COMPONENTS_MISSING")
        _observed_times(evidence, cutoff)
        view = deriv.evidence_view(evidence, now=as_of_ms)
        for name, part in parts.items():
            _received(part["fetched_at_ms"], as_of_ms)
            expected = "BTC-USDT" if name in {"index_current", "index_history"} else "BTC-USDT-SWAP"
            _require(part["instrument"] == expected, "DERIVATIVE_INSTRUMENT_MISMATCH")
            _quality(view["components"][name]["status"])
            _require(part.get("data") is not None and _ms(part.get("as_of_ms")), "DERIVATIVE_DATA_MISSING")
            if name in LIVE:
                _require(as_of_ms - part["as_of_ms"] <= RULES["freshness_ms"], "SHADOW_EVIDENCE_EXPIRED", "DATA_BLOCKED")
        checks[stage] = "PASS"
        decision, reasons = "RESEARCH_SUGGESTED", ["VALID_POINT_IN_TIME_EVIDENCE_NO_SAFE_DEFER_RULE"]
    except _Rejected as exc:
        checks[stage] = "FAIL"
        decision, reasons = exc.decision, [exc.code]
    except okx.DataQualityError:
        checks[stage] = "FAIL"
        decision, reasons = "DATA_BLOCKED", ["NATIVE_QUALITY_REJECTED"]
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, AttributeError, RecursionError):
        checks[stage] = "FAIL"
        reasons = ["MALFORMED_OR_NATIVE_VALIDATION_FAILED"]
    return ShadowResult(mode=mode, decision=decision, as_of_ms=as_of_ms, cutoff_ms=cutoff,
        snapshot_id=sid, evidence_fingerprint=evidence_hash, reason_codes=reasons,
        checks=checks, coverage=coverage, deterministic_feature_hashes=features)


def record_shadow(result: ShadowResult, directory):
    """Only a dedicated temp FIXTURE/REPLAY sink, never a research audit path.

    Repeated inputs deliberately append the same content-addressed event ID;
    they are not new opportunities, research authorizations or extra samples.
    """
    result = ShadowResult.model_validate(result.model_dump())
    _require(result.rule_fingerprint == RULE_HASH, "UNKNOWN_SHADOW_RULE")
    if result.decision == "DEFER_SUGGESTED":
        raise ValueError("SHADOW_V1_DEFER_RULE_DISABLED")
    root = Path(directory).absolute()
    temp = Path(tempfile.gettempdir()).resolve()
    if root.is_symlink() or root.resolve() != root or not root.is_relative_to(temp) or root == temp \
            or not root.name.startswith("market-shadow-" + result.mode + "-"):
        raise ValueError("SHADOW_TEST_AUDIT_DIRECTORY_REQUIRED")
    payload = result.model_dump(mode="json")
    event = {"kind": "OFFLINE_RESEARCH_SHADOW", "event_id": _digest(payload), "result": payload}
    append_audit(root / "shadow.jsonl", (json.dumps(event, sort_keys=True, allow_nan=False) + "\n").encode(), 4194304)
    return event["event_id"]
