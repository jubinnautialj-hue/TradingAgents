"""TEST_FIXTURE Responses SSE; no model/network or historical records."""
import copy
import json
from types import SimpleNamespace as NS

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import START, END, StateGraph
from langgraph.prebuilt import ToolNode
from langchain_core.tools import StructuredTool

from tradingagents.agents.analysts.market_analyst import TOOLS
from tradingagents.agents.analysts.market_contract import MarketIndicatorArgs, admit_market_tool
from tradingagents.agents.analysts.market_safe_adapter import ACTIVE, scope
from tradingagents.agents.state import AgentState
from tradingagents.llm_clients.openai_client import ChatGPTPlanChatOpenAI
from tradingagents.llm_clients.reliability import LLMUnavailable
from tests.test_guarded_research import audited
from tests.test_market_tool_contract import pinned

INDICATORS = ['rsi', 'macd', 'close_50_sma', 'close_200_sma', 'close_10_ema', 'macds', 'macdh']


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', lambda *a, **k: pytest.fail('REAL_NETWORK_FORBIDDEN'))


def events(n=1, mode='interleaved'):
    items = [dict(type='function_call', id=f'fc_{i}', call_id=f'call_{i}', name='get_indicators',
                  arguments=json.dumps(dict(indicator=INDICATORS[i], curr_date='2026-11-03', look_back_days=30+i)),
                  status='completed') for i in range(n)]
    def added(i): return dict(type='response.output_item.added', output_index=i,
                              item={**items[i], 'arguments':'', 'status':'in_progress'})
    def delta(i, value=None): return dict(type='response.function_call_arguments.delta', output_index=i,
                                         item_id=items[i]['id'], delta=items[i]['arguments'] if value is None else value)
    def done(i): return dict(type='response.output_item.done', output_index=i, item=copy.deepcopy(items[i]))
    rows=[]
    if mode == 'sequential':
        for i in range(n):rows.extend([added(i),delta(i),done(i)])
    else:
        rows.extend(added(i) for i in range(n))
        if mode == 'fragmented':
            for part in range(2):
                for i in reversed(range(n)):
                    value=items[i]['arguments'];mid=len(value)//2
                    rows.append(delta(i,value[:mid] if part==0 else value[mid:]))
        elif mode != 'done_only':rows.extend(delta(i) for i in range(n))
        rows.extend(done(i) for i in reversed(range(n)))
    response=dict(id='resp_TEST_FIXTURE', object='response', created_at=1, model='gpt-6.1-sol',
                  parallel_tool_calls=True, tool_choice='auto', tools=[], output=copy.deepcopy(items),
                  status='completed', error=None, usage=dict(input_tokens=80, output_tokens=20,total_tokens=100,
                      input_tokens_details=dict(cached_tokens=0,cache_write_tokens=0),output_tokens_details=dict(reasoning_tokens=0)))
    rows.append(dict(type='response.completed',response=response))
    return rows, items


def model_for(rows, requests):
    wire=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in rows).encode()
    def send(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire,request=request)
    client=httpx.Client(transport=httpx.MockTransport(send))
    model=ChatGPTPlanChatOpenAI(model='gpt-6.1-sol', api_key='TEST_FIXTURE',base_url='https://api.openai.com/v1',
                              use_responses_api=True,max_retries=0,http_client=client)
    return model,client


def invoke(rows, *, safe=True):
    requests=[];model,client=model_for(rows,requests)
    try:
        if safe:
            with scope(TOOLS) as evidence:
                msg=model.bind_tools(TOOLS).invoke([HumanMessage(content='TEST_FIXTURE')])
                msg=evidence.admit(msg)
        else:msg=model.bind_tools(TOOLS).invoke([HumanMessage(content='TEST_FIXTURE')])
        return msg,requests
    finally:client.close()


