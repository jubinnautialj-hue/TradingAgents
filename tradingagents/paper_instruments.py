"""R1.3 opt-in, instrument-scoped adapters over the unchanged native F3 engine.

No source rewriting, module-global patching, instrument impersonation or alternate
financial engine: FunctionType binds the ORIGINAL code objects to private dependency
namespaces. Legacy BTC uses its original functions, policy and ledger. Non-BTC
books have an explicit admission-extension policy and separate code fingerprints.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from types import FunctionType, SimpleNamespace
from typing import Literal

from pydantic import Field, model_validator

from tradingagents import forward_paper as native, forward_tracking as tracking
from tradingagents import automation, okx_diagnostics as diagnostics
from tradingagents.agents.perpetual_schemas import ResearchModel
from tradingagents.forward_validation import atomic, digest, read
from tradingagents.dataflows.files import locked
from tradingagents.research_budget import ResearchBudgetExceeded

VERSION = 'forward-paper-instruments-v1'
SPEC_FIELDS = ('instId', 'instType', 'instFamily', 'state', 'settleCcy', 'ctType',
               'ctValCcy', 'quoteCcy', 'ctVal', 'ctMult', 'minSz', 'lotSz', 'tickSz')
CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DEFAULT_CONFIG = native.ROOT / 'docs/paper_instruments_r13.json'


def specification(info, symbol):
    native.okx.validate_instrument(info, symbol)
    native.deriv.multiplier(info)  # ctMult is optional/empty => official unit multiplier 1
    return {key: info.get(key) for key in SPEC_FIELDS}


class Certificate(ResearchModel):
    instrument: str
    fetched_ms: int = Field(gt=0)
    source: Literal['/api/v5/public/instruments'] = '/api/v5/public/instruments'
    specification: dict
    public_evidence_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    scope: Literal['CURRENT_PUBLIC_SPEC_NOT_HISTORICAL_OR_LIVE_EXECUTION'] = 'CURRENT_PUBLIC_SPEC_NOT_HISTORICAL_OR_LIVE_EXECUTION'

    @model_validator(mode='after')
    def units(self):
        if specification(self.specification, self.instrument) != self.specification:
            raise ValueError('Incomplete/noncanonical instrument specification')
        return self


class InstrumentProfile(ResearchModel):
    instrument: str
    enabled: bool = False
    certificate: Certificate
    taker_fee_bps: Literal[5] = 5
    slippage_bps: Literal[2] = 2
    cost_scope: Literal['LOCAL_PAPER_ASSUMPTION_NOT_ACCOUNT_FEE'] = 'LOCAL_PAPER_ASSUMPTION_NOT_ACCOUNT_FEE'

    @model_validator(mode='after')
    def ownership(self):
        if self.instrument != self.certificate.instrument:
            raise ValueError('Cross-instrument certificate')
        return self


class InstrumentConfig(ResearchModel):
    version: Literal['forward-paper-instruments-v1'] = VERSION
    parent_policy_hash: Literal['e29998a373c1295791116b026a04f18c4067f5c14a1352cfad2a4b93066dbd64'] = native.POLICY_HASH
    profiles: list[InstrumentProfile] = Field(min_length=1, max_length=20)
    simulation_admitted: Literal[False] = False
    allow_new_risk: Literal[False] = False
    execution_enabled: Literal[False] = False

    @model_validator(mode='after')
    def unique(self):
        symbols = [p.instrument for p in self.profiles]
        if len(set(symbols)) != len(symbols): raise ValueError('Duplicate paper profiles')
        if 'BTC-USDT-SWAP' not in symbols: raise ValueError('Explicit legacy BTC profile required')
        return self


class ExtensionProtocol(native.PaperProtocol):
    version: Literal['forward-paper-instruments-v1'] = VERSION
    parent_policy_hash: str
    instrument_admission: dict


class ExtensionEvent(native.PaperEvent):
    version: Literal['forward-paper-instruments-v1'] = VERSION


class ExtensionJournal(ResearchModel):
    policy: ExtensionProtocol
    code_hash: str
    namespace: Literal['PRODUCTION', 'FIXTURE']
    events: list[ExtensionEvent] = Field(max_length=20000)


class ExtensionReport(native.PaperReport):
    version: Literal['forward-paper-instruments-v1']


def bind(function, namespace):
    """Dependency binding only: bytecode/defaults/closures remain unchanged."""
    result = FunctionType(function.__code__, namespace, function.__name__,
                          function.__defaults__, function.__closure__)
    result.__kwdefaults__ = function.__kwdefaults__
    result.__doc__ = function.__doc__
    return result


def funding_verifier(symbol):
    """Same D.3.1 page/coverage rules, explicitly bound to the actual symbol."""
    native.okx.validate_symbol(symbol)
    deps = dict(vars(diagnostics))
    deps['bt'] = SimpleNamespace(SYMBOL=symbol, HOUR=native.HOUR, digest=digest)
    return bind(diagnostics.verify_funding_evidence, deps)


def engine(profile, registry_root):
    """Per-source/per-instrument scope; never changes the original module."""
    profile = profile.model_copy(deep=True)
    symbol = profile.instrument
    legacy = symbol == 'BTC-USDT-SWAP'
    registry_path = Path(registry_root).resolve()
    deps = dict(vars(native))

    class ScopedPath(type(Path())):
        def glob(self, pattern, *args, **kwargs):
            paths = super().glob(pattern, *args, **kwargs)
            if self.resolve() != registry_path or pattern != '*.json': return paths
            # Load+verify before filtering: forged instrument metadata must not
            # make a record disappear from the instrument view silently.
            registry = automation.readonly_registry(registry_path)
            def selected():
                count = 0
                for path in paths:
                    if len(path.stem) != 64: continue
                    count += 1
                    if count > 2000: raise ValueError('Instrument registry enumeration cap')
                    if registry.load(path.stem).instrument == symbol: yield path
            return selected()

    deps['Path'] = ScopedPath
    # Native Registry constructor may create files; all source reads are read-only.
    deps['Registry'] = automation.readonly_registry
    if legacy:
        # BTC policy, financial functions, Ledger and code hash stay byte-for-byte.
        deps['scan'] = bind(native.scan, deps)
        deps['instrument_allowed'] = lambda candidate: profile.enabled and candidate == symbol
        if not profile.enabled:
            def disabled_admission(record, trigger, now):
                return sorted(set(native.admission(record, trigger, now) + ['PAPER_INSTRUMENT_DISABLED']))
            def disabled_fill(*args, **kwargs):
                raise ValueError('Paper instrument disabled')
            deps.update(admission=disabled_admission, fill=disabled_fill,
                        create_state=bind(native.create_state, deps))
        return SimpleNamespace(**deps)

    cert = profile.certificate
    admission_profile = profile.model_dump(exclude={'enabled'})
    protocol = ExtensionProtocol(**{**native.POLICY, 'version': VERSION},
                                 parent_policy_hash=native.POLICY_HASH,
                                 instrument_admission=admission_profile)
    verify = funding_verifier(symbol)
    deps.update(VERSION=VERSION, PROTOCOL=protocol, POLICY=protocol.model_dump(),
                POLICY_HASH=digest(protocol.model_dump()),
                CODE_HASH=digest({'native': native.CODE_HASH, 'adapter': CODE_HASH,
                                  'timely_adapter': hashlib.sha256((native.ROOT/'tradingagents/paper_timing.py').read_bytes()).hexdigest(),
                                  'funding_validator': hashlib.sha256(Path(diagnostics.__file__).read_bytes()).hexdigest(),
                                  'profile': admission_profile}),
                PaperEvent=ExtensionEvent, PaperJournal=ExtensionJournal,
                PaperReport=ExtensionReport, verify_funding_evidence=verify)
    # Copy functions into a PRIVATE globals dictionary. No global monkeypatch and
    # no textual code substitutions; rules are the native original code objects.
    for name, value in vars(native).items():
        if isinstance(value, FunctionType) and value.__module__ == native.__name__:
            deps[name] = bind(value, deps)

    native_load = bind(native.Ledger.load, deps)
    class ScopedLedger(native.Ledger):
        def load(self):
            events = native_load(self)
            for state in self.states(events).values():
                if (state.identity.get('instrument') != symbol or
                    state.identity.get('paper_protocol_hash') != deps['POLICY_HASH'] or
                    specification(state.info, symbol) != cert.specification):
                    raise ValueError('Cross-instrument paper ledger state')
            return events
        append = bind(native.Ledger.append, deps)

    deps['Ledger'] = ScopedLedger

    def check_info(info):
        actual = specification(info, symbol)
        if actual != cert.specification: raise ValueError('Certified instrument specification changed')
        return actual

    def admission(record, trigger, now):
        reasons = native.admission(record, trigger, now)
        # Only this explicit legacy whitelist constraint is superseded. Every
        # original time, provenance, observation and false-risk guard is retained.
        reasons = [r for r in reasons if r != 'PAPER_FUNDING_AUDIT_BTC_ONLY']
        if not profile.enabled: reasons.append('PAPER_INSTRUMENT_DISABLED')
        if record.instrument != symbol: reasons.append('PAPER_INSTRUMENT_SCOPE_MISMATCH')
        if cert.fetched_ms > now: reasons.append('FUTURE_SPECIFICATION_CERTIFICATE')
        try: check_info(record.quality_evidence['market_snapshot']['instrument'])
        except (ValueError, KeyError): reasons.append('CERTIFIED_SPECIFICATION_MISMATCH')
        return sorted(set(reasons))

    deps['admission'] = admission
    deps['instrument_allowed'] = lambda candidate: profile.enabled and candidate == symbol
    native_create = deps['create_state']

    def create_state(record, trigger, proof, now, namespace='PRODUCTION'):
        state = native_create(record, trigger, proof, now, namespace)
        state.identity.update(instrument_certificate=digest(cert.model_dump()),
                              parent_paper_policy_hash=native.POLICY_HASH,
                              instrument_admission=admission_profile)
        return state

    deps['create_state'] = create_state

    # Native fill's single legacy-symbol argument is a validation dependency,
    # not market data. Validate the UNCHANGED actual info under its bound symbol.
    # Never change instId/ctVal/lotSz or pass a fake BTC contract into arithmetic.
    def validate_bound(info, legacy_argument):
        if legacy_argument != 'BTC-USDT-SWAP': raise ValueError('Unexpected native validator contract')
        check_info(info)

    fill_deps = dict(deps)
    fill_deps['okx'] = SimpleNamespace(validate_instrument=validate_bound)
    native_fill = bind(native.fill, fill_deps)

    def fill(state, opening, now, equity=10000):
        if not profile.enabled: raise ValueError('Paper instrument disabled')
        if state.identity.get('instrument') != symbol or state.identity.get('paper_protocol_hash') != deps['POLICY_HASH']:
            raise ValueError('Paper instrument/policy ownership mismatch')
        proof = state.revalidation.get('instrument_specification', {})
        if (proof.get('specification') != cert.specification or proof.get('source') != cert.source
            or proof.get('fetched_ms', 0) > state.revalidation['completed_ms']
            or proof.get('fetched_ms', 0) < state.revalidation['checked_ms'] - 60000
            or proof.get('fingerprint') != digest({k:v for k,v in proof.items() if k != 'fingerprint'})):
            raise ValueError('Missing or stale current specification provenance')
        return native_fill(state, opening, now, equity)

    deps['fill'] = fill

    class Market(native.Market):
        def bars(self, requested, start, end):
            if requested != symbol: raise ValueError('Cross-instrument candle request')
            batch = super().bars(requested, start, end)
            for candle in batch['bars']:
                if not math.isclose(candle['volume_base'], candle['volume_contracts']*native.deriv.multiplier(cert.specification),
                                    rel_tol=1e-6, abs_tol=1e-8):
                    raise ValueError('Candle contract/base units mismatch')
            return batch

        def opening(self, requested, target):
            if requested != symbol: raise ValueError('Cross-instrument opening request')
            return super().opening(requested, target)

        def refresh(self, record, trigger, now):
            # Capture the existing revalidate_live instrument GET, not a duplicate.
            captured = {}
            client = self.client
            class RecordingClient:
                get = client.get
                def instrument(self, requested):
                    if requested != symbol: raise ValueError('Cross-instrument revalidation')
                    info = client.instrument(requested)
                    captured.update(specification=check_info(info), source=cert.source,
                                    fetched_ms=native.okx.utc_ms())
                    return info
            scoped = native.Market(RecordingClient(), self.limits)
            result = scoped.refresh(record, trigger, now)
            if not captured: raise ValueError('No live contract specification')
            captured['fingerprint'] = digest(captured)
            result['instrument_specification'] = captured
            return result

        def funding(self, requested, start, end):
            if requested != symbol: raise ValueError('Cross-instrument funding request')
            server = self.now(); initial = cursor = server + 1; pages = []
            for _ in range(self.limits.max_pages):
                rows = self.client.get(native.deriv.FUNDING_HISTORY,
                                       {'instId': symbol, 'after': str(cursor), 'limit': '100'})
                if not rows: break
                pages.append({'after': cursor, 'rows': rows, 'hash': digest(rows)})
                cursor = min(int(r['fundingTime']) for r in rows)
                if cursor <= start: break
            evidence = {'path': native.deriv.FUNDING_HISTORY, 'initial_cursor': initial,
                        'pages': pages, 'server_ms': server}
            records = native.deriv.parse_funding_history([r for p in pages for r in p['rows']], symbol, server)['records']
            verify(records, evidence, start, end)
            return {'records': records, 'evidence': evidence, 'available_ms': native.okx.utc_ms(),
                    'source': native.deriv.FUNDING_HISTORY}

    deps['Market'] = Market
    return SimpleNamespace(**deps)


def load_config(path):
    return InstrumentConfig.model_validate(read(path))


def routes(config, sources):
    result = []
    for source in sources:
        instruments_root = Path(source['f3']).parent / 'instruments-r13' / source['name']
        configured = {p.instrument for p in config.profiles}
        if instruments_root.exists() and any(p.is_dir() and p.name not in configured for p in instruments_root.iterdir()):
            raise ValueError('Existing instrument book omitted; retain disabled profile for audit/settlement')
        for profile in config.profiles:
            # Disabled profiles remain readable/settleable; no new admission/fill.
            core = engine(profile, source['registry'])
            output = Path(source['f3']) if profile.instrument == 'BTC-USDT-SWAP' else instruments_root / profile.instrument
            result.append({**source, 'name': source['name'] + '/' + profile.instrument,
                           'f3': str(output), '_paper_engine': core, 'instrument': profile.instrument,
                           'enabled': profile.enabled})
    return result


def public_probe(symbols=('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP')):
    """Current specifications + closed candles + ACTUAL settled rate samples only."""
    limits = tracking.ScanLimits(max_requests=20, max_seconds=60)
    client = tracking.PublicBudgetClient(limits)
    server = native.Market(client, limits).now(); end = server // native.HOUR * native.HOUR
    results = []
    for symbol in symbols:
        info = client.instrument(symbol); fetched = native.okx.utc_ms()
        batch = tracking.Collector(client, limits).get(symbol, end - 2*native.HOUR, end)
        for candle in batch['bars']:
            if not math.isclose(candle['volume_base'], candle['volume_contracts']*native.deriv.multiplier(info),
                                rel_tol=1e-6, abs_tol=1e-8):
                raise ValueError('Public candle contract/base units mismatch')
        rows = client.get(native.deriv.FUNDING_HISTORY, {'instId': symbol, 'limit': '10'})
        parsed = native.deriv.parse_funding_history(rows, symbol, server)['records']
        if len(parsed) < 2: raise ValueError('Insufficient settled funding samples')
        proof = {'path': native.deriv.FUNDING_HISTORY, 'initial_cursor': server+1, 'server_ms': server,
                 'pages': [{'after': server+1, 'rows': rows, 'hash': digest(rows)}]}
        intervals = funding_verifier(symbol)(parsed, proof, parsed[0]['ts'], parsed[-1]['ts'])
        spec = specification(info, symbol)
        evidence = {'instrument': symbol, 'specification': spec, 'fetched_ms': fetched,
                    'closed_bars': batch, 'funding_records': parsed, 'funding_evidence': proof,
                    'observed_funding_intervals_hours': intervals}
        certificate = Certificate(instrument=symbol, specification=spec, fetched_ms=fetched,
                                  public_evidence_hash=digest(evidence))
        results.append({'evidence': evidence, 'certificate': certificate.model_dump()})
    return {'version': VERSION, 'scope': 'UNBOUND_PUBLIC_VERIFICATION_NOT_A_FORWARD_FILL',
            'results': results, 'logical_get_calls': client.calls, 'llm_calls': 0,
            'fills': 0, 'execution_enabled': False}


def _run_phase(cfg, config, phase):
    """Reuse R12 sources, pause/locks, budgets. Research/observe stay original."""
    if phase in {'research', 'observe'}: return automation.dispatch(cfg, phase)
    if automation.paused(cfg, phase): return {'status': 'PAUSED', 'execution_enabled': False}
    root = automation.expand(cfg.state_root)
    if phase == 'paper' and automation.reserved(cfg, native.okx.utc_ms()):
        return {'status': 'TIMELY_RESERVED', 'execution_enabled': False}
    with locked(root / 'paper.phase', wait=False) as held:
        if not held: return {'status': 'BUSY', 'execution_enabled': False}
        index = automation.discover(cfg, persist=True)
        sources = [s for s in index['sources'] if s['status'] == 'READY']
        scoped = routes(config, sources)
        if phase == 'timely':
            from tradingagents.paper_timing import warm_window
            return warm_window(cfg, scoped)
        started = time.monotonic(); results = []
        for source in scoped:
            remaining = cfg.schedule.paper_timeout - (time.monotonic() - started)
            if remaining < 5:
                results.append({'instrument': source['instrument'], 'status': 'PHASE_TIME_BUDGET'}); continue
            if not Path(source['f2']).is_dir():
                results.append({'instrument': source['instrument'], 'status': 'WAITING_FOR_F2'}); continue
            try:
                limits = automation.limits_for(cfg, min(remaining, cfg.schedule.paper_timeout / max(1, len(scoped))), len(scoped))
                value = source['_paper_engine'].scan(source['registry'], source['f2'], source['f3'], limits)
                results.append({'source': source['name'], 'instrument': source['instrument'],
                                'ledger': source['f3'], 'research': value.get('research'),
                                'paper': value.get('paper'), 'status': value.get('status', 'BUSY')})
            except (ValueError, KeyError, OSError, tracking.BudgetExhausted, native.okx.VendorUnavailableError) as exc:
                results.append({'instrument': source['instrument'], 'status': 'SOURCE_UNAVAILABLE', 'error_type': type(exc).__name__})
        return {'version': VERSION, 'results': results, 'execution_enabled': False,
                'capital_scope': 'INDEPENDENT_PER_INSTRUMENT_BOOKS_NOT_ONE_SHARED_PORTFOLIO'}


def run_phase(cfg, config, phase):
    if phase in {'research', 'observe'}: return automation.dispatch(cfg, phase)
    if phase not in {'paper', 'timely'}: raise ValueError('Invalid paper phase')
    from tradingagents.research_budget import Meter, guards
    from uuid import uuid4
    root = automation.expand(cfg.state_root)/'r13_runs'
    root.mkdir(parents=True, exist_ok=True)
    name = str(native.okx.utc_ms())+'_'+uuid4().hex
    timeout = cfg.schedule.timely_timeout if phase == 'timely' else cfg.schedule.paper_timeout
    meter = Meter(root/(name+'.meter.json'), 0, cfg.schedule.timely_http_cap, timeout)
    started = native.okx.utc_ms()
    try:
        with guards(meter): value = _run_phase(cfg, config, phase)
    except Exception as exc:
        atomic(root/(name+'.json'), {'phase':phase, 'started_ms':started,
               'completed_ms':native.okx.utc_ms(), 'status':'COMPONENT_UNAVAILABLE',
               'error_type':type(exc).__name__, 'profile_hash':digest(config.model_dump()),
               'meter':meter.value, 'execution_enabled':False})
        raise
    value.update(profile_hash=digest(config.model_dump()), started_ms=started,
                 completed_ms=native.okx.utc_ms(), meter=meter.value)
    atomic(root/(name+'.json'), value)
    return value


def export(cfg, config, config_path, profile_path, directory):
    """Export-only R12 cadence, with ONLY paper/timely routed to R13."""
    from tradingagents.automation_schedule import export as base_export
    import xml.etree.ElementTree as ET
    result = base_export(cfg, config_path, directory)
    namespace = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    expected = digest(config.model_dump())
    for filename in result['files']:
        path = Path(filename)
        if not any(path.name == 'r12-'+phase+suffix for phase in ('paper','timely') for suffix in ('.xml','.service')):
            continue
        phase = 'timely' if 'timely' in path.name else 'paper'
        if path.suffix == '.xml':
            tree = ET.parse(path)
            tree.find('.//t:Arguments',namespace).text = subprocess.list2cmdline([
                '-m','tradingagents.paper_instruments',phase,'--config',str(Path(config_path).resolve()),
                '--instruments',str(Path(profile_path).resolve()),'--expected-profile-hash',expected,
                '--expected-config-hash',digest(cfg.model_dump())])
            tree.write(path, encoding='utf-16', xml_declaration=True)
        else:
            text = path.read_text(encoding='utf-8')
            text = text.replace('-m tradingagents.automation '+phase,
                                '-m tradingagents.paper_instruments '+phase)
            text = text.replace('\nTimeoutStartSec=', ' --instruments "%h/TradingAgents/docs/paper_instruments_r13.json" --expected-profile-hash '+expected+'\nTimeoutStartSec=')
            path.write_text(text, encoding='utf-8')
    result.update(instrument_profile_hash=expected, protocol=VERSION, installed=False)
    atomic(Path(result['files'][0]).parent/'manifest.json', result)
    return result


def status(cfg, config):
    index = automation.discover(cfg)
    sources = [s for s in index['sources'] if s['status'] == 'READY']
    results = []
    for source in routes(config, sources):
        core = source['_paper_engine']
        # Read-only construction, same original ledger validators.
        ledger = object.__new__(core.Ledger)
        ledger.root = Path(source['f3']) / 'production'; ledger.path = ledger.root / 'ledger.json'; ledger.namespace = 'PRODUCTION'
        events = ledger.load()
        registry = automation.readonly_registry(source['registry'])
        records = [r for r in registry.registrations() if r.instrument == source['instrument']]
        results.append({'source': source['name'], 'instrument': source['instrument'], 'enabled': source['enabled'],
                        'ledger': str(ledger.path), 'report': core.report(records, events)})
    return {'version': VERSION, 'results': results, 'execution_enabled': False,
            'unapproved_registrations': [{'registration_id':rid, 'instrument':record['instrument'],
                                         'reason':'NO_EXPLICIT_INSTRUMENT_PROFILE'}
                                        for rid,record in index['records'].items()
                                        if record['instrument'] not in {p.instrument for p in config.profiles}],
            'capital_scope': 'INDEPENDENT_PER_INSTRUMENT_BOOKS_NOT_ONE_SHARED_PORTFOLIO'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['probe', 'paper', 'timely', 'status', 'export', '_worker'])
    parser.add_argument('--config', type=Path, default=automation.ROOT/'docs/automation_r12.json')
    parser.add_argument('--instruments', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--phase', choices=['paper', 'timely'])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--expected-profile-hash')
    parser.add_argument('--expected-config-hash')
    args = parser.parse_args()
    try:
        if args.command == 'probe':
            value = public_probe()
            if args.output: atomic(args.output, value)
        else:
            cfg = automation.AutomationConfig.model_validate(read(args.config)); automation.paths(cfg)
            if args.expected_config_hash and args.expected_config_hash != digest(cfg.model_dump()):
                raise ValueError('Automation configuration changed; export/review again')
            profiles = load_config(args.instruments)
            if args.expected_profile_hash and args.expected_profile_hash != digest(profiles.model_dump()):
                raise ValueError('Paper admission config changed; explicit review required')
            if args.command == 'status': value = status(cfg, profiles)
            elif args.command == 'export':
                if args.output is None: raise ValueError('Explicit new export directory required')
                if args.output.exists(): raise ValueError('Do not overwrite previous schedule exports')
                value = export(cfg, profiles, args.config, args.instruments, args.output)
            elif args.command == '_worker':
                if not args.phase: raise ValueError('Missing phase')
                value = run_phase(cfg, profiles, args.phase)
            else:
                command = [sys.executable, '-m', 'tradingagents.paper_instruments', '_worker',
                           '--phase', args.command, '--config', str(args.config.resolve()),
                           '--instruments', str(args.instruments.resolve()),
                           '--expected-profile-hash', digest(profiles.model_dump()),
                           '--expected-config-hash', digest(cfg.model_dump())]
                timeout = cfg.schedule.timely_timeout if args.command == 'timely' else cfg.schedule.paper_timeout
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, start_new_session=sys.platform != 'win32')
                try:
                    output, _ = proc.communicate(timeout=timeout)
                    value = json.loads(output)
                except subprocess.TimeoutExpired:
                    automation.kill_owned(proc)
                    value = {'status': 'PHASE_TIMEOUT', 'execution_enabled': False}
        print(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False))
    except (ValueError, KeyError, OSError, ResearchBudgetExceeded, tracking.BudgetExhausted, native.okx.VendorUnavailableError) as exc:
        print(json.dumps({'status': 'R13_UNAVAILABLE', 'error_type': type(exc).__name__, 'execution_enabled': False}))
        raise SystemExit(2)


if __name__ == '__main__': main()
