"""Synthetic offline structures, never a reconstruction of window-003."""
import copy
import hashlib
import json
import os
import ast
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts.market_analyst import TOOLS
from tradingagents.agents.analysts.market_safe_adapter import scope, ObservedResponse
from tradingagents.agents.analysts.market_safe_adapter import terminal_structure
from tradingagents.dataflows.audit_file import append_audit
from tradingagents.llm_clients.diagnostics import CONTEXT
from tradingagents.llm_clients.reliability import LLMUnavailable
from tests.test_market_safe_adapter import events, invoke


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', lambda *a, **k: pytest.fail('REAL_NETWORK_FORBIDDEN'))


@pytest.fixture
def audit(tmp_path, monkeypatch):
    path = tmp_path / 'fixture.jsonl'
    monkeypatch.setenv('TA_SINGLE_AUDIT_FILE', str(path))
    return path


def obj(value):
    if isinstance(value, dict):
        result = NS(**{k: obj(v) for k, v in value.items()})
        if 'input_tokens' in value:
            result.model_dump = lambda: copy.deepcopy(value)
        return result
    if isinstance(value, list):
        return [obj(v) for v in value]
    return value


def feed(evidence, rows):
    token = CONTEXT.set({'call_id': 'FIXTURE_LOGICAL_CALL'})
    try:
        for row in rows:
            evidence.observe(obj(row))
    finally:
        CONTEXT.reset(token)
    return AIMessage(content='', response_metadata={'id': 'resp_TEST_FIXTURE'},
                     usage_metadata={'input_tokens': 80, 'output_tokens': 20, 'total_tokens': 100,
                                     'input_token_details': {'cache_read': 0, 'cache_creation': 0},
                                     'output_token_details': {'reasoning': 0}})


def last(path):
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line)['kind'] == 'MARKET_EVENT_DIAGNOSTIC'][-1]


@pytest.mark.parametrize('n', [1, 6, 7])
@pytest.mark.parametrize('mode', ['sequential', 'interleaved', 'fragmented', 'done_only'])
def test_normal_sdk_result_and_diagnostics(n, mode, audit):
    rows, items = events(n, mode)
    message, _ = invoke(rows)
    assert [c['args'] for c in message.tool_calls] == [json.loads(i['arguments']) for i in items]
    record = last(audit)
    assert record['code'] == 'TERMINAL_VERIFIED'
    assert record['counts']['terminal'] == n
    assert not any(record['conditions'].values())


@pytest.mark.parametrize('case,expected', [
    ('added', (True, False, False)), ('done', (False, True, False)),
    ('delta', (False, False, True)), ('arguments_done', (False, False, True)),
    ('all', (True, True, True)),
])
def test_independent_unmatched_conditions(case, expected, audit):
    rows, _ = events()
    with scope(TOOLS) as evidence:
        message = feed(evidence, rows)
        if case in {'added', 'all'}:
            evidence.added['fc_orphan_added'] = (3, 'call_extra', 'get_indicators', '', None)
        if case in {'done', 'all'}:
            evidence.done['fc_orphan_done'] = (4, 'call_extra2', 'get_indicators', '', None)
        if case in {'delta', 'all'}:
            evidence.deltas['fc_orphan_delta'] = ['SECRET_PARAMETER_VALUE']
        if case in {'arguments_done', 'all'}:
            evidence.arguments_done['fc_orphan_final'] = 'SECRET_PARAMETER_VALUE'
        with pytest.raises(LLMUnavailable, match='UNMATCHED_EVENT'):
            evidence.admit(message)
    record = last(audit)
    assert tuple(record['conditions'][k] for k in
                 ['terminal_added_mismatch', 'terminal_done_mismatch', 'orphan_argument_events']) == expected
    assert record['logical_call_id_sha256'] == hashlib.sha256(b'FIXTURE_LOGICAL_CALL').hexdigest()
    assert record['legacy_rejection_call_id_is_placeholder'] is True
    assert 'SECRET_PARAMETER_VALUE' not in audit.read_text()