def tool_results(message):
    executed=[]
    def body(**kwargs): executed.append(kwargs); return 'TEST_FIXTURE_DETERMINISTIC_OUTPUT'
    tool=StructuredTool.from_function(body,name='get_indicators',description='TEST_FIXTURE',args_schema=MarketIndicatorArgs)
    graph=StateGraph(AgentState)
    graph.add_node('tools',ToolNode([tool],wrap_tool_call=admit_market_tool))
    graph.add_edge(START,'tools');graph.add_edge('tools',END)
    result=graph.compile().invoke(dict(messages=[message],company_of_interest='BTC-USDT-SWAP',
                                      trade_date='2026-11-03',market_snapshot={}))
    return executed,[m for m in result['messages'] if m.type=='tool']


@pytest.mark.parametrize('n',[1,6,7])
@pytest.mark.parametrize('mode',['sequential','interleaved','fragmented','done_only'])
def test_terminal_evidence_preserves_identity_arguments_and_schema(n,mode):
    rows,items=events(n,mode);msg,requests=invoke(rows)
    assert [(x['id'],x['name'],x['args']) for x in msg.tool_calls] == [(x['call_id'],x['name'],json.loads(x['arguments'])) for x in items]
    executed,results=tool_results(msg)
    assert len(executed)==len(results)==n and all(r.status=='success' for r in results)
    assert len(requests)==1 and 'parallel_tool_calls' not in requests[0]
    assert msg.usage_metadata['total_tokens']==100
    assert ACTIVE.get() is None


@pytest.mark.parametrize('n',[6,7])
def test_before_after_original_interleaving(n):
    rows,_=events(n);old,_=invoke(rows,safe=False);new,_=invoke(rows)
    assert len(old.tool_calls)==n*2
    assert sum(not c['args'] for c in old.tool_calls if c['name']=='get_indicators')==n
    assert len(new.tool_calls)==n and all(c['args'] for c in new.tool_calls)


@pytest.mark.parametrize('fault', ['duplicate_call','duplicate_item','index','delta_conflict','type_conflict',
    'done_conflict','missing_done','missing_added','missing_completed','incomplete','unknown_usage','usage_sum',
    'duplicate_done','unknown_tool','unknown_indicator','missing_parameter','duplicate_json_key','non_object',
    'nan','missing_delta_identity','response_id','after_complete'])
def test_invalid_evidence_never_reaches_tools(fault):
    rows,_=events(2 if fault in {'duplicate_call','duplicate_item','index'} else 1)
    if fault=='duplicate_call':
        for e in rows:
            if e.get('item',{}).get('id')=='fc_1':e['item']['call_id']='call_0'
        rows[-1]['response']['output'][1]['call_id']='call_0'
    elif fault=='duplicate_item':rows[1]['item']['id']='fc_0'
    elif fault=='index':rows[1]['output_index']=0
    elif fault=='delta_conflict':next(e for e in rows if e['type'].endswith('.delta'))['delta']='{"indicator":"macd","curr_date":"2026-11-03","look_back_days":30}'
    elif fault=='type_conflict':next(e for e in rows if e['type'].endswith('.delta'))['delta']='{"indicator":"rsi","curr_date":"2026-11-03","look_back_days":true}'
    elif fault=='done_conflict':next(e for e in rows if e['type'].endswith('.done'))['item']['arguments']='{}'
    elif fault=='missing_done':rows=[e for e in rows if e['type']!='response.output_item.done']
    elif fault=='missing_added':rows=[e for e in rows if e['type']!='response.output_item.added']
    elif fault=='missing_completed':rows=rows[:-1]
    elif fault=='incomplete':rows[-1]['response']['status']='incomplete'
    elif fault=='unknown_usage':rows[-1]['response']['usage']=None
    elif fault=='usage_sum':rows[-1]['response']['usage']['total_tokens']=101
    elif fault=='duplicate_done':rows.insert(-1,copy.deepcopy(rows[-2]))
    elif fault in {'unknown_tool','unknown_indicator','missing_parameter','duplicate_json_key','non_object','nan'}:
        if fault=='unknown_tool':
            for e in rows:
                if 'item' in e:e['item']['name']='not_offered'
            rows[-1]['response']['output'][0]['name']='not_offered'
        else:
            value={'unknown_indicator':'{"indicator":"not_registered","curr_date":"2026-11-03"}',
                   'missing_parameter':'{}','duplicate_json_key':'{"indicator":"rsi","indicator":"macd","curr_date":"2026-11-03"}',
                   'non_object':'[]','nan':'{"indicator":"rsi","curr_date":"2026-11-03","look_back_days":NaN}'}[fault]
            for e in rows:
                if e['type'].endswith('.delta'):e['delta']=value
                if e['type'].endswith('.done'):e['item']['arguments']=value
            rows[-1]['response']['output'][0]['arguments']=value
    elif fault=='missing_delta_identity':next(e for e in rows if e['type'].endswith('.delta')).pop('item_id')
    elif fault=='response_id':rows[-1]['response']['id']='invalid_response'
    elif fault=='after_complete':rows.append(copy.deepcopy(rows[0]))
    # SDK can reject malformed JSON before project admission; both paths deny
    # the response. Nothing returned here is ever dispatched to ToolNode.
    with pytest.raises((LLMUnavailable,ValueError,TypeError)):
        invoke(rows)
    assert ACTIVE.get() is None


