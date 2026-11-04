"""Synthetic offline proof, NOT a replay of Probe-001/002 private payloads."""
import copy
import gc
import json

import pytest
from langchain_core.messages import AIMessage

from tests.test_market_safe_adapter import events, invoke
from tests.test_market_event_diagnostics import obj, last
from tests.test_guarded_research import audited
from tests.test_market_tool_contract import pinned
from tradingagents.agents.analysts.market_analyst import TOOLS
from tradingagents.agents.analysts.market_safe_adapter import scope, ObservedResponse
from tradingagents.llm_clients.reliability import LLMUnavailable


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # Finish earlier mock iterators before another test installs a failing sink.
    gc.collect()
    monkeypatch.setattr('socket.socket.connect', lambda *a, **k: pytest.fail('REAL_NETWORK_FORBIDDEN'))
    yield
    gc.collect()


def complete_events(n=1, mode='fragmented', empty=True, stock=False):
    rows, items = events(n, mode)
    if stock:
        args = json.dumps({'start_date': '2026-11-01', 'end_date': '2026-11-03'})
        for row in rows:
            if 'item' in row:
                row['item']['name'] = 'get_stock_data'
                if row['type'].endswith('.done'):
                    row['item']['arguments'] = args
            if row['type'].endswith('.delta'):
                row['delta'] = args  # Stock fixture uses one delta.
        items[0].update(name='get_stock_data', arguments=args)
        rows[-1]['response']['output'] = copy.deepcopy(items)
    expanded = []
    initial = copy.deepcopy(rows[-1]['response'])
    initial.update(output=[], status='in_progress', usage=None)
    expanded.append(dict(type='response.created', response=initial))
    for row in rows:
        if row['type'] == 'response.output_item.done':
            expanded.append(dict(type='response.function_call_arguments.done',
                output_index=row['output_index'], item_id=row['item']['id'], arguments=row['item']['arguments']))
        expanded.append(row)
    if empty:
        expanded[-1]['response']['output'] = []
    for i, row in enumerate(expanded):
        row['sequence_number'] = i
        row['response_id'] = 'resp_TEST_FIXTURE'
    return expanded, items


@pytest.mark.parametrize('n', [1, 6, 7])
@pytest.mark.parametrize('mode', ['fragmented', 'interleaved', 'sequential'])
def test_explicit_empty_and_nonempty_have_identical_calls_and_usage(n, mode, tmp_path, monkeypatch):
    path = tmp_path/'audit.jsonl';monkeypatch.setenv('TA_SINGLE_AUDIT_FILE', str(path))
    empty, items = complete_events(n, mode)
    full, _ = complete_events(n, mode, empty=False)
    a, _ = invoke(empty);b, _ = invoke(full)
    assert a.tool_calls == b.tool_calls == [dict(name=i['name'], args=json.loads(i['arguments']),
        id=i['call_id'], type='tool_call') for i in items]
    assert a.usage_metadata == b.usage_metadata
    records = [json.loads(t) for t in path.read_text().splitlines() if json.loads(t).get('code') == 'EVENTS_RECONCILED']
    assert len(records) == 1
    record = records[0]
    assert record['sdk_terminal_structure']['classification'] == 'EMPTY_ARRAY'
    assert record['admission_terminal_structure']['classification'] == 'EMPTY_ARRAY'
    assert record['counts']['terminal'] == 0  # Raw SDK evidence is never rewritten.
    assert record['reconciliation'] == dict(version='market-stream-reconciliation-v1',
        source='COMPLETED_EVENTS', tool_count=n, status='VERIFIED')
    assert '2026-11-03' not in path.read_text()


@pytest.mark.parametrize('fault', ['missing_added', 'missing_done', 'missing_args_done',
    'duplicate_added', 'duplicate_done', 'duplicate_args_done', 'duplicate_call_id', 'index_conflict',
    'delta_conflict', 'args_done_conflict', 'done_conflict', 'args_done_after_done', 'delta_after_args_done',
    'delta_before_added', 'sequence_repeated', 'sequence_reversed', 'sequence_missing',
    'other_response', 'unknown_event', 'missing_created', 'missing_completed', 'incomplete',
    'unknown_usage', 'bad_usage_sum', 'missing_field', 'null', 'unknown_indicator', 'duplicate_json_key',
    'unknown_parameter', 'done_not_completed', 'index_gap', 'orphan_args', 'late_created'])
