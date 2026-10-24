"""R1.1 process-local guards. Never log network URLs, headers or payloads."""
from __future__ import annotations

import importlib
import threading
import time
from contextlib import ExitStack, contextmanager
from urllib.parse import urlsplit
from unittest.mock import patch

from tradingagents.forward_validation import atomic, digest, read
from tradingagents.llm_clients.reliability import LLMUnavailable


class ResearchBudgetExceeded(LLMUnavailable):
    def __init__(self, category):
        self.category=category
        super().__init__('R11_'+category)


class Meter:
    def __init__(self,path,llm_limit,http_limit,seconds):
        self.path=path;self.llm_limit=llm_limit;self.http_limit=http_limit
        self.deadline=time.monotonic()+seconds;self.lock=threading.RLock()
        if path.exists():raise ValueError('Never reuse a dispatched worker meter')
        self.value={'llm_calls':0,'http_dispatches':0,'reported_total_tokens':0,
                    'usage_available':False,'budget_error':None,'execution_enabled':False}
        self.save()

    def save(self):atomic(self.path,{**self.value,'sha256':digest(self.value)})

    def take(self,kind):
        with self.lock:
            limit=self.llm_limit if kind=='llm_calls' else self.http_limit
            error=('TIME_BUDGET' if time.monotonic()>=self.deadline else
                   'LLM_CALL_BUDGET' if kind=='llm_calls' and self.value[kind]>=limit else
                   'HTTP_BUDGET' if kind=='http_dispatches' and self.value[kind]>=limit else None)
            if error:
                self.value['budget_error']=error;self.save()
                raise ResearchBudgetExceeded(error)
            self.value[kind]+=1
            # Persist BEFORE dispatch; a crash cannot erase consumed budget.
            self.save()

    def usage(self,result):
        usage=[]
        for generation in getattr(result,'generations',[]):
            item=getattr(getattr(generation,'message',None),'usage_metadata',None)
            if item and isinstance(item.get('total_tokens'),int):usage.append(item['total_tokens'])
        if usage:
            with self.lock:
                self.value['reported_total_tokens']+=sum(usage)
                self.value['usage_available']=True;self.save()


def read_meter(path):
    raw=read(path);value={k:v for k,v in raw.items() if k!='sha256'}
    if raw.get('sha256')!=digest(value):raise ValueError('Counter fingerprint mismatch')
    if any(type(value.get(k)) is not int or value[k]<0 for k in ('llm_calls','http_dispatches','reported_total_tokens')):
        raise ValueError('Invalid usage counter')
    return value


def public_exchange_only(method,url):
    host=urlsplit(str(url)).hostname or ''
    path=urlsplit(str(url)).path
    if host=='okx.com' or host.endswith('.okx.com'):
        from tradingagents.dataflows.vendors.okx import PUBLIC_PATHS
        if method.upper()!='GET' or path not in PUBLIC_PATHS:
            raise ValueError('Non-public OKX transport blocked')
    if host=='binance.com' or host.endswith('.binance.com'):
        from tradingagents.dataflows.vendors.okx_market_checks import BINANCE_PATHS
        if method.upper()!='GET' or path not in BINANCE_PATHS:raise ValueError('Non-public Binance transport blocked')


@contextmanager
def guards(meter):
    """Only installed inside an isolated worker, restored for tests afterwards.

    Counts LangChain sync logical calls (including cache attempts), Requests
    dispatches and httpx/httpx2 single request sends, including SDK retries.
    Async research is unsupported and denied rather than escaping the budget.
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    import requests
    with ExitStack() as stack:
        original=BaseChatModel._generate_with_cache
        def generate(self,*args,**kwargs):
            meter.take('llm_calls');result=original(self,*args,**kwargs);meter.usage(result);return result
        stack.enter_context(patch.object(BaseChatModel,'_generate_with_cache',generate))
        original_stream=BaseChatModel.stream
        def stream(self,*args,**kwargs):
            meter.take('llm_calls')
            yield from original_stream(self,*args,**kwargs)
        stack.enter_context(patch.object(BaseChatModel,'stream',stream))
        async def no_async(*args,**kwargs):raise ResearchBudgetExceeded('ASYNC_UNSUPPORTED')
        stack.enter_context(patch.object(BaseChatModel,'_agenerate_with_cache',no_async))
        send=requests.Session.send
        def request_send(self,request,*args,**kwargs):
            public_exchange_only(request.method,request.url);meter.take('http_dispatches')
            return send(self,request,*args,**kwargs)
        stack.enter_context(patch.object(requests.Session,'send',request_send))
        for name in ('httpx','httpx2'):
            try:module=importlib.import_module(name)
            except ImportError:continue
            base=module.Client._send_single_request
            def http_send(self,request,*args,_base=base,**kwargs):
                public_exchange_only(request.method,request.url);meter.take('http_dispatches')
                return _base(self,request,*args,**kwargs)
            stack.enter_context(patch.object(module.Client,'_send_single_request',http_send))
            stack.enter_context(patch.object(module.AsyncClient,'_send_single_request',no_async))
        yield
