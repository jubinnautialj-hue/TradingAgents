"""Bounded, public-only diagnostics around the unchanged R14 quote probe."""
from __future__ import annotations
import argparse
import json
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests
from tradingagents import forward_tracking as f2, forward_paper as f3
from tradingagents import ubuntu_operations as operations

VERSION = 'public-quote-diagnostics-d321-v1'
SYMBOLS = ('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP')
SAFE_ERRORS = {'Opening not observed on time', 'Opening not verifiable',
               'Clock jumped before quote', 'Clock jumped during quote',
               'Clock jumped during probe', 'Clock offset or uncertainty too large',
               'Invalid numeric field', 'Diagnostic request unavailable', 'Diagnostic window exhausted'}


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def numeric(value):
    text = str(value)
    return text if len(text) <= 48 and re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', text) else 'INVALID_NUMERIC'


def summary(rows):
    if not isinstance(rows, list): return {'container': type(rows).__name__}
    result = {'container': 'list', 'count': len(rows), 'rows': []}
    for row in rows[:2]:
        item = {'type': type(row).__name__}
        if isinstance(row, list):
            item['length'] = len(row)
            for key, index in [('timestamp', 0), ('open', 1), ('volume_contracts', 5), ('confirm', 8)]:
                if len(row) > index: item[key] = numeric(row[index])
        result['rows'].append(item)
    return result


