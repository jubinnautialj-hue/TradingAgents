"""Conservative Market evidence identity. No network, model or archived writes."""
import copy

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tradingagents.agents.analysts.market_display import _argument_identity, project_history
from tradingagents.agents.analysts.turn import _as_text
from tests.test_market_display import IDENTITY_HEADER

BODY = IDENTITY_HEADER + "STALE_DATA; UNAVAILABLE; full frozen range evidence\n" * 70


def pair(args1=None, args2=None, **second):
    args = {"symbol": "BTC-USDT-SWAP", "start_date": "2026-10-01", "end_date": "2026-10-28"}
    a = args if args1 is None else args1
    b = args if args2 is None else args2
    return [AIMessage(content="", tool_calls=[{"name": "get_stock_data", "args": a, "id": "a"}]),
            ToolMessage(content=BODY, name="get_stock_data", tool_call_id="a"),
            AIMessage(content="", tool_calls=[{"name": "get_stock_data", "args": b, "id": "b"}]),
            ToolMessage(content=BODY, name="get_stock_data", tool_call_id="b", **second)]


class Uncomparable:
    def __bool__(self):
        raise AssertionError("Artifact must not be inspected")

    def __eq__(self, other):
        raise AssertionError("Artifact must not be compared")

    def __repr__(self):
        raise AssertionError("Artifact must not be serialised")


@pytest.mark.parametrize("artifact", [{"source": "B"}, {"source": "A"}, {}, [], "opaque", Uncomparable()])
def test_any_non_none_artifact_conservatively_excluded(artifact):
    messages = pair(artifact=artifact)
    messages[1] = messages[1].model_copy(update={"artifact": {"source": "A"}})
    result = project_history(messages)
    assert result[1] is messages[1] and result[3] is messages[3]
    assert result[3].content == BODY


def test_none_artifact_and_confirmed_identical_call_identity_can_fold():
    messages = pair(artifact=None)
    before = copy.deepcopy(messages)
    result = project_history(messages)
    assert messages == before
    assert result[1] is messages[1] and len(result[3].content) < len(BODY)
    assert "tool_call_id=a" in result[3].content and "args_sha256=" in result[3].content
    assert "market-tool-identity-v1" in result[3].content
    assert result[3].tool_call_id == "b"
    assert _as_text(result)[2].content == _as_text(messages)[2].content
    assert project_history(result) == result


@pytest.mark.parametrize("key,value", [
    ("start_date", "2026-09-01"), ("end_date", "2026-10-27"),
    ("symbol", "ETH-USDT-SWAP"), ("source", "another-origin"),
    ("snapshot_id", "different-snapshot"), ("interval", "4h"),
])
def test_different_parameters_even_identical_body_not_folded(key, value):
    args = {"symbol": "BTC-USDT-SWAP", "start_date": "2026-10-01", "end_date": "2026-10-28", key: value}
    messages = pair(args2=args)
    assert project_history(messages) == messages


@pytest.mark.parametrize("field", ["additional_kwargs", "response_metadata"])
def test_metadata_only_source_difference_not_folded(field):
    messages = pair(**{field: {"source": "different"}})
    assert project_history(messages) == messages


@pytest.mark.parametrize("fragment", ["source=OTHER", "symbol=ETH", "snapshot=other", "range=other"])
def test_visible_identity_change_not_folded(fragment):
    messages = pair()
    messages[-1] = messages[-1].model_copy(update={"content": BODY + fragment})
    assert project_history(messages) == messages


def test_missing_call_association_not_folded():
    messages = pair()
    assert project_history([messages[1], messages[3]]) == [messages[1], messages[3]]


def test_forward_reference_not_confirmed():
    messages = pair()
    reordered = [messages[1], messages[0], messages[3], messages[2]]
    assert project_history(reordered) == reordered


def test_duplicate_call_id_is_ambiguous():
    messages = pair()
    duplicate = AIMessage(content="", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "b"}])
    assert project_history([*messages, duplicate]) == [*messages, duplicate]


def test_duplicate_result_id_is_ambiguous():
    messages = pair()
    repeated = messages[-1].model_copy()
    assert project_history([*messages, repeated]) == [*messages, repeated]


def test_name_call_mismatch_not_folded():
    messages = pair()
    messages[2] = AIMessage(content="", tool_calls=[{"name": "get_indicators", "args": {}, "id": "b"}])
    assert project_history(messages) == messages


@pytest.mark.parametrize("args", [{"x": float("nan")}, {"x": float("inf")}, {"x": {1, 2}}, {1: "key"}, {"x": object()}, []])
def test_unconfirmed_json_arguments_rejected(args):
    assert _argument_identity(args) is None


def test_canonical_mapping_order_same_identity():
    messages = pair(args1={"a": 1, "b": [False, None]}, args2={"b": [False, None], "a": 1})
    assert project_history(messages)[-1].content != BODY


def test_error_results_not_folded():
    messages = pair(status="error")
    assert project_history(messages) == messages


@pytest.mark.parametrize("field", ["source", "exchange", "symbol", "interval", "snapshot_id",
                                   "fetched_at_ms", "cutoff_ms", "latest_close_ms"])
def test_missing_visible_identity_not_folded(field):
    messages = pair()
    text = "\n".join(line for line in BODY.splitlines() if not line.startswith(f"- {field}: "))
    messages[1] = messages[1].model_copy(update={"content": text})
    messages[3] = messages[3].model_copy(update={"content": text})
    assert project_history(messages) == messages
