"""One Market protocol turn under the native ledger; never a research/F1 runner."""
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from tradingagents.forward_validation import atomic, digest, read
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.audit_file import append_audit
from tradingagents.safety_budget import Policy, SafetyStop, implementation_hash

PURPOSE = 'market-protocol-probe-v1'
VERSION = 'market-online-probe-v1'
ROOT = Path(__file__).resolve().parents[1]
SAFETY = dict(execution_enabled=False, allow_new_risk=False, simulation_admitted=False)


def folder(ledger):
    return ledger.root / 'probe'


def _new(path, value):
    """Same durable O_EXCL/no-follow claim protocol as the installed daily gate."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write((json.dumps(value, sort_keys=True) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)


def _need(condition, code):
    if not condition: raise SafetyStop(code)


def _scope(ledger, approval):
    obj = ledger.load(); policy = Policy.model_validate(obj['policy'])
    _need(policy.run_purpose == PURPOSE, 'PURPOSE_MISMATCH')
    _need(approval is not None, 'PROBE_APPROVAL_REQUIRED')
    approval = Path(approval).absolute(); policy_path = approval.with_name('policy.json')
    for path in [approval.parent, approval, policy_path]:
        st = path.lstat()
        _need(not stat.S_ISLNK(st.st_mode), 'PROBE_UNTRUSTED_CONFIG')
        if obj['namespace'] == 'PRODUCTION':
            _need(st.st_uid == 0 and not st.st_mode & 0o022, 'PROBE_UNTRUSTED_CONFIG')
    scope = read(approval)
    expected = {'version', 'purpose', 'approved', 'authorization_id', 'attempts_authorized', 'no_retry',
                'code_commit', 'module_sha256', 'implementation_hash', 'ledger_root',
                'policy_sha256', 'policy_config_hash', 'config_hash', 'not_before_ms', 'latest_claim_ms'}
    _need(type(scope) is dict and set(scope) == expected, 'PROBE_APPROVAL_FIELDS')
    _need(scope['version'] == VERSION and scope['purpose'] == PURPOSE and scope['approved'] is True
          and type(scope['attempts_authorized']) is int and scope['attempts_authorized'] == 1
          and scope['no_retry'] is True, 'PROBE_NOT_APPROVED')
    _need(type(scope['authorization_id']) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', scope['authorization_id']), 'PROBE_SCOPE')
    _need(scope['ledger_root'] == str(ledger.root), 'PROBE_SCOPE')
    _need(type(scope['not_before_ms']) is int and type(scope['latest_claim_ms']) is int
          and scope['latest_claim_ms'] - scope['not_before_ms'] == 60000, 'PROBE_CLAIM_WINDOW')
    _need(scope['code_commit'] == subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), 'PROBE_CODE_DRIFT')
    _need(scope['module_sha256'] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'PROBE_CODE_DRIFT')
    _need(scope['implementation_hash'] == implementation_hash(), 'IMPLEMENTATION_CHANGED')
    raw = policy_path.read_bytes()
    _need(scope['policy_sha256'] == hashlib.sha256(raw).hexdigest()
          and scope['policy_config_hash'] == digest(obj['policy'])
          and Policy.model_validate(json.loads(raw)).model_dump() == policy.model_dump(), 'PROBE_POLICY_DRIFT')
    _need(scope['config_hash'] == digest(_config(ledger, policy)), 'PROBE_CONFIG_DRIFT')
    if obj['namespace'] == 'PRODUCTION':
        from tradingagents.guarded_research import reviewed_implementation
        _need(obj['approved'] and policy.enabled and policy.accepted_post_response_exposure, 'USER_APPROVAL_REQUIRED')
        reviewed_implementation(extra=('market_online_probe.py',))
        _need(subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', 'tradingagents'], cwd=ROOT).returncode == 0,
              'PROBE_CODE_DRIFT')
    audit = folder(ledger) / 'audit.jsonl'
    _need(os.environ.get('TA_SINGLE_AUDIT_FILE', str(audit)) == str(audit), 'PROBE_AUDIT_SCOPE')
    return obj, policy, scope


def claim_once(ledger, approval):
    obj, policy, scope = _scope(ledger, approval)
    if obj['namespace'] == 'PRODUCTION':
        from tradingagents.guarded_research import resource_check
        resource_check(policy)
    with locked(ledger.root / 'controller', wait=False) as held:
        _need(held, 'CONTROLLER_BUSY')
        state = ledger.status()
        _need(not state['runs'] and not state['calls'] and not state['wires'] and not state['paused']
              and all(state[k] == 0 for k in ['tokens', 'llm_calls', 'http']), 'PROBE_NOT_PRISTINE')
        at = ledger.clock()
        _need(scope['not_before_ms'] <= at < scope['latest_claim_ms']
              and obj['events'][0]['at_ms'] <= at < state['expires_ms'], 'PROBE_CLAIM_WINDOW')
        target = folder(ledger)
        target.mkdir(mode=0o700, exist_ok=True)
        st = target.lstat()
        _need(stat.S_ISDIR(st.st_mode) and not target.is_symlink(), 'PROBE_DIRECTORY')
        if os.name != 'nt':
            _need(st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o700, 'PROBE_DIRECTORY')
        value = {'version': VERSION, 'purpose': PURPOSE, 'scope': 'SINGLE_ATTEMPT_CONSUMED_NO_RETRY',
                 'status': 'UNFINALIZED',
                 'approval_sha256': hashlib.sha256(Path(approval).read_bytes()).hexdigest(),
                 'authorization_id': scope['authorization_id'], 'claimed_ms': at, **SAFETY}
        _new(target / 'claim.json', value)
    return {'status': 'PROBE_CLAIMED', **SAFETY}


def verify_claim(ledger, approval, *, for_launch=False):
    _, _, scope = _scope(ledger, approval)
    path = folder(ledger) / 'claim.json'
    _need(not path.is_symlink(), 'PROBE_CLAIM_IDENTITY')
    st = path.lstat()
    _need(stat.S_ISREG(st.st_mode), 'PROBE_CLAIM_IDENTITY')
    if os.name != 'nt':
        _need(st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o600, 'PROBE_CLAIM_IDENTITY')
    claim = read(path)
    _need(claim.get('version') == VERSION and claim.get('purpose') == PURPOSE
          and claim.get('scope') == 'SINGLE_ATTEMPT_CONSUMED_NO_RETRY'
          and claim.get('authorization_id') == scope['authorization_id']
          and claim.get('approval_sha256') == hashlib.sha256(Path(approval).read_bytes()).hexdigest()
          and type(claim.get('claimed_ms')) is int
          and scope['not_before_ms'] <= claim['claimed_ms'] < scope['latest_claim_ms'], 'PROBE_CLAIM_IDENTITY')
    if for_launch:
        _need(ledger.clock() < scope['latest_claim_ms'], 'PROBE_CLAIM_WINDOW')
        _need(not (folder(ledger) / 'binding.json').exists(), 'PROBE_ALREADY_BOUND')
    return claim


def bind_run(ledger, run_id, approval):
    verify_claim(ledger, approval)
    _need(ledger.status()['runs'][run_id]['status'] == 'RUNNING', 'RESEARCH_NOT_ACTIVE')
    value = {'version': VERSION, 'purpose': PURPOSE, 'run_id': run_id,
             'claim_sha256': hashlib.sha256((folder(ledger) / 'claim.json').read_bytes()).hexdigest(), **SAFETY}
    _new(folder(ledger) / 'binding.json', value)
    _new(folder(ledger) / 'receipt.json', {**value, 'status': 'UNFINALIZED'})


def _config(ledger, policy):
    from tradingagents.default_config import build_default_config
    config = build_default_config()
    _need(config['llm_provider'] == 'chatgpt' and config['quick_think_llm'] == policy.model
          and config['deep_think_llm'] == policy.model
          and all(config.get(k) in {None, 'chatgpt'} for k in ['quick_think_provider', 'deep_think_provider']), 'UNAPPROVED_MODEL')
    config.update(market_mode='okx_usdt_perpetual', okx_interval='4h', okx_multi_timeframe=True,
                  okx_derivatives_enabled=True, okx_events_enabled=True, okx_trade_plan_enabled=False,
                  okx_orderflow_enabled=False, okx_market_checks_enabled=False, okx_price_depth_enabled=False,
                  checkpoint_enabled=False, results_dir=str(folder(ledger) / 'artifacts'),
                  data_cache_dir=str(folder(ledger) / 'cache'), memory_log_path=str(folder(ledger) / 'unused-memory'))
    config['data_vendors'].update(core_stock_apis='okx', technical_indicators='okx')
    return config


def _market_turn(config):
    from tradingagents.dataflows.okx_research import preflight
    from tradingagents.agents.analysts.market_analyst import create_market_analyst
    from tradingagents.llm_clients.factory import create_tier_client
    from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI
    from tradingagents.graph.propagation import Propagator
    from langchain_core.messages import convert_to_messages
    state = Propagator().create_initial_state('BTC-USDT-SWAP', datetime.now(UTC).strftime('%Y-%m-%d'),
        asset_type='crypto', instrument_context=(
            'Exchange: OKX; instrument: BTC-USDT-SWAP; type: linear USDT-settled SWAP. '
            'Required timeframes: 1d regime, 4h structure, 1h confirmation; compatibility anchor: 4h; '
            'UTC closed candles only. This is a perpetual contract, not Yahoo/spot. Research only; execution disabled.'))
    state['messages'] = convert_to_messages(state['messages'])
    state.update(preflight(state))
    _need(state.get('data_quality') == 'VALID' and state.get('allow_new_risk') is True, 'PROBE_DATA_BLOCKED')
    # Quality passage is not a trading permission. Never create a plan or graph.
    state.update(SAFETY)
    llm = create_tier_client(config, 'quick').get_llm()
    _need(type(llm) is ChatGPTPlanChatOpenAI, 'AUTH_ADAPTER_CHANGED')
    result = create_market_analyst(llm)(state)
    messages = result.get('messages', [])
    _need(len(messages) == 1 and not messages[0].invalid_tool_calls, 'PROBE_RESULT')
    # No tool dispatch, second turn, report writer, graph or F1 entry.
    snapshot = state['market_snapshot']
    return {'tool_count': len(messages[0].tool_calls), 'data_quality': state['data_quality'],
            'snapshot_sha256': digest(snapshot), 'cutoff_ms': snapshot.get('cutoff_ms')}


def finalize_interrupted(ledger, run_id):
    policy = Policy.model_validate(ledger.load()['policy'])
    _need(policy.run_purpose == PURPOSE, 'PURPOSE_MISMATCH')
    path = folder(ledger) / 'receipt.json'
    if not path.exists(): return
    receipt = read(path)
    _need(receipt.get('run_id') == run_id and receipt.get('purpose') == PURPOSE
          and receipt.get('version') == VERSION, 'PROBE_BINDING')
    if receipt['status'] != 'UNFINALIZED': return
    state = ledger.status()
    if state['runs'][run_id]['status'] == 'RUNNING': return
    if state['runs'][run_id]['status'] == 'COMPLETED':
        with ledger.transaction() as (obj, _, __): ledger._pause(obj, 'PROBE_FINALIZATION_FAILURE')
    receipt.update(status='USAGE_UNKNOWN' if state['unknown_usage'] else 'FAILED',
                   reported_tokens=state['tokens'], total_usage=state['total_usage'],
                   logical_calls=state['llm_calls'], http=state['http'], **SAFETY)
    atomic(path, receipt)


def worker(ledger, run_id, approval, *, invoker=None):
    from unittest.mock import patch
    from tradingagents.guarded_research import Session
    from tradingagents.dataflows.config import run_config
    _need(not (ledger.load()['namespace'] == 'PRODUCTION' and invoker is not None), 'FIXTURE_CANNOT_REGISTER_PRODUCTION')
    obj, policy, scope = _scope(ledger, approval)
    _need(not (obj['namespace'] == 'PRODUCTION' and invoker is not None), 'FIXTURE_CANNOT_REGISTER_PRODUCTION')
    _need(not (obj['namespace'] == 'FIXTURE' and invoker is None), 'FIXTURE_CANNOT_CALL_MODEL')
    verify_claim(ledger, approval)
    binding = read(folder(ledger) / 'binding.json')
    _need(binding.get('version') == VERSION and binding.get('purpose') == PURPOSE
          and binding['run_id'] == run_id
          and binding['claim_sha256'] == hashlib.sha256((folder(ledger) / 'claim.json').read_bytes()).hexdigest(), 'PROBE_BINDING')
    _need(ledger.status()['runs'][run_id]['status'] == 'RUNNING', 'RESEARCH_NOT_ACTIVE')
    receipt = read(folder(ledger) / 'receipt.json')
    _need(receipt.get('run_id') == run_id and receipt.get('purpose') == PURPOSE
          and receipt.get('version') == VERSION and receipt.get('status') == 'UNFINALIZED', 'PROBE_BINDING')
    _new(folder(ledger) / 'worker.entered.json', {'version': VERSION, 'run_id': run_id, **SAFETY})
    session = Session(ledger, run_id, health=(lambda: None) if invoker else None)
    try:
        session.check()
        config = _config(ledger, policy)
        _need(digest(config) == scope['config_hash'], 'PROBE_CONFIG_DRIFT')
        receipt['config_hash'] = digest(config)
        atomic(folder(ledger) / 'receipt.json', receipt)
        meter = session.meter_class()(folder(ledger) / 'meter.json', 1, policy.research_http, policy.task_seconds)
        # Establish the bounded private audit sink before any possible request.
        append_audit(folder(ledger) / 'audit.jsonl', (json.dumps({'kind': 'MARKET_PROBE_ADMISSION',
                     'version': VERSION, 'run_id': run_id, **SAFETY}) + '\n').encode(), 4 * 1024**2)
        with patch.dict(os.environ, {'TA_SINGLE_AUDIT_FILE': str(folder(ledger) / 'audit.jsonl')}), run_config(config), session.guards(meter):
            result = invoker(config) if invoker else _market_turn(config)
        state = ledger.status(); run = state['runs'][run_id]
        calls = [c for c in state['calls'].values() if c['run_id'] == run_id]
        _need(len(calls) == 1 and calls[0]['status'] == 'COMPLETE' and not state['paused']
              and meter.value['reported_total_tokens'] == run['tokens'], 'PROBE_RECONCILIATION')
        _need(type(result) is dict and type(result.get('tool_count')) is int and result['tool_count'] >= 0, 'PROBE_RESULT')
        ledger.finish_research(run_id, run['tokens'], success=True)
        receipt.update(status='PASS' if result['tool_count'] else 'INCONCLUSIVE_NO_TOOL_CALL',
                       tool_count=result['tool_count'], reported_tokens=run['tokens'], total_usage=run['tokens'],
                       logical_calls=run['llm_calls'], http=run['http'], **SAFETY)
        for key in ['data_quality', 'snapshot_sha256', 'cutoff_ms']:
            if key in result: receipt[key] = result[key]
        atomic(folder(ledger) / 'receipt.json', receipt)
        return receipt
    except BaseException:
        if ledger.status()['runs'][run_id]['status'] == 'RUNNING':
            ledger.finish_research(run_id, None, success=False)
        finalize_interrupted(ledger, run_id)
        raise