def test_normal_single_output_and_protocol_unchanged():
    rows,_=events(1,'sequential');old,_=invoke(rows,safe=False);new,_=invoke(rows)
    assert old.tool_calls==new.tool_calls and old.usage_metadata==new.usage_metadata
    a,ra=tool_results(old);b,rb=tool_results(new)
    assert a==b and [(x.content,x.tool_call_id,x.status) for x in ra]==[(x.content,x.tool_call_id,x.status) for x in rb]


def test_audit_is_structural_private_and_shared_locked(tmp_path,monkeypatch):
    path=tmp_path/'TEST_FIXTURE.jsonl';monkeypatch.setenv('TA_SINGLE_AUDIT_FILE',str(path))
    msg,_=invoke(events(1)[0]);text=path.read_text()
    assert 'MARKET_SAFE_ADMISSION' in text and 'TERMINAL_VERIFIED' in text
    assert '2026-11-03' not in text and 'look_back_days": 30' not in text
    assert 'TEST_FIXTURE' not in text and path.stat().st_size<4*1024**2
    assert path.with_name(path.name+'.lock').exists()


def test_scope_does_not_leak_into_other_agent_request():
    rows,_=events(1);model,client=model_for(rows,[])
    try:
        original=model.root_client
        with scope(TOOLS) as evidence:
            msg=model.bind_tools(TOOLS).invoke('TEST_FIXTURE');evidence.admit(msg)
        assert model.root_client is original and ACTIVE.get() is None
        assert model.bind_tools(TOOLS).invoke('TEST_FIXTURE').tool_calls==msg.tool_calls
    finally:client.close()


@pytest.mark.parametrize('mode',['valid','unknown','conflict'])
def test_real_guard_reconciles_before_tool_admission(audited,mode):
    from tradingagents.safety_budget import SafetyStop
    ledger,rid,session,meter=audited
    rows,_=events(7)
    if mode=='unknown':rows[-1]['response']['usage']=None
    if mode=='conflict':rows[-2]['item']['arguments']='{}'
    with session.guards(meter):
        if mode=='valid':
            message,_=invoke(rows)
            assert len(message.tool_calls)==7
        else:
            with pytest.raises((SafetyStop,LLMUnavailable)):invoke(rows)
    status=ledger.status()
    assert status['llm_calls']==status['http']==1
    if mode=='unknown':
        assert status['paused'] and status['total_usage']=='UNKNOWN'
    else:
        assert status['tokens']==meter.value['reported_total_tokens']==100
        if mode=='conflict':
            ledger.finish_research(rid,100,success=False)
            assert ledger.status()['paused']


