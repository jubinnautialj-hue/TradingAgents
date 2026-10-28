"""Offline frozen-snapshot display A/B; no real prompts, model or registrations.

Usage: python scripts/market_display_ab.py --snapshot FILE --output METRICS.json
Nine rounds are a declared synthetic tool schedule, NOT recovered real calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import socket
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from tradingagents.agents import tools
from tradingagents.agents.analysts.market_analyst import TOOLS
from tradingagents.agents.analysts.market_display import project_history
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.vendors import okx, okx_derivatives


def digest(data):
    return hashlib.sha256(data).hexdigest()


def measure(text):
    raw = text.encode("utf-8")
    return {"characters": len(text), "utf8_bytes": len(raw), "sha256": digest(raw),
            "estimated_tokens_utf8_bytes_div4": math.ceil(len(raw) / 4),
            "actual_model_tokens": None}


def deny(*args, **kwargs):
    raise RuntimeError("Offline A/B prohibits all network/model connections")


def evaluate(bundle, history_projector=project_history):
    original = json.dumps(bundle, sort_keys=True).encode()
    day = datetime.fromtimestamp(bundle["cutoff_ms"] / 1000, UTC).date().isoformat()
    # Frozen-time replay ONLY. This does not assert this old data is valid now.
    config = {"market_mode": okx.MODE, "okx_multi_timeframe": bundle["multi_timeframe"],
              "okx_interval": bundle["interval"], "okx_history_bars": len(bundle["records"]),
              "okx_derivatives_enabled": bool(bundle.get("derivatives"))}
    stock = (bundle["symbol"], "2000-01-01", day, day, bundle)
    snapshot = (bundle["symbol"], day, 30, day, bundle)
    indicators = (bundle["symbol"], "rsi,macd,macds,macdh,atr,close_50_sma,close_200_sma,close_10_ema", day, 30, day, bundle)
    # Same frozen evidence and same calls A/B; distinct and duplicate results.
    schedule = [(0, stock), (2, snapshot), (1, indicators), (0, stock),
                (1, (bundle["symbol"], "rsi", day, 30, day, bundle)), (2, snapshot),
                (0, stock), (1, (bundle["symbol"], "macd", day, 30, day, bundle)), (2, snapshot)]
    old_tools = (tools.get_stock_data, tools.get_indicators, tools.get_verified_market_snapshot)
    histories = {"A": [], "B": []}
    output = []
    before_calcs = {p: okx.indicator_frame(c)[list(okx.INDICATORS)].to_csv(index=False)
                    for p, c in bundle["timeframes"].items()}
    with patch.object(socket.socket, "connect", deny), patch.object(socket, "getaddrinfo", deny), \
            patch.object(okx, "utc_ms", lambda: bundle["fetched_at_ms"]), \
            patch.object(okx_derivatives, "utc_ms", lambda: bundle["fetched_at_ms"]), run_config(config):
        for round_number, (index, args) in enumerate(schedule, 1):
            texts = {"A": old_tools[index].func(*args), "B": TOOLS[index].func(*args)}
            if index == 0 and "Supplemental full-input" not in texts["B"]:
                raise ValueError("Frozen replay did not pass original quality gate; no valid size comparison")
            entry = {"round": round_number, "tool": old_tools[index].name}
            for group in ("A", "B"):
                call_id = f"ab-{round_number}"
                # Match the actual exposed tool arguments, excluding injected state.
                call_args = ({"start_date": args[1], "end_date": args[2]} if index == 0 else
                             {"indicator": args[1], "curr_date": args[2], "look_back_days": args[3]} if index == 1 else
                             {"curr_date": args[1], "look_back_days": args[2]})
                histories[group] += [AIMessage(content="", tool_calls=[{"name": old_tools[index].name, "args": call_args, "id": call_id}]),
                                     ToolMessage(content=texts[group], name=old_tools[index].name, tool_call_id=call_id)]
                projected = history_projector(histories[group]) if group == "B" else histories[group]
                entry[group] = {"tool_output": measure(texts[group]),
                                "cumulative_tool_context": measure("\n".join(m.content for m in projected)),
                                "native_tool_call_ids": [m.tool_call_id for m in projected if isinstance(m, ToolMessage)]}
            assert entry["A"]["native_tool_call_ids"] == entry["B"]["native_tool_call_ids"]
            output.append(entry)
    assert original == json.dumps(bundle, sort_keys=True).encode()
    for period, child in bundle["timeframes"].items():
        assert before_calcs[period] == okx.indicator_frame(child)[list(okx.INDICATORS)].to_csv(index=False)
    return {"scope": "FROZEN_REPLAY_SYNTHETIC_TOOL_SCHEDULE_NOT_REAL_MODEL_USAGE",
            "snapshot_id": bundle["snapshot_id"], "snapshot_content_sha256": digest(original),
            "data_and_indicator_values_unchanged": True, "verified_snapshot_output_byte_identical": True,
            "network_calls": 0, "model_calls": 0, "formal_registrations": 0,
            "token_estimator": "ceil(UTF8 bytes / 4); heuristic only, NOT real tokenizer or SDK usage; numeric CSV may differ substantially",
            "rounds": output}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.snapshot.resolve():
        parser.error("output must not overwrite frozen snapshot")
    result = evaluate(json.loads(args.snapshot.read_text(encoding="utf-8-sig")))
    result["snapshot_file_sha256"] = digest(args.snapshot.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rounds"}, indent=2))