@pytest.mark.parametrize('missing', ['added', 'done'])
def test_missing_final_evidence_has_directional_difference(missing, audit):
    rows, _ = events()
    with scope(TOOLS) as evidence:
        message = feed(evidence, rows)
        getattr(evidence, missing).clear()
        with pytest.raises(LLMUnavailable, match='MISSING_FINAL_EVIDENCE'):
            evidence.admit(message)
    assert last(audit)['differences']['terminal_without_' + missing]['count'] == 1


@pytest.mark.parametrize('fault,code', [
    ('duplicate', 'DUPLICATE_OR_LIMIT'), ('index', 'INDEX_CONFLICT'),
    ('parameters', 'FINAL_CONFLICT'), ('missing_id', 'IDENTITY'),
    ('duplicate_terminal_call', 'DUPLICATE_ID'),
])
def test_other_rejections_keep_original_codes(fault, code, audit):
    rows, _ = events(2)
    if fault == 'duplicate':
        rows.insert(1, copy.deepcopy(rows[0]))
    elif fault == 'index':
        rows[1]['output_index'] = 0
    elif fault == 'parameters':
        next(r for r in rows if r['type'] == 'response.output_item.done')['item']['arguments'] = '{}'
    elif fault == 'missing_id':
        rows[0]['item'].pop('id')
    else:
        rows[-1]['response']['output'][1]['call_id'] = 'call_0'
    with scope(TOOLS) as evidence:
        message = feed(evidence, rows)
        with pytest.raises(LLMUnavailable, match=code):
            evidence.admit(message)
    record = last(audit)
    assert record['code'] == code
    if fault == 'missing_id':
        assert record['first_error_event']['item']['state'] == 'MISSING'
    if fault == 'duplicate':
        assert record['first_error_event']['event_type'] == 'response.output_item.added'


@pytest.mark.parametrize('failure', ['eof', 'exception', 'unknown_usage'])
def test_incomplete_stream_and_unknown_usage_are_diagnosed(failure, audit):
    rows, _ = events()
    with scope(TOOLS) as evidence:
        if failure == 'unknown_usage':
            rows[-1]['response']['usage'] = None
            message = feed(evidence, rows)
            with pytest.raises(LLMUnavailable, match='USAGE_UNCONFIRMED'):
                evidence.admit(message)
        else:
            def stream():
                yield obj(rows[0])
                if failure == 'exception':
                    raise RuntimeError('SECRET_STREAM_BODY')
            if failure == 'exception':
                with pytest.raises(RuntimeError, match='SECRET_STREAM_BODY'):
                    list(ObservedResponse(stream(), evidence))
            else:
                list(ObservedResponse(stream(), evidence))
    record = last(audit)
    assert record['code'] == {'eof': 'STREAM_WITHOUT_COMPLETED', 'exception': 'STREAM_INTERRUPTED',
                              'unknown_usage': 'USAGE_UNCONFIRMED'}[failure]
    assert 'SECRET_STREAM_BODY' not in audit.read_text()


def test_orphan_parameter_event_is_collected_by_original_observer(audit):
    rows, _ = events()
    rows.insert(-1, dict(type='response.function_call_arguments.done', output_index=9,
                         item_id='fc_orphan', arguments='SECRET'))
    with scope(TOOLS) as evidence:
        message = feed(evidence, rows)
        with pytest.raises(LLMUnavailable, match='UNMATCHED_EVENT'):
            evidence.admit(message)
    assert last(audit)['differences']['orphan_arguments_done']['count'] == 1
    assert 'SECRET' not in audit.read_text()