def test_empty_terminal_insufficient_proof_never_reaches_tools(fault):
    rows, _ = complete_events(2 if fault in {'duplicate_call_id', 'index_conflict'} else 1)
    find = lambda kind: next(row for row in rows if row['type'] == kind)
    if fault in {'missing_added', 'missing_done', 'missing_args_done'}:
        suffix = {'missing_added': 'response.output_item.added', 'missing_done': 'response.output_item.done',
                  'missing_args_done': 'response.function_call_arguments.done'}[fault]
        rows = [row for row in rows if row['type'] != suffix]
    elif fault in {'duplicate_added', 'duplicate_done', 'duplicate_args_done'}:
        kind = {'duplicate_added': 'response.output_item.added', 'duplicate_done': 'response.output_item.done',
                'duplicate_args_done': 'response.function_call_arguments.done'}[fault]
        row = find(kind);rows.insert(rows.index(row)+1, copy.deepcopy(row))
    elif fault == 'duplicate_call_id':
        for row in rows:
            if row.get('item', {}).get('id') == 'fc_1':row['item']['call_id'] = 'call_0'
    elif fault == 'index_conflict':
        next(row for row in rows if row.get('item', {}).get('id') == 'fc_1')['output_index'] = 0
    elif fault == 'delta_conflict':find('response.function_call_arguments.delta')['delta'] = '{}'
    elif fault == 'args_done_conflict':find('response.function_call_arguments.done')['arguments'] = '{}'
    elif fault == 'done_conflict':find('response.output_item.done')['item']['arguments'] = '{}'
    elif fault == 'args_done_after_done':
        row = find('response.function_call_arguments.done');rows.remove(row);rows.insert(-1, row)
    elif fault == 'delta_after_args_done':
        row = find('response.function_call_arguments.delta');rows.remove(row);rows.insert(-2, row)
    elif fault == 'delta_before_added':
        row = find('response.function_call_arguments.delta');rows.remove(row);rows.insert(1, row)
    elif fault == 'sequence_repeated':rows[2]['sequence_number'] = rows[1]['sequence_number']
    elif fault == 'sequence_reversed':rows[2]['sequence_number'] = 0
    elif fault == 'sequence_missing':rows[2].pop('sequence_number')
    elif fault == 'other_response':rows[2]['response_id'] = 'resp_UNRELATED'
    elif fault == 'unknown_event':rows.insert(-1, dict(type='response.UNKNOWN', sequence_number=999))
    elif fault == 'missing_created':rows.pop(0)
    elif fault == 'late_created':rows.insert(-1, rows.pop(0))
    elif fault == 'missing_completed':rows.pop()
    elif fault == 'incomplete':rows[-1]['response']['status'] = 'incomplete'
    elif fault == 'unknown_usage':rows[-1]['response']['usage'] = None
    elif fault == 'bad_usage_sum':rows[-1]['response']['usage']['total_tokens'] = 101
    elif fault == 'missing_field':rows[-1]['response'].pop('output')
    elif fault == 'null':rows[-1]['response']['output'] = None
    elif fault in {'unknown_indicator', 'duplicate_json_key', 'unknown_parameter'}:
        value = {'unknown_indicator': '{"indicator":"NOT_REGISTERED","curr_date":"2026-11-03"}',
            'duplicate_json_key': '{"indicator":"rsi","indicator":"macd","curr_date":"2026-11-03"}',
            'unknown_parameter': '{"indicator":"rsi","curr_date":"2026-11-03","typo":1}'}[fault]
        for row in rows:
            if row['type'] == 'response.output_item.added':row['item']['arguments'] = ''
            if row['type'] == 'response.function_call_arguments.delta':row['delta'] = value
            if row['type'] == 'response.function_call_arguments.done':row['arguments'] = value
            if row['type'] == 'response.output_item.done':row['item']['arguments'] = value
        # One complete delta so this isolates JSON/Schema rather than fragmentation.
        rows = [r for r in rows if r['type'] != 'response.function_call_arguments.delta']
        rows.insert(2, dict(type='response.function_call_arguments.delta', item_id='fc_0', output_index=0,
                            delta=value, sequence_number=1))
    elif fault == 'done_not_completed':find('response.output_item.done')['item']['status'] = 'in_progress'
    elif fault == 'index_gap':
        for row in rows:
            if 'output_index' in row:row['output_index'] = 2
    elif fault == 'orphan_args':find('response.function_call_arguments.done')['item_id'] = 'fc_UNRELATED'
    # Renumber unless sequence metadata itself is the intended negative case.
    if not fault.startswith('sequence_'):
        for i, row in enumerate(rows):row['sequence_number'] = i
    with pytest.raises((LLMUnavailable, ValueError, TypeError)):
        invoke(rows)


