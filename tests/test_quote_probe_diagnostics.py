import json
import pytest
import requests
from tradingagents import quote_probe_diagnostics as d

ROW = ['1792868400000','100','101','99','100','1','1','100','0']

def test_summary_bounded_and_no_hlc_or_arbitrary_strings():
    r = d.summary([ROW] * 100)
    assert r['count'] == 100 and len(r['rows']) == 2
    assert set(r['rows'][0]) == {'type','length','timestamp','open','volume_contracts','confirm'}
    assert d.numeric('Bearer SECRET') == 'INVALID_NUMERIC'

@pytest.mark.parametrize('change,valid', [({},True),({8:'1'},False),({5:'0'},False),({1:'NaN'},False),({0:'1'},False)])
def test_shape_and_units(change,valid):
    row=ROW.copy()
    for key,value in change.items():row[key]=value
    assert (d.shape_check([row],int(ROW[0]))['status']=='VALID') is valid

def test_error_only_known_messages_and_code_location():
    try: raise ValueError('Authorization: secret https://host/?token=private')
    except ValueError as exc: result=d.failure(exc)
    assert 'secret' not in json.dumps(result)
    assert result['message']=='NON_ALLOWLISTED_MESSAGE_OMITTED' and result['locations']
    try: raise ValueError('Opening not verifiable')
    except ValueError as exc: assert d.failure(exc)['message']=='Opening not verifiable'

def test_native_transport_attempts_and_restore(monkeypatch):
    count=[0]
    def send(self,request,**kwargs):
        count[0]+=1
        response=requests.Response();response.status_code=500 if count[0]==1 else 200
        response._content=json.dumps({'code':'0','data':[ROW]}).encode()
        response.headers['Set-Cookie']='SECRET'
        return response
    monkeypatch.setattr(requests.Session,'send',send)
    monkeypatch.setattr(d.f2.okx.time,'sleep',lambda _:None)
    client=d.DiagnosticClient(d.f2.ScanLimits(max_requests=1,max_seconds=10))
    assert client.get('/api/v5/market/candles',{'instId':'SOL-USDT-SWAP'})==[ROW]
    r=client.records[0]
    assert r['retried'] and r['actual_http_attempts']==2
    assert [x['http_status'] for x in r['http_attempts']]==[500,200]
    assert 'SECRET' not in json.dumps(r)
    assert requests.Session.send is send
    with pytest.raises(ValueError):client.get('/api/v5/trade/order')

@pytest.mark.parametrize('error',[requests.Timeout,requests.ConnectionError])
def test_network_failures_bounded_and_redacted(monkeypatch,error):
    def send(*args,**kwargs):raise error('SECRET')
    monkeypatch.setattr(requests.Session,'send',send)
    monkeypatch.setattr(d.f2.okx.time,'sleep',lambda _:None)
    c=d.DiagnosticClient(d.f2.ScanLimits(max_requests=1,max_seconds=10))
    with pytest.raises(Exception):c.get('/api/v5/market/candles')
    assert c.records[0]['actual_http_attempts']==3
    assert 'SECRET' not in json.dumps(c.records)

def test_check_mode_does_not_fake_opening_window(monkeypatch):
    class Client:
        def __init__(self,limits):self.limits=limits;self.records=[];self.last_rows=None
        def instrument(self,symbol):return {}
        def get(self,path,params=None):self.last_rows=[ROW];return self.last_rows
    monkeypatch.setattr(d,'DiagnosticClient',Client)
    monkeypatch.setattr(d.operations,'clock_observation',lambda client:{'server_ms':int(ROW[0])+120000})
    monkeypatch.setattr(d.f2.okx,'utc_ms',lambda:int(ROW[0])+120000)
    result=d.execute('check')
    assert result['status']=='STRUCTURES_VALID_NOT_TIMING_ACCEPTANCE'
    assert len(result['quotes'])==3 and result['paper_trades']==0
    assert all(q['status']=='NATIVE_OPENING_REJECTED' for q in result['quotes'])
    assert all(q['error']['message']=='Opening not observed on time' for q in result['quotes'])
    assert d.f3.PROTOCOL.opening_delivery_ms==15000

