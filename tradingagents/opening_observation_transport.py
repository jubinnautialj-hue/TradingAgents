"""F3.1 owned public GET transport: durable wire budget, no hidden retries."""
from __future__ import annotations
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
import requests
from tradingagents import auto_research as audit
from tradingagents.dataflows.files import locked
from tradingagents.dataflows.vendors import okx
from tradingagents.quote_probe_diagnostics import summary, stamp


class PublicFailure(ValueError):
    def __init__(self, kind):
        self.kind=kind
        super().__init__(kind)


class WireBudget:
    def __init__(self, root, policy_hash, *, total=60, quotes=9, clock=okx.utc_ms, sleep=time.sleep):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'budget.json';self.policy_hash=policy_hash
        self.total=total;self.quotes=quotes;self.clock=clock;self.sleep=sleep

    def dispatch(self, path, params, context, deadline):
        while True:
            with locked(self.path):
                now=self.clock()
                if deadline is not None and now>=deadline:raise PublicFailure('WINDOW_EXHAUSTED')
                value=audit.unseal(self.path) if self.path.exists() else {
                    'policy_hash':self.policy_hash,'count':0,'quote_count':0,'last_ms':0,'blocked':False}
                if (any(type(value.get(k)) is not int or value[k]<0 for k in ('count','quote_count','last_ms'))
                    or value['count']>self.total or value['quote_count']>min(value['count'],self.quotes)
                    or type(value.get('blocked')) is not bool):raise PublicFailure('CORRUPT_HTTP_BUDGET')
                if value['policy_hash']!=self.policy_hash:raise PublicFailure('BUDGET_POLICY_MISMATCH')
                if value['blocked']:raise PublicFailure('PUBLIC_ACCESS_PAUSED')
                if now<value['last_ms']:raise PublicFailure('CLOCK_ROLLBACK')
                quote=path=='/api/v5/market/candles'
                if value['count']>=self.total or quote and value['quote_count']>=self.quotes:
                    raise PublicFailure('HTTP_BUDGET_EXHAUSTED')
                wait=max(0,250-(now-value['last_ms']))
                if not wait:
                    value['count']+=1;value['quote_count']+=int(quote);value['last_ms']=now
                    # Reserve durably BEFORE the dispatch. Crashes consume budget.
                    audit.seal(self.path,value)
                    identity=audit.digest({'policy':self.policy_hash,'sequence':value['count']})
                    event={'request_id':identity,'path':path,'query':{k:v for k,v in (params or {}).items()
                        if k in {'instId','instType','bar','limit','after','before','ccy','uly','instFamily'}},
                        'dispatch_reserved_ms':now,'dispatch_reserved_utc':stamp(now),
                        'context':context,'execution_enabled':False}
                    intent=self.root/(identity+'.send.json')
                    if intent.exists():raise PublicFailure('HTTP_SEQUENCE_CONFLICT')
                    audit.seal(intent,event)
                    return event
            if deadline is not None and now+wait>=deadline:raise PublicFailure('WINDOW_EXHAUSTED')
            self.sleep(wait/1000)

    def finish(self, event):
        path=self.root/(event['request_id']+'.result.json')
        if path.exists():raise PublicFailure('HTTP_RECEIPT_CONFLICT')
        audit.seal(path,event)
        if event.get('http_status') in {401,403,429} or event.get('api_code') in {'50011','50040'}:
            with locked(self.path):
                value=audit.unseal(self.path);value['blocked']=True;audit.seal(self.path,value)

    def status(self):
        return audit.unseal(self.path) if self.path.exists() else {'count':0,'quote_count':0,'blocked':False}


