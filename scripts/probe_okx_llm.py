"""One short research-only model probe; no tools, retries or account access."""
import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from tradingagents.okx_agent_comparison import model_config
from tradingagents.okx_traded_comparison import immutable, safe_config
from tradingagents.llm_clients.factory import create_tier_client
from tradingagents.llm_clients.reliability import classify_failure
from tradingagents.dataflows.config import run_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output/"probe_started.json").exists():
        raise ValueError("Probe already attempted; no implicit retry")
    config = model_config()
    config["llm_max_retries"] = 0
    started = datetime.now(UTC).isoformat()
    immutable(args.output/"probe_started.json", {"started_at_utc": started,
              "model_config": safe_config(config), "execution_enabled": False})
    result = {"started_at_utc": started, "model_config": safe_config(config),
              "attempts": 1, "usage": None, "execution_enabled": False}
    begin = time.monotonic()
    try:
        with run_config(config):
            model = create_tier_client(config, "quick", timeout=45).get_llm()
            response = model.invoke("Reply with exactly OK. Do not use tools or provide any other text.")
        if not response.content or getattr(response, "tool_calls", None):
            raise ValueError("Unexpected probe response")
        result.update(status="COMPLETED", response=response.content,
                      usage=getattr(response, "usage_metadata", None),
                      completion_status=getattr(response, "response_metadata", {}).get("status"))
    except Exception as exc:
        failure = classify_failure(exc)
        result.update(status="FAILED", exception_type=type(exc).__name__,
                      failure=failure.summary() if failure else {"kind": "NON_TRANSPORT_ERROR"})
    result["elapsed_seconds"] = time.monotonic()-begin
    immutable(args.output/"probe_result.json", result)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