def test_native_market_node_uses_safe_boundary():
    from tradingagents.agents.analysts.market_analyst import create_market_analyst
    model,client=model_for(events(7)[0],[])
    try:
        state=dict(company_of_interest='BTC-USDT-SWAP',trade_date='2026-11-03',messages=[HumanMessage(content='TEST_FIXTURE')])
        result=create_market_analyst(model)(state)
        assert len(result['messages'][0].tool_calls)==7
        assert result['market_report']==''
    finally:client.close()


def test_concurrent_scopes_are_isolated():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda n:invoke(events(n)[0])[0], [6,7]))
    assert [len(m.tool_calls) for m in results]==[6,7]
    assert ACTIVE.get() is None


def test_delta_before_added_is_associated_by_exact_identity():
    rows,_=events(2)
    delta=next(e for e in rows if e['type'].endswith('.delta'))
    rows.remove(delta);rows.insert(0,delta)
    msg,_=invoke(rows)
    assert [(c['id'],c['args']['indicator']) for c in msg.tool_calls]==[('call_0','rsi'),('call_1','macd')]


def test_valid_comma_batch_preserves_bytes():
    rows,_=events(1)
    value=json.dumps(dict(indicator='rsi,macd,macds,macdh',curr_date='2026-11-03',look_back_days=30))
    for row in rows:
        if row['type'].endswith('.delta'):row['delta']=value
        if row['type'].endswith('.done'):row['item']['arguments']=value
    rows[-1]['response']['output'][0]['arguments']=value
    old,_=invoke(rows,safe=False);new,_=invoke(rows)
    assert old.tool_calls==new.tool_calls
    assert tool_results(old)[1][0].content.encode()==tool_results(new)[1][0].content.encode()


def test_rejected_response_audit_contains_no_argument_values(tmp_path,monkeypatch):
    path=tmp_path/'TEST_FIXTURE.jsonl';monkeypatch.setenv('TA_SINGLE_AUDIT_FILE',str(path))
    rows,_=events(1)
    rows[-2]['item']['arguments']='{"indicator":"SECRET_ARGUMENT_VALUE"}'
    with pytest.raises(LLMUnavailable,match='FINAL_CONFLICT'):invoke(rows)
    text=path.read_text()
    assert 'MARKET_SAFE_REJECTION' in text and 'FINAL_CONFLICT' in text
    assert 'SECRET_ARGUMENT_VALUE' not in text


def test_audit_failure_prevents_admission(monkeypatch,tmp_path):
    import tradingagents.agents.analysts.market_contract as contract
    monkeypatch.setattr(contract,'append_audit',lambda *a: (_ for _ in ()).throw(RuntimeError('AUDIT_FIXTURE_FAILURE')))
    monkeypatch.setenv('TA_SINGLE_AUDIT_FILE',str(tmp_path/'TEST_FIXTURE.jsonl'))
    with pytest.raises(RuntimeError,match='AUDIT_FIXTURE_FAILURE'):invoke(events(1)[0])


def test_text_only_completed_response_preserves_wait_text():
    rows,_=events(1)
    rows=rows[-1:]
    rows[0]['response']['output']=[dict(type='message',id='msg_fixture',role='assistant',status='completed',
        content=[dict(type='output_text',text='WAIT TEST_FIXTURE',annotations=[])])]
    # The normal SDK requires text delta events to build streamed visible text.
    rows.insert(0,dict(type='response.output_text.delta',output_index=0,content_index=0,item_id='msg_fixture',delta='WAIT TEST_FIXTURE'))
    old,_=invoke(rows,safe=False);new,_=invoke(rows)
    assert old.content==new.content=='WAIT TEST_FIXTURE' and new.tool_calls==[]


