"""Windows/POSIX offline MockTransport only. No native PRODUCTION invoker."""
import gzip
import hashlib
import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from openai._streaming import SSEDecoder
from tradingagents.llm_clients import raw_sse_evidence as r
from tradingagents.forward_validation import digest
from tests.test_market_online_probe import gate, bound, policy
from tests.test_market_tool_contract import pinned
ACTUAL_CODE_IDENTITY = r.code_identity

RAW = (': fixture-comment\r\nevent: response.metadata\r\ndata: {"type":"response.metadata","private":"SECRET_BODY"}\r\n\r\n'
       'event: response.completed\ndata: {"type":"response.completed","output":[]}\n\n').encode()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('NETWORK_FORBIDDEN'))
    monkeypatch.delenv(r.ENV, raising=False)
    monkeypatch.setenv('TRADINGAGENTS_LLM_PROVIDER','chatgpt')
    monkeypatch.setenv('TRADINGAGENTS_QUICK_THINK_LLM','gpt-6.1-sol')
    monkeypatch.setenv('TRADINGAGENTS_DEEP_THINK_LLM','gpt-6.1-sol')


@pytest.fixture
def root(tmp_path):
    p = tmp_path / 'PRIVATE_FIXTURE'; p.mkdir(mode=0o777 if os.name == 'nt' else 0o700)
    if os.name == 'nt':
        sid = r.windows_sid()
        subprocess.run(['icacls', str(p), '/inheritance:r', '/grant:r', '*'+sid+':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], check=True, capture_output=True)
    r.private(p, True)
    return p


def capture(root, limit=r.MAX_BYTES):
    d = root / 'capture'; d.mkdir(mode=0o777 if os.name == 'nt' else 0o700)
    return r.Capture(d, limit, root/'audit.jsonl', {'run_id':'fixture-run','claim_sha256':'fixture-claim'})


class Source(httpx2.SyncByteStream):
    def __init__(self, data, broken=False):self.data=data;self.broken=broken;self.closed=False
    def __iter__(self):
        for i in range(0,len(self.data),7):
            if self.broken and i>=28:raise httpx2.ReadError('SECRET_EXCEPTION')
            yield self.data[i:i+7]
    def close(self):self.closed=True


def run(c=None, compressed=False, broken=False, ids=True):
    source=Source(gzip.compress(RAW,mtime=0) if compressed else RAW,broken)
    observed={}
    def handler(req):
        observed['request']=req.content
        return httpx2.Response(200,headers={'content-type':'text/event-stream','content-encoding':'gzip' if compressed else 'identity',
            'set-cookie':'SECRET_COOKIE',**({'x-request-id':'fixture-original-id'} if ids else {})},stream=source)
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        req=client.build_request('POST','https://api.openai.com/v1/responses',json={'stream':True,'store':False,'model':'fixture-model','tools':[]},
            headers={'Authorization':'Bearer SECRET_AUTH',**({'x-client-request-id':'fixture-client-id'} if ids else {})})
        body=req.content
        response=client.send(req,stream=True)
        try:
            if c:c.attach(req,response,'fixture-wire')
            result=list(SSEDecoder().iter_bytes(response.iter_bytes()))
        finally:response.close()
        assert body==req.content==observed['request']
    assert source.closed
    return [(e.event,e.data) for e in result]


@pytest.mark.parametrize('compressed',[False,True])
def test_exact_stream_and_rollback(root,compressed):
    before=run(compressed=compressed)
    c=capture(root)
    after=run(c,compressed=compressed)
    assert before==after and len(after)==2
    raw=(c.root/'entity.bin').read_bytes()
    assert (gzip.decompress(raw) if compressed else raw)==RAW
    m=json.loads((c.root/'metadata.json').read_text())
    assert m['state']=='ENTITY_EOF' and m['captured_sha256']==hashlib.sha256(raw).hexdigest()
    assert m['http_status']==200 and m['sent_x_client_request_id']=='fixture-client-id'
    assert 'SECRET_COOKIE' not in json.dumps(m) and 'SECRET_AUTH' not in json.dumps(m)
    assert 'SECRET_BODY' not in (root/'audit.jsonl').read_text()


def test_missing_ids_and_200_not_sdk_success(root):
    c=capture(root);run(c,ids=False)
    m=json.loads((c.root/'metadata.json').read_text())
    assert m['sent_x_client_request_id'] is None and m['response_headers']['x-request-id'] is None
    assert m['state']=='ENTITY_EOF' and 'usage_confirmed' not in m


@pytest.mark.parametrize('mode',['interrupt','capacity','write','short','audit'])
def test_failures_propagate_and_never_claim_complete(root,monkeypatch,mode):
    c=capture(root,limit=20 if mode=='capacity' else r.MAX_BYTES)
    if mode in {'write','short'}:
        original=os.write
        def write(fd,b):
            if fd==c.fd:
                if mode=='write':raise OSError('SECRET_ERROR')
                return original(fd,b[:2])
            return original(fd,b)
        monkeypatch.setattr(os,'write',write)
    if mode=='audit':monkeypatch.setattr(r,'append_audit',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('AUDIT_FAILURE')))
    with pytest.raises((RuntimeError,OSError,httpx2.ReadError)):
        run(c,broken=mode=='interrupt')
    m=json.loads((c.root/'metadata.json').read_text())
    assert m['state']!='ENTITY_EOF'
    if m['state']=='INTERRUPTED':
        assert m['captured_sha256']==hashlib.sha256((c.root/'entity.bin').read_bytes()).hexdigest()
    if (root/'audit.jsonl').exists():
        assert 'SECRET' not in (root/'audit.jsonl').read_text()


def test_early_close_unfinalized_state(root):
    c=capture(root)
    req=httpx2.Request('POST','https://api.openai.com/v1/responses')
    response=httpx2.Response(200,stream=Source(RAW),request=req)
    c.attach(req,response,'fixture-wire');response.close()
    assert json.loads((c.root/'metadata.json').read_text())['state']=='INTERRUPTED'


def test_file_replacement_rejected(root):
    c=capture(root)
    req=httpx2.Request('POST','https://api.openai.com/v1/responses')
    response=httpx2.Response(200,stream=Source(RAW),request=req)
    c.attach(req,response,'fixture-wire')
    # Rename over an open file is POSIX-specific; Windows deliberately rejects it.
    if os.name=='nt':
        with pytest.raises(OSError):(c.root/'entity.bin').unlink()
        response.close()
    else:
        (c.root/'entity.bin').rename(c.root/'saved.bin');(c.root/'entity.bin').touch(mode=0o600)
        with pytest.raises(RuntimeError,match='IDENTITY'):list(response.iter_bytes())


def approval(root,monkeypatch,**changes):
    ledger_root=root/'ledger';(ledger_root/'probe').mkdir(parents=True)
    (ledger_root/'probe/claim.json').write_text('FIXTURE')
    (ledger_root/'probe/binding.json').write_text(json.dumps({'run_id':'fixture-run','claim_sha256':hashlib.sha256(b'FIXTURE').hexdigest()}))
    policy={'run_purpose':'market-protocol-probe-v1','model':'fixture-model'}
    ledger=SimpleNamespace(root=ledger_root,load=lambda:{'policy':policy},clock=lambda:10)
    config={'fixture':True};scope={'authorization_id':'fixture-authorization'}
    monkeypatch.setattr(r,'code_identity',lambda:{'fixture_source':'fixture'})
    value={'version':r.VERSION,'approved':True,'purpose':'market-protocol-probe-v1','authorization_id':scope['authorization_id'],
        'ledger_root':str(ledger_root),'policy_hash':digest(policy),'config_hash':digest(config),'code_identity_hash':digest(r.code_identity()),
        'expires_ms':100,'directory':str(root),'max_bytes':4096,**changes}
    path=root/'approval.json';path.write_text(json.dumps(value));os.chmod(path,0o600)
    monkeypatch.setenv(r.ENV,str(path))
    return lambda:r.prepare(ledger,'fixture-run',scope,config,root/'audit.jsonl')


def test_default_off_no_file_access():
    assert r.prepare(None,None,None,None,None) is None


@pytest.mark.parametrize('changes',[{'approved':False},{'purpose':'RESEARCH'},{'config_hash':'wrong'},{'code_identity_hash':'wrong'},
    {'expires_ms':10},{'max_bytes':True},{'max_bytes':r.MAX_BYTES+1},{'authorization_id':'wrong'},{'extra':'wrong'}])
def test_independent_approval_drift_rejected(root,monkeypatch,changes):
    launch=approval(root,monkeypatch,**changes)
    with pytest.raises(RuntimeError):launch()
    assert 'APPROVAL_OR_SETUP_FAILED' in (root/'audit.jsonl').read_text()


def test_one_capture_concurrent_and_no_replay(root,monkeypatch):
    launch=approval(root,monkeypatch)
    def attempt(_):
        try:return launch()
        except RuntimeError:return None
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(attempt,range(4)))
    assert sum(x is not None for x in results)==1
    with pytest.raises(RuntimeError,match='CONSUMED'):launch()