def test_identity_redaction_and_bounded_record(audit):
    rows, _ = events()
    with scope(TOOLS) as evidence:
        feed(evidence, rows)
        for i in range(100):
            evidence.observe(NS(type='SECRET_EVENT_TEXT', item=NS(id='SECRET_ID', call_id='SECRET_CALL'),
                                output_index=i, arguments='SECRET_ARGUMENT', headers='SECRET_KEY'))
        evidence.diagnostic('TEST_FIXTURE')
    record = last(audit)
    assert len(record['event_identities']) == 64 and record['event_identities_truncated']
    assert record['event_counts']['OTHER'] == 100
    assert len(audit.read_bytes()) < 65536
    assert 'SECRET_' not in audit.read_text()


def test_diagnostic_capacity_failure_propagates_without_dispatch(audit):
    append_audit(audit, b'x' * (4194304 - 2) + b'\n', 4194304)
    before = audit.read_bytes()
    rows, _ = events()
    with scope(TOOLS) as evidence:
        message = feed(evidence, rows)
        with pytest.raises(RuntimeError, match='CAPACITY'):
            evidence.admit(message)
        assert message.tool_calls == []
    assert audit.read_bytes() == before


def test_concurrent_diagnostics_are_complete_json(audit):
    def write(_):
        with scope(TOOLS) as evidence:
            evidence.diagnostic('TEST_FIXTURE')
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(24)))
    assert len(audit.read_text().splitlines()) == 24
    assert all(json.loads(line)['code'] == 'TEST_FIXTURE' for line in audit.read_text().splitlines())
    assert audit.with_name(audit.name + '.lock').exists()
    if os.name == 'posix':
        assert audit.stat().st_mode & 0o777 == 0o600


def test_shared_admission_validators_are_unchanged():
    """Keep original validators; only the explicit output proof selector changes."""
    name = 'tradingagents/agents/analysts/market_safe_adapter.py'
    old = subprocess.check_output(['git', 'show', '348ae9983242c4dd86463ceae4702de5c313ace5:' + name], text=True)
    new = (Path(__file__).resolve().parents[1] / name).read_text()
    def methods(source):
        cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'Evidence')
        return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    before, after = methods(old), methods(new)
    class StripDiagnostics(ast.NodeTransformer):
        def visit_Assign(self, node):
            if any(isinstance(t, ast.Attribute) and t.attr == 'admission_item' for t in node.targets):
                return None
            return self.generic_visit(node)

        def visit_If(self, node):
            if (isinstance(node.test, ast.Attribute) and node.test.attr == 'reconciliation'
                    and len(node.body) == 1 and isinstance(node.body[0], ast.Assign)):
                return None  # Audit status only, not a validation exemption.
            return self.generic_visit(node)

        def visit_For(self, node):
            if (isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute)
                    and node.iter.func.attr == 'verified_output'):
                node.iter = ast.parse("enumerate(getattr(response, 'output', ()))", mode='eval').body
            return self.generic_visit(node)

        def visit_Expr(self, node):
            if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == 'diagnostic'):
                return None
            return self.generic_visit(node)
    for name in ['_observe', 'identifier', 'parse', 'same', 'admit']:
        if name == 'admit':
            after[name] = StripDiagnostics().visit(after[name])
        assert ast.dump(before[name], include_attributes=False) == ast.dump(after[name], include_attributes=False)


@pytest.mark.parametrize('response,classification,container,length,counts', [
    (NS(), 'OUTPUT_MISSING', 'MISSING', None, {}),
    (NS(output=None), 'OUTPUT_NONE', 'NONE', None, {}),
    (NS(output=[]), 'EMPTY_ARRAY', 'list', 0, {}),
    (NS(output=[NS(type='reasoning')]), 'REASONING_ONLY', 'list', 1, {'reasoning': 1}),
    (NS(output=[NS(type='function_call'), NS(type='reasoning')]), 'CONTAINS_FUNCTION_CALL',
     'list', 2, {'function_call': 1, 'reasoning': 1}),
    (NS(output=[NS(type='message')]), 'NON_TOOL_ITEMS', 'list', 1, {'message': 1}),
    (NS(output='SECRET_BODY'), 'UNRECOGNIZED', 'OTHER', None, {}),
    (NS(output=[NS(type='SECRET_TYPE')]), 'UNRECOGNIZED', 'list', 1, {'OTHER': 1}),
    ({'output': []}, 'EMPTY_ARRAY', 'list', 0, {}),
])
def test_terminal_shape_categories_are_metadata_only(response, classification, container, length, counts):
    result = terminal_structure(response)
    assert result['classification'] == classification
    assert result['output_container'] == container
    assert result['output_length'] == length
    assert result['item_type_counts'] == counts
    assert 'SECRET' not in json.dumps(result)