def test_timed_sol_failure_keeps_partial_evidence_and_frozen_rules(tmp_path,monkeypatch):
    from tests.test_ubuntu_r14 import T, Quotes
    now=[T-80000];mono=[0.]
    monkeypatch.setattr(d.f2.okx,'utc_ms',lambda:now[0])
    monkeypatch.setattr(d.operations.components()[0].r11,'utc_ms',lambda:now[0])
    def sleep(seconds):now[0]+=round(seconds*1000);mono[0]+=seconds
    class Client(Quotes):
        def __init__(self,limits):super().__init__(now);self.records=[];self.limits=limits
        def get(self,path,params=None):
            rows=super().get(path,params)
            if (params or {}).get('instId')=='SOL-USDT-SWAP':rows[0][5]='0'
            return rows
    monkeypatch.setattr(d,'DiagnosticClient',Client)
    original=d.operations.run_probe
    monkeypatch.setattr(d.operations,'run_probe',lambda plan,client:original(plan,client=client,
        sleep=sleep,clock=lambda:now[0],monotonic=lambda:mono[0]))
    plan=tmp_path/'plan.json';d.operations.arm_probe(plan,T,now=now[0])
    result=d.execute('timed',plan)
    assert result['status']=='MISSED_OR_UNCONFIRMED'
    assert len(result['native_result']['observations'])==2
    assert result['quotes'][-1]['instrument']=='SOL-USDT-SWAP'
    assert result['quotes'][-1]['error']['message']=='Opening not verifiable'
    assert any(x['function']=='opening' and x['file']=='forward_paper.py' for x in result['quotes'][-1]['error']['locations'])
    assert not result['execution_enabled'] and not result['allow_new_risk'] and not result['simulation_admitted']
    assert result['native_result']['paper_trades']==0

@pytest.fixture
def visibility_case(tmp_path,monkeypatch):
    from tests.test_ubuntu_r14 import T
    now=[T-80000]
    monkeypatch.setattr(d.f2.okx,'utc_ms',lambda:now[0])
    monkeypatch.setattr(d.operations.components()[0].r11,'utc_ms',lambda:now[0])
    plan=tmp_path/'visibility-plan.json';d.operations.arm_probe(plan,T,now=now[0])
    def sleep(seconds):now[0]+=round(seconds*1000)
    class Client:
        def __init__(self,missing=(),broken=(),delay=10,always_missing=False):
            self.limits=d.f2.ScanLimits(max_requests=10,max_seconds=120)
            self.records=[];self.counts={s:0 for s in d.SYMBOLS};self.last_rows=None
            self.missing=missing;self.broken=broken;self.delay=delay;self.always_missing=always_missing
        def get(self,path,params=None):
            before=now[0];symbol=(params or {}).get('instId')
            now[0]+=self.delay
            self.last_rows=None
            rec={'instrument':symbol,'completed_ms':now[0],'http_attempts':[{'response_received_or_failed_ms':now[0]}]}
            self.records.append(rec)
            if path.endswith('/time'):return [{'ts':str(now[0])}]
            self.counts[symbol]+=1
            if symbol in self.broken:raise RuntimeError('DO_NOT_LOG_SECRET')
            ts=T-3600000 if self.always_missing or symbol in self.missing and self.counts[symbol]==1 else T
            row=ROW.copy();row[0]=str(ts)
            self.last_rows=[row]
            return [row]
    def run(client):return d.visibility(plan,client=client,clock=lambda:now[0],sleep=sleep,monotonic=lambda:now[0]/1000)
    return T,now,plan,Client,run


def test_visibility_first_failure_does_not_stop_eth_sol(visibility_case):
    T,now,plan,Client,run=visibility_case
    c=Client(broken=('BTC-USDT-SWAP',));r=run(c)
    assert r['status']=='INCOMPLETE_OR_FAILED'
    assert r['symbols']['ETH-USDT-SWAP']['status']=='ACCEPTED_DIAGNOSTIC_ONLY'
    assert r['symbols']['SOL-USDT-SWAP']['status']=='ACCEPTED_DIAGNOSTIC_ONLY'
    assert c.counts['BTC-USDT-SWAP']==3
    assert 'DO_NOT_LOG_SECRET' not in json.dumps(r)
    assert not r['formal_f3_admission'] and r['paper_trades']==0