def test_no_admission_before_iterator_exhaustion():
    rows, _ = complete_events()
    message = AIMessage(content='', response_metadata={'id': 'resp_TEST_FIXTURE'},
        usage_metadata={'input_tokens': 80, 'output_tokens': 20, 'total_tokens': 100,
                        'input_token_details': {'cache_read': 0, 'cache_creation': 0},
                        'output_token_details': {'reasoning': 0}})
    with scope(TOOLS) as evidence:
        stream = iter(ObservedResponse(iter(obj(r) for r in rows), evidence))
        for _ in rows:next(stream)
        assert not evidence.stream_exhausted
        with pytest.raises(LLMUnavailable, match='EVENT_COMPLETION_PROOF'):evidence.admit(message)
        stream.close()


@pytest.mark.parametrize('unknown', [False, True])
def test_native_guard_accounting_and_unknown_pause(audited, unknown):
    ledger, rid, session, meter = audited
    rows, _ = complete_events(7)
    if unknown:rows[-1]['response']['usage'] = None
    with session.guards(meter):
        if unknown:
            with pytest.raises(LLMUnavailable):invoke(rows)
        else:
            message, _ = invoke(rows);assert len(message.tool_calls) == 7
    s = ledger.status()
    assert s['llm_calls'] == s['http'] == 1
    if unknown:assert s['paused'] and s['total_usage'] == 'UNKNOWN'
    else:assert s['tokens'] == meter.value['reported_total_tokens'] == 100