def failure(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    return {'type': type(exc).__name__,
            'message': str(exc) if str(exc) in SAFE_ERRORS else 'NON_ALLOWLISTED_MESSAGE_OMITTED',
            'locations': [{'file': Path(x.filename).name, 'function': x.name, 'line': x.lineno} for x in frames[-4:]]}


class DiagnosticClient(f2.PublicBudgetClient):
    """Observe actual sends without changing retries, pacing, auth or parser."""
    def __init__(self, limits):
        super().__init__(limits)
        self.records = []
        self.last_rows = None
        self.window_end_ms = None
        self.request_cap = 7
        self.single_http_attempt = False
        self.event_sink = None

    def get(self, path, params=None):
        if path not in {'/api/v5/public/time', '/api/v5/public/instruments', '/api/v5/market/candles'}:
            raise ValueError('Diagnostic path not allowed')
        if len(self.records) >= self.request_cap: raise ValueError('Diagnostic record limit')
        self.last_rows = None
        start = f2.okx.utc_ms()
        record = {'path': path, 'instrument': (params or {}).get('instId'), 'request_started_ms': start,
                  'request_started_utc': stamp(start), 'http_attempts': [],
                  'query': {k: (params or {}).get(k) for k in ('instId', 'bar', 'limit') if k in (params or {})}}
        send = requests.Session.send
        def observe(session, request, **kwargs):
            before = f2.okx.utc_ms()
            if self.single_http_attempt:
                remaining = (self.window_end_ms - before) / 1000 if self.window_end_ms else 1.0
                if remaining < .01 or record['http_attempts']: raise DiagnosticRequestError('Diagnostic window exhausted')
                budget = min(1.0, remaining)
                kwargs['timeout'] = (max(.001, budget * .35), max(.001, budget * .65))
            attempt = {'request_started_ms': before}
            record['http_attempts'].append(attempt)
            if self.event_sink: self.event_sink({'kind':'HTTP_SEND','instrument':record['instrument'], 'path':path, **attempt})
            try:
                response = send(session, request, **kwargs)
                attempt['http_status'] = response.status_code
                if self.single_http_attempt:
                    if response.status_code != 200: raise DiagnosticRequestError('Diagnostic request unavailable')
                    body = response.json()
                    if not isinstance(body, dict) or str(body.get('code')) != '0':
                        raise DiagnosticRequestError('Diagnostic request unavailable')
                return response
            except Exception as exc:
                attempt['error_type'] = type(exc).__name__
                if self.single_http_attempt: raise DiagnosticRequestError('Diagnostic request unavailable') from None
                raise
            finally:
                end = f2.okx.utc_ms()
                attempt.update(response_received_or_failed_ms=end, elapsed_ms=end-before)
                if self.event_sink: self.event_sink({'kind':'HTTP_FINISH','instrument':record['instrument'], 'path':path, **attempt})
        try:
            with patch.object(requests.Session, 'send', observe):
                rows = super().get(path, params)
            self.last_rows = rows
            if path.endswith('/candles'): record['response_summary'] = summary(rows)
            else: record['response_summary'] = {'container': type(rows).__name__, 'count': len(rows)}
            record['status'] = 'RECEIVED'
            return rows
        except Exception as exc:
            record.update(status='FAILED', error=failure(exc))
            raise
        finally:
            end = f2.okx.utc_ms()
            record.update(completed_ms=end, completed_utc=stamp(end), elapsed_ms=end-start,
                          actual_http_attempts=len(record['http_attempts']),
                          retried=len(record['http_attempts']) > 1)
            self.records.append(record)
            if self.event_sink: self.event_sink({'kind':'REQUEST_RESULT', **record})


def shape_check(rows, target):
    if not isinstance(rows, list): return {'status':'INVALID','reason':'NO_CANDLE_RESPONSE'}
    matches = [r for r in rows if isinstance(r, list) and len(r) == 9 and str(r[0]) == str(target)]
    if len(matches) != 1: return {'status': 'INVALID', 'reason': 'TARGET_BAR_NOT_UNIQUE_OR_MISSING'}
    row = matches[0]
    try:
        price = f3.deriv.number(row[1], positive=True)
        volume = f3.deriv.number(row[5], nonnegative=True)
        valid = str(row[8]) == '0' and volume > 0
        return {'status': 'VALID' if valid else 'INVALID', 'open': price,
                'volume_contracts': volume, 'confirm': numeric(row[8]),
                'scope': 'SHAPE_ONLY_NOT_15_SECOND_ACCEPTANCE'}
    except (ValueError, TypeError):
        return {'status': 'INVALID', 'reason': 'NUMERIC_VALIDATION_FAILED'}


def execute(mode, plan=None):
    client = DiagnosticClient(f2.ScanLimits(max_requests=7 if mode == 'check' else 4, max_seconds=120))
    value = {'version': VERSION, 'mode': mode, 'scope': 'DIAGNOSTIC_ONLY_NOT_F1_F2_F3',
             'execution_enabled': False, 'allow_new_risk': False, 'simulation_admitted': False,
             'paper_trades': 0, 'quotes': []}
    original = f3.Market.opening
    def opening(market, symbol, target):
        item = {'instrument': symbol, 'target_ms': target, 'validation_started_ms': f2.okx.utc_ms()}
        value['quotes'].append(item)
        try:
            observed = original(market, symbol, target)
            item.update(status='NATIVE_OPENING_ACCEPTED', observation=observed)
            return observed
        except Exception as exc:
            item.update(status='NATIVE_OPENING_REJECTED', error=failure(exc))
            raise
        finally:
            item['validation_completed_ms'] = f2.okx.utc_ms()
    try:
        with patch.object(f3.Market, 'opening', opening):
            if mode == 'timed':
                value['native_result'] = operations.run_probe(plan, client=client)
                value['status'] = value['native_result']['status']
            else:
                value['clock_check'] = operations.clock_observation(client)
                target = value['clock_check']['server_ms'] // f3.HOUR * f3.HOUR
                market = f3.Market(client, client.limits)
                for symbol in SYMBOLS:
                    try:
                        info = client.instrument(symbol)  # unchanged contract identity/unit validation
                    except Exception as exc:
                        value['quotes'].append({'instrument':symbol,'status':'CONTRACT_CHECK_FAILED','error':failure(exc),
                                                'shape':{'status':'INVALID','reason':'CONTRACT_CHECK_FAILED'}})
                        continue
                    try: market.opening(symbol, target)
                    except Exception: pass
                    item = value['quotes'][-1]
                    item['contract'] = {k: info.get(k) for k in ('instId','state','ctVal','ctValCcy','ctMult','lotSz','minSz','tickSz')}
                    item['shape'] = shape_check(client.last_rows, target) if client.last_rows is not None else {'status':'INVALID','reason':'NO_CANDLE_RESPONSE'}
                value['status'] = ('STRUCTURES_VALID_NOT_TIMING_ACCEPTANCE' if
                    all(q['shape']['status'] == 'VALID' for q in value['quotes']) else 'STRUCTURE_CHECK_FAILED')
    except Exception as exc:
        value.update(status='DIAGNOSTIC_FAILED', error=failure(exc))
    value['requests'] = client.records
    return value


class DiagnosticRequestError(RuntimeError):
    """Only the versioned visibility experiment disables transport retries."""


class WindowDeadline(BaseException):
    """Interrupt only the standalone Linux diagnostic process at the cutoff."""


class WindowGuard:
    def __init__(self, end_ms, clock):
        self.end_ms=end_ms;self.clock=clock;self.enabled=False

    def arm(self):
        import signal,threading,sys
        if sys.platform!='linux' or threading.current_thread()!=threading.main_thread():return
        if signal.getitimer(signal.ITIMER_REAL)[0]:raise RuntimeError('Existing alarm cannot be replaced')
        self.previous=signal.getsignal(signal.SIGALRM)
        def stop(*args):raise WindowDeadline('Diagnostic window exhausted')
        signal.signal(signal.SIGALRM,stop)
        signal.setitimer(signal.ITIMER_REAL,max(.001,(self.end_ms-self.clock())/1000))
        self.enabled=True

    def close(self):
        if self.enabled:
            import signal
            signal.setitimer(signal.ITIMER_REAL,0)
            signal.signal(signal.SIGALRM,self.previous)


VISIBILITY_POLICY = {'version':'d321-hour-visibility-v1', 'offsets_ms':[1000,5000,9000],
                     'window_ms':15000, 'max_http_attempts':10, 'per_symbol_queries':3,
                     'single_http_attempt_per_query':True, 'socket_budget_seconds':1.0}


def visibility(plan_path, *, client=None, clock=None, sleep=time.sleep, monotonic=time.monotonic):
    """Independent bounded observation, NOT the formal F3 fail-fast workflow.

    First visibility is a sampling upper bound, never the exchange publish time.
    A late response is retained as diagnostics but can never become a success.
    """
    a,_=operations.components(); clock=clock or f2.okx.utc_ms
    path=Path(plan_path);plan=a.r11.unseal(path);target=plan['target_ms']
    if plan.get('plan_id')!=a.digest({k:v for k,v in plan.items() if k!='plan_id'}):raise ValueError('Probe plan fingerprint')
    if (plan.get('version')!='r14-public-timing-probe-v1' or plan.get('scope')!='PROBE_ONLY_NOT_F1_F2_F3'
        or plan.get('instruments')!=list(SYMBOLS) or target%f3.HOUR
        or not 0<plan['armed_ms']<target<=plan['armed_ms']+f3.HOUR
        or any(plan.get(k) is not False for k in ('execution_enabled','allow_new_risk','simulation_admitted'))
        or plan.get('paper_trades')!=0):raise ValueError('Probe scope')
    started=clock()
    if started < plan['expected_dispatch_ms'] or started>=target+15000:raise ValueError('Outside prospective observation schedule')
    key=a.digest({'plan_id':plan['plan_id'],'policy':VISIBILITY_POLICY})
    attempt=path.with_name(key+'.visibility-attempt.json')
    events=path.with_name(key+'.visibility-events.jsonl')
    result=path.with_name(key+'.visibility-result.json')
    with a.locked(attempt,wait=False) as held:
        if not held or attempt.exists():raise ValueError('Visibility attempt already exists or busy')
        a.r11.seal(attempt,{'plan_id':plan['plan_id'],'policy':VISIBILITY_POLICY,'started_ms':started})
        with events.open('x',encoding='utf-8') as stream:
            def sink(event):
                stream.write(json.dumps(event,allow_nan=False)+'\n');stream.flush()
            c=client or DiagnosticClient(f2.ScanLimits(max_requests=10,max_seconds=120))
            c.request_cap=10;c.single_http_attempt=True;c.event_sink=sink
            # Preflight may only consume one bounded HTTP send, not network retries.
            c.window_end_ms=started+1000
            value={'version':VERSION,'policy':VISIBILITY_POLICY,'plan_id':plan['plan_id'],'target_ms':target,
                   'scope':'DIAGNOSTIC_REPEATED_SAMPLING_NOT_F3_ADMISSION', 'paper_trades':0,
                   'execution_enabled':False,'allow_new_risk':False,'simulation_admitted':False,
                   'started_ms':started,'symbols':{s:{'samples':[],'first_visible_received_ms':None,
                     'first_native_accepted_ms':None,'status':'NOT_OBSERVED'} for s in SYMBOLS}}
            original=f3.Market.opening
            guard=WindowGuard(target+15000,clock)
            try:
                guard.arm()
                value['clock_check']=operations.clock_observation(c)
                c.window_end_ms=target+15000
                begin=monotonic();baseline=clock()
                market=f3.Market(c,c.limits)
                for offset in VISIBILITY_POLICY['offsets_ms']:
                    while clock()<target+offset:
                        if abs(clock()-baseline-(monotonic()-begin)*1000)>1000:raise ValueError('Clock jumped during probe')
                        sleep(min(.1,(target+offset-clock())/1000))
                    for symbol in SYMBOLS:
                        row=value['symbols'][symbol]
                        if row['status'] in {'ACCEPTED_DIAGNOSTIC_ONLY','VISIBLE_BUT_INVALID'}:continue
                        if clock()>=target+15000:break
                        if abs(clock()-baseline-(monotonic()-begin)*1000)>1000:raise ValueError('Clock jumped before quote')
                        sample={'round_offset_ms':offset,'validation_started_ms':clock()}
                        c.last_rows=None
                        try:
                            obs=original(market,symbol,target)
                            if clock()>target+15000:raise DiagnosticRequestError('Diagnostic window exhausted')
                            if abs(clock()-baseline-(monotonic()-begin)*1000)>1000:raise ValueError('Clock jumped during quote')
                            sample.update(status='ACCEPTED',observation=obs)
                            row.update(status='ACCEPTED_DIAGNOSTIC_ONLY',first_native_accepted_ms=obs['observed_ms'])
                        except Exception as exc:sample.update(status='REJECTED',error=failure(exc))
                        sample.update(validation_completed_ms=clock(),response_summary=summary(c.last_rows))
                        present=isinstance(c.last_rows,list) and any(isinstance(r,list) and r and str(r[0])==str(target) for r in c.last_rows)
                        if present and clock()<=target+15000:
                            sends=c.records[-1].get('http_attempts',[])
                            row['first_visible_received_ms']=(sends[-1]['response_received_or_failed_ms'] if sends else c.records[-1]['completed_ms'])
                            row['first_visible_verified_ms']=sample['validation_completed_ms']
                            if row['status']!='ACCEPTED_DIAGNOSTIC_ONLY':row['status']='VISIBLE_BUT_INVALID'
                        row['samples'].append(sample);sink({'kind':'SYMBOL_SAMPLE','instrument':symbol,**sample})
                for row in value['symbols'].values():
                    if row['status']=='NOT_OBSERVED':row['status']='NOT_VISIBLE_OR_UNAVAILABLE_WITHIN_SAMPLE_BUDGET'
                value['status']=('ALL_DIAGNOSTIC_QUOTES_VALID_NOT_F3_ADMISSION' if all(
                    row['status']=='ACCEPTED_DIAGNOSTIC_ONLY' for row in value['symbols'].values()) else 'INCOMPLETE_OR_FAILED')
            except (Exception,WindowDeadline) as exc:value.update(status='DIAGNOSTIC_FAILED',error=failure(exc))
            finally:guard.close()
            for row in value['symbols'].values():
                row['first_attempt_accepted']=bool(row['samples'] and row['samples'][0]['status']=='ACCEPTED')
                row['diagnostic_retry_success']=row['status']=='ACCEPTED_DIAGNOSTIC_ONLY' and not row['first_attempt_accepted']
            value.update(finished_ms=clock(),requests=c.records,formal_f3_admission=False,hard_window_alarm_enabled=guard.enabled,
                         observation_window_end_ms=target+15000, sampling_is_discrete=True)
            a.r11.seal(result,value)
            return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['check', 'timed', 'visibility'])
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists(): raise ValueError('New output required')
    if args.mode in {'timed', 'visibility'} and not args.plan: raise ValueError('Prospectively armed native plan required')
    # Reserve first: a crash must not silently retry the same diagnostic attempt.
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps({'status': 'STARTED_NOT_COMPLETED', 'version': VERSION}))
    value = visibility(args.plan) if args.mode == 'visibility' else execute(args.mode, args.plan)
    args.output.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'status': value['status'], 'output': str(args.output), 'paper_trades': 0}))


if __name__ == '__main__': main()