def test_visibility_missing_then_visible_is_diagnostic_retry_only(visibility_case):
    T,now,plan,Client,run=visibility_case
    c=Client(missing=('BTC-USDT-SWAP',));r=run(c)
    assert r['status']=='ALL_DIAGNOSTIC_QUOTES_VALID_NOT_F3_ADMISSION'
    assert c.counts=={'BTC-USDT-SWAP':2,'ETH-USDT-SWAP':1,'SOL-USDT-SWAP':1}
    btc=r['symbols']['BTC-USDT-SWAP']
    assert btc['first_visible_received_ms']==T+5010
    assert btc['samples'][0]['status']=='REJECTED' and btc['samples'][1]['status']=='ACCEPTED'
    assert not r['formal_f3_admission']
    with pytest.raises(ValueError,match='already exists'):run(c)
    assert len(c.records)==5  # no duplicate network requests on replay


def test_visibility_missing_all_has_fixed_budget(visibility_case):
    T,now,plan,Client,run=visibility_case
    c=Client(always_missing=True);r=run(c)
    assert len(c.records)==10 and all(v==3 for v in c.counts.values())
    assert r['status']=='INCOMPLETE_OR_FAILED'
    assert all(v['first_visible_received_ms'] is None for v in r['symbols'].values())
    assert r['finished_ms']<T+15000


def test_visibility_late_response_never_success_or_followup(visibility_case):
    T,now,plan,Client,run=visibility_case
    c=Client(delay=16000);r=run(c)
    assert r['status']!='ALL_DIAGNOSTIC_QUOTES_VALID_NOT_F3_ADMISSION'
    assert all(v['first_native_accepted_ms'] is None for v in r['symbols'].values())
    assert c.counts['ETH-USDT-SWAP']==c.counts['SOL-USDT-SWAP']==0


def test_visibility_cannot_import_past_hour(visibility_case):
    T,now,plan,Client,run=visibility_case;now[0]=T+15000
    c=Client()
    with pytest.raises(ValueError,match='schedule'):run(c)
    assert not c.records


def test_visibility_one_http_attempt_even_if_429(monkeypatch):
    calls=[]
    def send(self,request,**kwargs):
        calls.append(kwargs['timeout']);r=requests.Response();r.status_code=429;r._content=b'{}';return r
    monkeypatch.setattr(requests.Session,'send',send)
    monkeypatch.setattr(d.f2.okx.time,'sleep',lambda _:None)
    c=d.DiagnosticClient(d.f2.ScanLimits(max_requests=1,max_seconds=10))
    c.single_http_attempt=True;c.window_end_ms=d.f2.okx.utc_ms()+500
    with pytest.raises(d.DiagnosticRequestError):c.get('/api/v5/market/candles')
    assert len(calls)==1 and sum(calls[0])<=.5
    assert c.records[0]['http_attempts'][0]['http_status']==429


def test_nonhour_contract_exception_isolated(monkeypatch):
    class Client:
        def __init__(self,limits):self.limits=limits;self.records=[];self.last_rows=None
        def instrument(self,symbol):
            if symbol=='BTC-USDT-SWAP':raise RuntimeError('SECRET')
            return {}
        def get(self,path,params=None):self.last_rows=[ROW];return self.last_rows
    monkeypatch.setattr(d,'DiagnosticClient',Client)
    monkeypatch.setattr(d.operations,'clock_observation',lambda client:{'server_ms':int(ROW[0])+120000})
    monkeypatch.setattr(d.f2.okx,'utc_ms',lambda:int(ROW[0])+120000)
    r=d.execute('check')
    assert len(r['quotes'])==3 and r['quotes'][0]['status']=='CONTRACT_CHECK_FAILED'
    assert r['quotes'][1]['shape']['status']==r['quotes'][2]['shape']['status']=='VALID'
    assert r['status']=='STRUCTURE_CHECK_FAILED'