class PublicClient(okx.OKXPublicClient):
    """Reuse native instrument/candle helpers; dedicated per-request deadline.

    HTTP dispatch is the owned Session.send boundary, not a packet timestamp.
    No global patch, custom URL, supplied auth, redirect or certificate bypass.
    """
    def __init__(self,budget,*,deadline=None,clock=okx.utc_ms,monotonic=time.monotonic):
        self.budget=budget;self.deadline=deadline;self.clock=clock;self.monotonic=monotonic
        self.context=None;self.last_record=None;self.last_rows=None;self.calls=0
        self.quote_reason=None

    def get(self,path,params=None):
        if path not in okx.PUBLIC_PATHS:raise PublicFailure('NON_PUBLIC_PATH')
        if path=='/api/v5/market/candles' and not self.context:raise PublicFailure('QUOTE_ATTEMPT_REQUIRED')
        self.calls+=1;self.last_record=None;self.last_rows=None
        owner=self;event=None;started=self.monotonic()
        class Session(requests.Session):
            def send(self,request,**kwargs):
                nonlocal event,started
                u=urlsplit(request.url)
                if request.method!='GET' or u.hostname!='www.okx.com' or u.path!=path:
                    raise PublicFailure('NON_PUBLIC_TRANSPORT')
                event=owner.budget.dispatch(path,params,owner.context,owner.deadline)
                remaining=1.0 if owner.deadline is None else min(1.,(owner.deadline-owner.clock())/1000)
                if remaining<.01:raise PublicFailure('WINDOW_EXHAUSTED')
                kwargs['timeout']=(remaining*.35,remaining*.65)
                started=owner.monotonic()
                event['http_dispatched_ms']=owner.clock()
                event['http_dispatched_utc']=stamp(event['http_dispatched_ms'])
                return super().send(request,**kwargs)
        try:
            with Session() as session:
                session.auth=lambda r:r  # suppress ambient .netrc; never add credentials
                with session.get(okx.BASE_URL+path,params=params or {},stream=True,allow_redirects=False) as response:
                    event['headers_received_ms']=self.clock();event['http_status']=response.status_code
                    if response.status_code!=200:raise PublicFailure('HTTP_STATUS_UNAVAILABLE')
                    body=bytearray()
                    for chunk in response.iter_content(8192):
                        body.extend(chunk)
                        if len(body)>1024*1024:raise PublicFailure('PUBLIC_RESPONSE_TOO_LARGE')
                        if self.monotonic()-started>1 or self.deadline is not None and self.clock()>self.deadline:
                            raise PublicFailure('REQUEST_DEADLINE')
                    event['response_received_ms']=self.clock()
                    if self.deadline is not None and event['response_received_ms']>self.deadline:
                        raise PublicFailure('REQUEST_DEADLINE')
                    try:payload=json.loads(body)
                    except (ValueError,UnicodeError):raise PublicFailure('MALFORMED_RESPONSE') from None
                    if not isinstance(payload,dict):raise PublicFailure('MALFORMED_RESPONSE')
                    code=str(payload.get('code',''))
                    event['api_code']=code if code.isdigit() and len(code)<=8 else 'INVALID'
                    if code!='0':raise PublicFailure('PUBLIC_API_UNAVAILABLE')
                    rows=payload.get('data')
                    if not isinstance(rows,list):raise PublicFailure('MALFORMED_RESPONSE')
                    self.last_rows=rows
                    event['summary']=summary(rows) if path=='/api/v5/market/candles' else {'rows':len(rows)}
                    event['response_fingerprint']=audit.digest(rows)
                    event['status']='RECEIVED'
                    return rows
        except PublicFailure as exc:
            if event is not None:event.update(status='FAILED',reason=exc.kind)
            raise
        except Exception as exc:
            if event is not None:event.update(status='FAILED',reason='TRANSPORT_UNAVAILABLE',error_type=type(exc).__name__)
            raise PublicFailure('TRANSPORT_UNAVAILABLE') from None
        finally:
            if event is not None:
                event.setdefault('status','INTERRUPTED_UNCONFIRMED')
                event.update(completed_ms=self.clock(),actual_http_attempts=int('http_dispatched_ms' in event))
                event['completed_utc']=stamp(event['completed_ms'])
                self.last_record=event;self.budget.finish(event)