def test_sdk_boundary_snapshot_is_preserved_before_admission(audit):
    rows, _ = events()
    with scope(TOOLS) as evidence:
        message = feed(evidence, rows)
        assert evidence.sdk_terminal_structure['classification'] == 'CONTAINS_FUNCTION_CALL'
        evidence.completed.output = []  # Synthetic mutation, not historical evidence.
        with pytest.raises(LLMUnavailable, match='UNMATCHED_EVENT'):
            evidence.admit(message)
    record = last(audit)
    assert record['sdk_terminal_structure']['output_length'] == 1
    assert record['admission_terminal_structure']['classification'] == 'EMPTY_ARRAY'
    assert record['counts']['terminal'] == 0


@pytest.mark.parametrize('shape', ['missing', 'none', 'empty', 'reasoning'])
def test_shape_diagnostic_does_not_replace_original_rejection(shape, audit):
    rows, _ = events()
    with scope(TOOLS) as evidence:
        message = feed(evidence, rows)
        if shape == 'missing':
            del evidence.completed.output
        else:
            evidence.completed.output = {'none': None, 'empty': [], 'reasoning': [NS(type='reasoning')]}[shape]
        evidence.fail('INCOMPLETE')
        with pytest.raises(LLMUnavailable, match='MARKET_TOOL_PROTOCOL_INCOMPLETE'):
            evidence.admit(message)
    assert last(audit)['code'] == 'INCOMPLETE'


def test_none_output_diagnostic_preserves_stream_exception(audit):
    with scope(TOOLS) as evidence:
        evidence.completed = NS(output=None)
        def stream():
            raise ValueError('TEST_FIXTURE_ORIGINAL_STREAM_FAILURE')
            yield
        with pytest.raises(ValueError, match='TEST_FIXTURE_ORIGINAL_STREAM_FAILURE'):
            list(ObservedResponse(stream(), evidence))
    assert last(audit)['code'] == 'STREAM_INTERRUPTED'
    assert last(audit)['admission_terminal_structure']['classification'] == 'OUTPUT_NONE'


def test_shape_projection_is_bounded_and_does_not_log_class_names_or_properties(audit):
    class SecretProviderClass:
        @property
        def output(self):
            raise TypeError('SECRET_RESPONSE_BODY')
    result = terminal_structure(SecretProviderClass())
    assert result['object_type'] == 'OTHER'
    assert result['inspection_failed'] and result['classification'] == 'UNRECOGNIZED'
    assert 'SECRET' not in json.dumps(result)
    many = terminal_structure(NS(output=[NS(type='reasoning', text='SECRET')] * 10000))
    assert many['output_length'] == 10000 and many['items_scanned'] == 256
    assert many['truncated'] and many['classification'] == 'UNRECOGNIZED'
    assert many['item_type_counts'] == {'reasoning': 256}
    with scope(TOOLS) as evidence:
        evidence.completed = SecretProviderClass()
        evidence.diagnostic('STREAM_INTERRUPTED')
    assert 'SECRET' not in audit.read_text()


def test_terminal_shape_audit_failure_is_not_swallowed(audit, monkeypatch):
    def failed_append(*args):
        raise RuntimeError('TEST_FIXTURE_AUDIT_FAILURE')
    monkeypatch.setattr('tradingagents.agents.analysts.market_safe_adapter.append_audit', failed_append)
    with scope(TOOLS) as evidence:
        evidence.completed = NS(output=None)
        with pytest.raises(RuntimeError, match='TEST_FIXTURE_AUDIT_FAILURE'):
            evidence.diagnostic('STREAM_INTERRUPTED')