def test_same_frozen_market_data_actual_tool_results_byte_identical(pinned):
    from tests.test_okx_multitimeframe import CFG,DAY,SYMBOL
    from tradingagents.dataflows.config import run_config
    def dated_rows(mode):
        rows,_=events(7,mode)
        for row in rows:
            if row['type'].endswith('.delta'):
                value=json.loads(row['delta']);value['curr_date']=DAY;row['delta']=json.dumps(value)
            if row['type'].endswith('.done'):
                value=json.loads(row['item']['arguments']);value['curr_date']=DAY;row['item']['arguments']=json.dumps(value)
        for item in rows[-1]['response']['output']:
            value=json.loads(item['arguments']);value['curr_date']=DAY;item['arguments']=json.dumps(value)
        return rows
    before=copy.deepcopy(pinned)
    old,_=invoke(dated_rows('sequential'),safe=False)
    new,_=invoke(dated_rows('interleaved'))
    def execute(message):
        graph=StateGraph(AgentState)
        graph.add_node('tools',ToolNode(list(TOOLS),wrap_tool_call=admit_market_tool))
        graph.add_edge(START,'tools');graph.add_edge('tools',END)
        with run_config(CFG):
            result=graph.compile().invoke(dict(messages=[message],company_of_interest=SYMBOL,trade_date=DAY,market_snapshot=copy.deepcopy(pinned)))
        return [(x.content.encode(),x.tool_call_id,x.status) for x in result['messages'] if x.type=='tool']
    a,b=execute(old),execute(new)
    assert a==b and len(a)==7 and all(x[2]=='success' for x in b)
    assert pinned==before


@pytest.mark.parametrize('mode',['equal','conflict','duplicate'])
def test_arguments_done_terminal_is_checked_when_present(mode):
    rows,items=events(1)
    final=dict(type='response.function_call_arguments.done',item_id='fc_0',output_index=0,
               arguments=items[0]['arguments'] if mode!='conflict' else '{}')
    rows.insert(-2,final)
    if mode=='duplicate':rows.insert(-2,copy.deepcopy(final))
    if mode=='equal':assert invoke(rows)[0].tool_calls[0]['args']==json.loads(items[0]['arguments'])
    else:
        with pytest.raises(LLMUnavailable):invoke(rows)


def test_completed_output_index_conflict_is_rejected():
    rows,_=events(2)
    rows[-1]['response']['output'].reverse()
    with pytest.raises(LLMUnavailable,match='COMPLETE_INDEX'):invoke(rows)


def test_optional_item_status_does_not_replace_required_completed_event():
    rows,_=events(1)
    for row in rows:
        if 'item' in row:row['item'].pop('status',None)
    rows[-1]['response']['output'][0].pop('status')
    assert len(invoke(rows)[0].tool_calls)==1
    rows.pop()
    with pytest.raises(LLMUnavailable):invoke(rows)


@pytest.mark.parametrize('name,args',[
    ('get_stock_data',{'start_date':'2026-10-01','end_date':'2026-11-03'}),
    ('get_verified_market_snapshot',{'curr_date':'2026-11-03','look_back_days':30}),
])
@pytest.mark.parametrize('injected',[False,True])
def test_other_market_tools_public_schema_and_no_injected_overrides(name,args,injected):
    rows,_=events(1)
    if injected:args={**args,'symbol':'UNTRUSTED_FIXTURE'}
    value=json.dumps(args)
    for row in rows:
        if 'item' in row:row['item']['name']=name
        if row['type'].endswith('.delta'):row['delta']=value
        if row['type'].endswith('.done'):row['item']['arguments']=value
    rows[-1]['response']['output'][0].update(name=name,arguments=value)
    if injected:
        with pytest.raises(LLMUnavailable,match='SCHEMA_FIELDS'):invoke(rows)
    else:
        msg,_=invoke(rows)
        assert msg.tool_calls[0]['name']==name and msg.tool_calls[0]['args']==args
