"""Opt-in immutable registration-time attestation; never an entry/risk permission."""
import copy
from datetime import datetime
from tradingagents.dataflows.vendors import okx, okx_derivatives as derivatives
from tradingagents.dataflows import perpetual_plan as plans
from tradingagents.forward_validation import digest
VERSION = 'forward-registration-revalidation-v1'
KEY = 'registration_revalidation'
POLICY = {'version': VERSION, 'scope': 'REGISTRATION_OBSERVATION_ONLY', 'proof_max_age_ms': 60000, 'execution_enabled': False, 'allow_new_risk': False, 'simulation_admitted': False}
POLICY_HASH = digest(POLICY)

def build(snapshot, result, config, completed, registered):
    """Pure. Reject all originally invalid evidence; retain natural ageing diagnostics."""
    if config.get('forward_admission_policy') != VERSION:
        return None
    try:
        if not (result['plan_valid'] and result['proposed_action'] in {'OPEN_LONG_CANDIDATE', 'OPEN_SHORT_CANDIDATE'}):
            raise ValueError('Invalid registration admission evidence')
        if not (result['allow_new_risk'] is False and result['execution_enabled'] is False):
            raise ValueError('Invalid registration admission evidence')
        if not snapshot['snapshot_id'] == okx.fingerprint(snapshot) == result['snapshot_id']:
            raise ValueError('Invalid registration admission evidence')
        if not (snapshot['status'] == 'VALID' and snapshot['cutoff_ms'] <= completed <= registered):
            raise ValueError('Invalid registration admission evidence')
        if not all((c['status'] == 'VALID' and c['snapshot_id'] == okx.fingerprint(c) for c in snapshot['timeframes'].values())):
            raise ValueError('Invalid registration admission evidence')
        raw = snapshot['derivatives']
        if raw.get('instrument') != snapshot['symbol'] or raw.get('research_cutoff_ms') != snapshot['cutoff_ms']:
            raise ValueError('Original derivative ownership mismatch')
        if any(not part.get('data') for part in raw['components'].values()):
            raise ValueError('Incomplete original derivative evidence')
        if not all((p['status'] == 'VALID' for p in raw['components'].values())):
            raise ValueError('Invalid registration admission evidence')
        for name in plans.CRITICAL:
            p = raw['components'][name]
            if not 0 < p['as_of_ms'] <= p['fetched_at_ms'] <= snapshot['cutoff_ms']:
                raise ValueError('Invalid registration admission evidence')
            if not p['fetched_at_ms'] <= p['expires_at_ms'] <= p['as_of_ms'] + 300000:
                raise ValueError('Invalid registration admission evidence')
        proof = result['evidence']['entry_revalidation']
        if not (proof['status'] == 'VALID' and (not plans.compare_revalidation(snapshot, proof, config))):
            raise ValueError('Invalid registration admission evidence')
        checked = proof['checked_at_ms']
        expiry = min(proof['expires_at_ms'], checked + 60000)
        for name in plans.CRITICAL:
            part = proof['components'][name]
            if not (part['status'] == 'VALID' and 0 < part['as_of_ms'] <= checked):
                raise ValueError('Invalid registration admission evidence')
            if not registered < part['expires_at_ms'] <= part['as_of_ms'] + 300000:
                raise ValueError('Invalid registration admission evidence')
            if not proof['expires_at_ms'] <= part['expires_at_ms']:
                raise ValueError('Invalid registration admission evidence')
        if not snapshot['cutoff_ms'] <= checked <= completed <= registered < expiry:
            raise ValueError('Invalid registration admission evidence')
        if not registered < int(datetime.fromisoformat(result['expires_at_utc']).timestamp() * 1000):
            raise ValueError('Invalid registration admission evidence')
        expected = plans.candidates(snapshot, config)['LONG_BREAKOUT_RETEST' if result['proposed_action'] == 'OPEN_LONG_CANDIDATE' else 'SHORT_BREAKOUT_RETEST']
        for field, key in [('entry_reference', 'entry'), ('stop_loss', 'stop')]:
            if not result[field] == expected[key]:
                raise ValueError('Invalid registration admission evidence')
        if not result['take_profit'][0] == expected['target']:
            raise ValueError('Invalid registration admission evidence')
        if not result['signal_timing'] == expected['timing']:
            raise ValueError('Invalid registration admission evidence')
        value = {'version': VERSION, 'policy_hash': POLICY_HASH, 'research_id': result['research_id'], 'snapshot_id': snapshot['snapshot_id'], 'instrument': snapshot['symbol'], 'candidate_id': 'LONG_BREAKOUT_RETEST' if expected['direction'] == 'LONG' else 'SHORT_BREAKOUT_RETEST', 'result_hash': digest(result), 'conditions_hash': digest(expected), 'validation_hash': digest(proof), 'registered_ms': registered, 'checked_ms': checked, 'expires_ms': expiry, 'execution_enabled': False, 'allow_new_risk': False, 'simulation_admitted': False}
        return {**value, 'proof_hash': digest(value)}
    except (AssertionError, ValueError, KeyError, TypeError, IndexError, ArithmeticError):
        return None

def evidence_valid(evidence, snapshot, registered):
    """Only exactly reproducible natural ageing of the six live components is exempt.

    This is historical qualification at registration, not current entry evidence.
    Other stale/invalid evidence remains a rejection. No saved object is changed.
    """
    effective = derivatives.evidence_view(snapshot['derivatives'], registered)

    def walk(node, path=()):
        if isinstance(node, dict):
            if path in {('derivatives_at_registration',), ('derivatives_snapshot.json','derivatives')} and node == effective:
                node = copy.deepcopy(node)
                for name in plans.CRITICAL:
                    p = node['components'][name]
                    if p['status'] == 'STALE_DATA':
                        p['status'] = 'VALID'
                        p['issues'] = []
            return {k: walk(v, path+(k,)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, path+(str(i),)) for i,v in enumerate(node)]
        return node
    return not any((s in __import__('json').dumps(walk({k:v for k,v in evidence.items() if k!=KEY})) for s in ('STALE_DATA', 'RESEARCH_INVALID')))

def verify(record):
    proof = record.quality_evidence.get(KEY)
    if not proof:
        return False
    core = record.quality_evidence.get('market_snapshot', {})
    expected = build(core, record.original_result, record.config, record.completed_ms, record.registered_ms)
    return bool(expected and proof == expected and evidence_valid(record.quality_evidence, core, record.registered_ms))