def test_completed_response_property_failure_keeps_original_event_structure_rejection(audit):
    class BrokenEvent:
        type = 'response.completed'
        @property
        def response(self):
            raise TypeError('SECRET_PROVIDER_BODY')
    with scope(TOOLS) as evidence:
        evidence.observe(BrokenEvent())
        assert evidence.error == 'EVENT_STRUCTURE'
        with pytest.raises(LLMUnavailable, match='EVENT_STRUCTURE'):
            evidence.admit(NS())
    assert last(audit)['sdk_terminal_structure']['inspection_failed']
    assert 'SECRET_PROVIDER_BODY' not in audit.read_text()


def test_complete_terminal_structure_and_normal_tool_output_match_baseline(audit):
    # Existing invoke path: same expected native IDs, arguments and tools.
    rows, items = events(7, 'fragmented')
    message, _ = invoke(rows)
    assert message.tool_calls == [dict(name=i['name'], args=json.loads(i['arguments']),
                                      id=i['call_id'], type='tool_call') for i in items]
    record = last(audit)
    assert record['sdk_terminal_structure'] == record['admission_terminal_structure']
    assert record['sdk_terminal_structure']['item_type_counts'] == {'function_call': 7}


def test_normal_admission_message_is_byte_equal_to_fixed_commit(audit):
    source = subprocess.check_output(['git', 'show',
        '17bfd7f2dfe330691ab4329f3af22046fb121c30:tradingagents/agents/analysts/market_safe_adapter.py'], text=True)
    baseline = {'__name__': 'TEST_FIXTURE_BASELINE'}
    exec(compile(source, 'TEST_FIXTURE_BASELINE', 'exec'), baseline)
    rows, _ = events(7, 'fragmented')
    old = baseline['Evidence'](TOOLS)
    old_message = old.admit(feed(old, rows))
    with scope(TOOLS) as evidence:
        new_message = evidence.admit(feed(evidence, rows))
    assert new_message.model_dump_json().encode() == old_message.model_dump_json().encode()


@pytest.mark.parametrize('value,classification,name', [
    ('response.queued', 'SDK_KNOWN', 'response.queued'),
    ('response.reasoning_text.done', 'SDK_KNOWN', 'response.reasoning_text.done'),
    ('response.vendor_extension.done', 'UNKNOWN_NAME', 'response.vendor_extension.done'),
    (None, 'MISSING', None), (42, 'INVALID_TYPE', None),
    ('SECRET_BODY\nAuthorization: SECRET_KEY', 'REDACTED_NAME', None),
    ('response.' + 'x' * 200, 'REDACTED_NAME', None)])
def test_other_original_type_is_bounded_classified_and_never_allowed(value, classification, name, audit):
    from tests.test_market_stream_reconciliation import complete_events
    rows, _ = complete_events()
    rows.insert(-2, dict(type=value, response_id='resp_TEST_FIXTURE', call_id='SECRET_CALL',
                        item_id='SECRET_ITEM', arguments='SECRET_ARGUMENTS', headers='SECRET_KEY'))
    for i, row in enumerate(rows):row['sequence_number'] = i
    with scope(TOOLS) as evidence:
        message = feed(evidence, [])
        list(ObservedResponse(iter(obj(row) for row in rows), evidence))
        with pytest.raises(LLMUnavailable, match='EVENT_COMPLETION_PROOF'):
            evidence.admit(message)
    record = last(audit)
    summary = record['other_event_diagnostics']['events'][0]
    assert summary['type'] == dict(name=name, classification=classification)
    assert summary['sequence_number'] == summary['event_ordinal']
    assert summary['event_call'] == evidence.identity('SECRET_CALL')
    assert summary['item'] == evidence.identity('SECRET_ITEM')
    assert summary['response_id'] == evidence.identity('resp_TEST_FIXTURE')
    assert record['completion_proof']['gate_rejected']
    assert record['completion_proof']['failed_conditions']['other_events_present']
    assert 'SECRET_' not in audit.read_text()
    assert message.tool_calls == []