def test_permissions_reject_broad_access(tmp_path):
    if os.name=='nt':
        with pytest.raises(RuntimeError,match='PERMISSIONS'):r.private(tmp_path,True)
    else:
        tmp_path.chmod(0o755)
        with pytest.raises(RuntimeError,match='PERMISSIONS'):r.private(tmp_path,True)


@pytest.mark.parametrize('completed,cap,tools',[(True,4096,False),(False,4096,False),(True,16,False),(True,r.MAX_BYTES,True)])
def test_native_probe_sdk_meter_and_unknown_fail_closed(root,gate,pinned,monkeypatch,completed,cap,tools):
    from tradingagents import market_online_probe as probe
    from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI
    ledger,normal_approval,rid=bound(gate)
    obj=ledger.load();scope=json.loads(normal_approval.read_text());config=probe._config(ledger,policy())
    approval_path=root/'raw-approval.json'
    approval_path.write_text(json.dumps({'version':r.VERSION,'approved':True,'purpose':probe.PURPOSE,
        'authorization_id':scope['authorization_id'],'ledger_root':str(ledger.root),'policy_hash':digest(obj['policy']),
        'config_hash':digest(config),'code_identity_hash':digest(r.code_identity()),'expires_ms':ledger.clock()+60000,
        'directory':str(root),'max_bytes':cap}));os.chmod(approval_path,0o600)
    monkeypatch.setenv(r.ENV,str(approval_path))
    response={'id':'resp_fixture','object':'response','created_at':0,'status':'completed','model':'gpt-6.1-sol',
        'output':[],'error':None,'incomplete_details':None,'usage':{'input_tokens':80,'output_tokens':20,'total_tokens':100,
        'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}
    rows=[{'type':'response.created','response':{**response,'status':'in_progress','usage':None}}]
    if completed:rows.append({'type':'response.completed','response':response})
    if tools:
        from tests.test_market_safe_adapter import events
        rows,_=events(7,'fragmented')
    raw=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in rows).encode()
    requests=[]
    def handler(req):
        requests.append(req.content)
        return httpx2.Response(200,headers={'content-type':'text/event-stream','x-request-id':'fixture-request-id'},stream=Source(raw))
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        model=ChatGPTPlanChatOpenAI(model='gpt-6.1-sol',api_key='TEST_FIXTURE',use_responses_api=True,http_client=client,max_retries=0)
        def invoke(cfg):model.invoke('TEST_FIXTURE_PRIVATE');return {'tool_count':0}
        if tools:
            import copy
            monkeypatch.setattr('tradingagents.dataflows.okx_research.preflight',lambda state:
                {'market_snapshot':copy.deepcopy(pinned),'data_quality':'VALID','allow_new_risk':True})
            monkeypatch.setattr('tradingagents.llm_clients.factory.create_tier_client',lambda *a,**k:SimpleNamespace(get_llm=lambda:model))
            monkeypatch.setattr('langgraph.prebuilt.ToolNode.invoke',lambda *a,**k:pytest.fail('TOOL_FORBIDDEN'))
            monkeypatch.setattr('tradingagents.forward_validation.Registry.begin',lambda *a,**k:pytest.fail('F1_FORBIDDEN'))
            invoke=probe._market_turn
        if completed and cap>16:
            receipt=probe.worker(ledger,rid,normal_approval,invoker=invoke)
            assert receipt['status']==('PASS' if tools else 'INCONCLUSIVE_NO_TOOL_CALL')
            if tools:assert receipt['tool_count']==7
            assert ledger.status()['tokens']==100 and not ledger.status()['paused']
        else:
            with pytest.raises(Exception):probe.worker(ledger,rid,normal_approval,invoker=invoke)
            assert ledger.status()['paused'] and ledger.status()['total_usage']=='UNKNOWN'
    assert len(requests)==ledger.status()['llm_calls']==ledger.status()['http']==1
    assert json.loads(requests[0])['stream'] is True and json.loads(requests[0])['store'] is False
    assert 'TEST_FIXTURE_PRIVATE' not in (probe.folder(ledger)/'audit.jsonl').read_text()


def test_process_unique_capture(root,monkeypatch):
    approval(root,monkeypatch)
    path=root/'approval.json';value=json.loads(path.read_text())
    # Use actual project identity in the independent processes, not a production invoker.
    value['code_identity_hash']=digest(ACTUAL_CODE_IDENTITY());path.write_text(json.dumps(value))
    program="""import json,sys;from pathlib import Path;from types import SimpleNamespace;from tradingagents.llm_clients.raw_sse_evidence import prepare
p=Path(sys.argv[1]);policy={'run_purpose':'market-protocol-probe-v1','model':'fixture-model'}
l=SimpleNamespace(root=p/'ledger',load=lambda:{'policy':policy},clock=lambda:10)
try:prepare(l,'fixture-run',{'authorization_id':'fixture-authorization'},{'fixture':True},p/'audit.jsonl');print('WIN')
except RuntimeError:print('REJECT')
"""
    import sys
    children=[subprocess.Popen([sys.executable,'-c',program,str(root)],env={**os.environ,r.ENV:str(path)},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    results=[c.communicate(timeout=45) for c in children]
    assert all(c.returncode==0 for c in children),results
    assert sorted(x[0].strip() for x in results)==['REJECT','WIN']


def test_expiry_and_no_automatic_second_request(root):
    c=capture(root);c.meta.update(model='fixture-model',approval_deadline_ms=0)
    request=httpx2.Request('POST','https://api.openai.com/v1/responses',json={'stream':True,'store':False,'model':'fixture-model'})
    with pytest.raises(RuntimeError,match='EXPIRED'):c.before_request(request,'fixture-wire')
    c.meta['approval_deadline_ms']=10**16
    c.before_request(request,'fixture-wire')
    with pytest.raises(RuntimeError,match='SECOND_REQUEST'):c.before_request(request,'fixture-wire')


@pytest.mark.parametrize('mode',['headers','url','preconsumed'])
def test_unsupported_metadata_or_body_never_adopted(root,mode):
    c=capture(root)
    url='https://api.openai.com/v1/responses?SECRET_QUERY' if mode=='url' else 'https://api.openai.com/v1/responses'
    request=httpx2.Request('POST',url)
    response=httpx2.Response(200,headers={'x-request-id':'x'*5000} if mode=='headers' else {},
        **({'content':b'SECRET_BODY'} if mode=='preconsumed' else {'stream':Source(RAW)}),request=request)
    with pytest.raises(RuntimeError):c.attach(request,response,'fixture-wire')
    assert response.is_closed
    assert 'SECRET_QUERY' not in (root/'audit.jsonl').read_text()