def test_probe_shaped_stock_fixture_is_not_a_historical_replay(tmp_path, monkeypatch):
    path = tmp_path/'audit.jsonl';monkeypatch.setenv('TA_SINGLE_AUDIT_FILE', str(path))
    rows, items = complete_events(mode='interleaved', stock=True)
    delta = next(r for r in rows if r['type'] == 'response.function_call_arguments.delta')
    args = delta['delta'];at = rows.index(delta)
    # Match only the public structural count of 21 deltas, with synthetic values.
    rows[at:at+1] = [{**delta, 'delta': args[i*len(args)//21:(i+1)*len(args)//21]} for i in range(21)]
    for i, row in enumerate(rows):row['sequence_number'] = i
    message, _ = invoke(rows)
    assert message.tool_calls[0]['name'] == 'get_stock_data'
    assert message.tool_calls[0]['args'] == json.loads(items[0]['arguments'])
    assert last(path)['counts'] == dict(terminal=0, added=1, done=1, delta=1, arguments_done=1)
    assert last(path)['event_counts']['response.function_call_arguments.delta'] == 21
    # Historical summaries also contain an unidentified OTHER event: never
    # guess its original type or assert that the actual Probe would now pass.
    rows.insert(-1, dict(type='UNKNOWN_HISTORICAL_TYPE', sequence_number=999))
    with pytest.raises(LLMUnavailable, match='EVENT_COMPLETION_PROOF'):invoke(rows)


def test_deterministic_indicator_output_bytes_match_full_terminal(pinned):
    from tradingagents.agents.tools import get_indicators
    from tests.test_okx_multitimeframe import CFG, DAY
    from tradingagents.dataflows.config import run_config
    empty, _ = complete_events(mode='interleaved')
    full, _ = complete_events(mode='interleaved', empty=False)
    # Synthetic date aligned with the SAME frozen bundle on both paths.
    empty = json.loads(json.dumps(empty).replace('2026-11-03', DAY))
    full = json.loads(json.dumps(full).replace('2026-11-03', DAY))
    a, _ = invoke(empty);b, _ = invoke(full)
    aa, bb = a.tool_calls[0]['args'], b.tool_calls[0]['args']
    args = dict(symbol='BTC-USDT-SWAP', trade_date=DAY, market_snapshot=pinned)
    with run_config(CFG):
        left, right = get_indicators.invoke({**aa, **args}), get_indicators.invoke({**bb, **args})
    assert left.encode() == right.encode()
    assert 'DATA_UNAVAILABLE' not in left and 'RESEARCH_INVALID' not in left
    assert 'rsi' in left.lower()


def test_audit_failure_prevents_reconciled_admission(tmp_path, monkeypatch):
    monkeypatch.setenv('TA_SINGLE_AUDIT_FILE', str(tmp_path/'audit.jsonl'))
    def fail(*args):raise RuntimeError('TEST_FIXTURE_AUDIT_FAILURE')
    monkeypatch.setattr('tradingagents.agents.analysts.market_safe_adapter.append_audit', fail)
    with pytest.raises(RuntimeError, match='TEST_FIXTURE_AUDIT_FAILURE'):invoke(complete_events()[0])


@pytest.mark.parametrize('fault', ['terminal_args', 'terminal_id', 'terminal_call', 'reasoning_only'])
def test_nonempty_terminal_is_never_replaced_by_event_projection(fault):
    rows, _ = complete_events(empty=False)
    if fault == 'terminal_args':rows[-1]['response']['output'][0]['arguments'] = '{}'
    elif fault == 'terminal_id':rows[-1]['response']['output'][0]['id'] = 'fc_UNRELATED'
    elif fault == 'terminal_call':rows[-1]['response']['output'][0]['call_id'] = 'call_UNRELATED'
    else:rows[-1]['response']['output'] = [dict(type='reasoning', id='rs_TEST_FIXTURE', summary=[])]
    with pytest.raises(LLMUnavailable):invoke(rows)


def test_stream_exception_after_completed_does_not_enable_projection():
    rows, _ = complete_events()
    def stream():
        yield from (obj(r) for r in rows)
        raise RuntimeError('TEST_FIXTURE_STREAM_FAILURE')
    with scope(TOOLS) as evidence:
        with pytest.raises(RuntimeError, match='TEST_FIXTURE_STREAM_FAILURE'):
            list(ObservedResponse(stream(), evidence))
        assert not evidence.stream_exhausted and evidence.reconciliation is None


def test_native_market_turn_accepts_only_validated_empty_terminal():
    from tests.test_market_safe_adapter import model_for
    from tradingagents.agents.analysts.market_analyst import create_market_analyst
    from langchain_core.messages import HumanMessage
    rows, _ = complete_events(7)
    model, client = model_for(rows, [])
    try:
        result = create_market_analyst(model)(dict(company_of_interest='BTC-USDT-SWAP',
            trade_date='2026-11-03', messages=[HumanMessage(content='TEST_FIXTURE')]))
        assert len(result['messages'][0].tool_calls) == 7 and result['market_report'] == ''
    finally:client.close()


def test_empty_terminal_cannot_discard_non_tool_item_evidence():
    rows, _ = complete_events()
    item = dict(type='reasoning', id='rs_TEST_FIXTURE', summary=[])
    rows[1:1] = [dict(type='response.output_item.added', output_index=1, item=item),
                 dict(type='response.output_item.done', output_index=1, item=item)]
    for i, row in enumerate(rows):row['sequence_number'] = i
    with pytest.raises(LLMUnavailable, match='UNMATCHED_EVENT'):invoke(rows)


def test_reconciliation_capacity_failure_preserves_history(tmp_path, monkeypatch):
    from tradingagents.dataflows.audit_file import append_audit
    path = tmp_path/'audit.jsonl';monkeypatch.setenv('TA_SINGLE_AUDIT_FILE', str(path))
    append_audit(path, b'x'*(4194304-2)+b'\n', 4194304)
    before = path.read_bytes()
    # Isolate admission append. Filling the SDK's shared audit sink before
    # invoke would instead fail its first-stream diagnostic, before this gate.
    rows, _ = complete_events()
    message = AIMessage(content='', response_metadata={'id': 'resp_TEST_FIXTURE'},
        usage_metadata={'input_tokens': 80, 'output_tokens': 20, 'total_tokens': 100,
                        'input_token_details': {'cache_read': 0, 'cache_creation': 0},
                        'output_token_details': {'reasoning': 0}})
    with scope(TOOLS) as evidence:
        list(ObservedResponse(iter(obj(r) for r in rows), evidence))
        with pytest.raises(RuntimeError, match='CAPACITY'):evidence.admit(message)
        assert message.tool_calls == []
    assert path.read_bytes() == before


def test_schema_failure_does_not_claim_verified_reconciliation(tmp_path, monkeypatch):
    path = tmp_path/'audit.jsonl';monkeypatch.setenv('TA_SINGLE_AUDIT_FILE', str(path))
    rows, _ = complete_events(mode='interleaved')
    for row in rows:
        if row['type'] == 'response.function_call_arguments.delta':row['delta'] = '{}'
        if row['type'] == 'response.function_call_arguments.done':row['arguments'] = '{}'
        if row['type'] == 'response.output_item.done':row['item']['arguments'] = '{}'
    with pytest.raises(LLMUnavailable, match='SCHEMA'):invoke(rows)
    record = last(path)
    assert record['reconciliation']['status'] == 'CANDIDATE'
    assert not any(json.loads(r).get('stage') == 'MARKET_SAFE_ADMISSION' for r in path.read_text().splitlines())


def test_original_stream_exception_and_audit_failure_path_is_unchanged():
    import ast
    import subprocess
    from pathlib import Path
    name = 'tradingagents/agents/analysts/market_safe_adapter.py'
    old = subprocess.check_output(['git', 'show', 'd59d82a9b04d5657e5a8d0c8c78c2abc06233f45:'+name], text=True)
    def iterator(source):
        cls = next(c for c in ast.parse(source).body if isinstance(c, ast.ClassDef) and c.name == 'ObservedResponse')
        return next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == '__iter__')
    before, after = iterator(old), iterator(Path(name).read_text())
    after.body = [node for node in after.body if not (isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == 'stream_exhausted' for t in node.targets))]
    assert ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False)