def test_all_completion_proof_failures_are_reported_together(audit):
    with scope(TOOLS) as evidence:
        evidence.observe(NS(type='response.vendor_extension', sequence_number=None,
                            response_id='resp_OTHER', response=NS(id='resp_NESTED')))
        evidence.diagnostic('EVENT_COMPLETION_PROOF')
    result = last(audit)
    assert len(result['completion_proof']['failed_conditions']) == 7
    assert all(result['completion_proof']['failed_conditions'].values())
    other = result['other_event_diagnostics']['events'][0]
    assert other['sequence_state'] == 'MISSING'
    assert other['nested_response_id'] == evidence.identity('resp_NESTED')
    assert other['response_id'] == evidence.identity('resp_OTHER')


def test_successful_empty_terminal_has_no_completion_proof_failures(audit):
    from tests.test_market_stream_reconciliation import complete_events
    rows, _ = complete_events()
    message, _ = invoke(rows)
    record = last(audit)
    assert not any(record['completion_proof']['failed_conditions'].values())
    assert not record['completion_proof']['gate_rejected']
    assert record['other_event_diagnostics'] == dict(version='market-other-events-v1', events=[], truncated=False)
    assert len(message.tool_calls) == 1


@pytest.mark.parametrize('sequence', [-1, True, 'SECRET_SEQUENCE', 2**100])
def test_other_sequence_is_bounded_without_affecting_original_observer(sequence, audit):
    with scope(TOOLS) as evidence:
        evidence.observe(NS(type='response.vendor_extension', sequence_number=sequence))
        evidence.diagnostic('TEST_FIXTURE')
    summary = last(audit)['other_event_diagnostics']['events'][0]
    assert summary['sequence_number'] is None and summary['sequence_state'] == 'INVALID'
    assert 'SECRET_' not in audit.read_text()


def test_other_samples_have_independent_capacity_and_explicit_truncation(audit):
    with scope(TOOLS) as evidence:
        for i in range(100):
            evidence.observe(NS(type='response.vendor_extension', sequence_number=i))
        evidence.diagnostic('TEST_FIXTURE')
    record = last(audit)
    assert record['event_counts']['OTHER'] == 100
    assert len(record['other_event_diagnostics']['events']) == 32
    assert record['other_event_diagnostics']['truncated']
    assert len(audit.read_bytes()) < 65536


def test_diagnostic_change_keeps_all_matching_and_completion_gates_identical():
    source = subprocess.check_output(['git', 'show',
        '3f385e1fd6d960070a5870eaa412aa3f6cca1a74:tradingagents/agents/analysts/market_safe_adapter.py'], text=True)
    current = (Path(__file__).resolve().parents[1] / 'tradingagents/agents/analysts/market_safe_adapter.py').read_text()
    def methods(text):
        cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == 'Evidence')
        return {n.name: ast.dump(n, include_attributes=False) for n in cls.body if isinstance(n, ast.FunctionDef)}
    old, new = methods(source), methods(current)
    for name in ['_observe', 'verified_output', 'admit', 'identifier', 'parse', 'same', 'fail']:
        assert old[name] == new[name], name


def test_new_metadata_property_failure_does_not_change_original_observation(audit):
    class BrokenMetadata:
        type = 'response.vendor_extension'
        sequence_number = 0
        @property
        def response(self):
            raise RuntimeError('SECRET_METADATA')
    with scope(TOOLS) as evidence:
        evidence.observe(BrokenMetadata())
        assert evidence.error is None
        assert evidence.events == 1 and evidence.event_counts['OTHER'] == 1
        evidence.diagnostic('TEST_FIXTURE')
    assert last(audit)['other_event_diagnostics']['events'] == [dict(inspection_failed=True)]
    assert 'SECRET_METADATA' not in audit.read_text()
