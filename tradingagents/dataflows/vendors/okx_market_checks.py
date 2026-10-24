"""Optional public crowding / cross-venue observations, never execution signals.

No historical substitution, credentials, background daemon or heatmap estimates.
Liquidation feeds are samples. Transport coverage is not exchange coverage.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import threading
import time
from datetime import UTC, datetime

import requests

from . import okx
from .okx_derivatives import multiplier, number, parse_funding_history, parse_oi_history
from .okx_orderflow import digest

HOUR = 3_600_000
RATIOS = {
    "accounts": "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
    "top_accounts": "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader",
    "top_positions": "/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader",
}
BINANCE_PATHS = frozenset({"/fapi/v1/time", "/fapi/v1/exchangeInfo", "/fapi/v1/klines",
    "/fapi/v1/fundingRate", "/futures/data/openInterestHist",
    "/futures/data/globalLongShortAccountRatio"})
_LOCK = threading.Lock()
_LAST = 0.


class BinancePublicClient:
    """Fixed public GET allowlist, per-run cache, pacing, bounded transient retries.

    No key headers, .netrc authentication, redirects, user URL or private routes.
    """
    def __init__(self):
        self.cache = {}

    def get(self, path, params=None):
        global _LAST
        if path not in BINANCE_PATHS or any(k.lower() in {"signature", "apikey", "api_key", "token"} for k in (params or {})):
            raise ValueError("Only credential-free public Binance paths permitted")
        key = json.dumps([path, params], sort_keys=True)
        if key in self.cache:
            return copy.deepcopy(self.cache[key])
        failure = "unavailable"
        for attempt in range(3):
            with _LOCK:
                time.sleep(max(0., .25 - (time.monotonic()-_LAST)))
                _LAST = time.monotonic()
            delay = 0.
            try:
                with requests.Session() as session:
                    session.auth = lambda r: r
                    response = session.get("https://fapi.binance.com"+path, params=params or {},
                                           timeout=(5, 15), allow_redirects=False)
                status = response.status_code
                if status == 429 or status >= 500:
                    failure = f"HTTP_{status}"
                    try:
                        delay = min(30., max(0., float(response.headers.get("Retry-After", 0))))
                    except ValueError:
                        pass
                elif status != 200:
                    # Includes 418 ban, 451 region restrictions, authentication: stop.
                    raise ValueError(f"Binance public HTTP_{status}; no bypass attempted")
                else:
                    value = response.json()
                    if not isinstance(value, (dict, list)) or isinstance(value, dict) and "code" in value:
                        raise ValueError("Binance public API/malformed response")
                    self.cache[key] = value
                    return copy.deepcopy(value)
            except (requests.Timeout, requests.ConnectionError) as exc:
                failure = type(exc).__name__
            if attempt < 2:
                time.sleep(max(delay, 2**attempt))
        raise ValueError("Binance transient retries exhausted: "+failure)


def stamp(value, ceiling):
    t = int(value)
    if t <= 0 or t > ceiling:
        raise ValueError("Future/invalid observation timestamp")
    return t


def series(rows, cutoff, *, ratio=False):
    """Hourly point observations, not time-integrated accounts or positions."""
    result = []
    for row in rows:
        t = stamp(row[0], cutoff)
        if t % HOUR or any(r["ts"] == t for r in result):
            raise ValueError("Duplicate/unaligned observations")
        result.append({"ts": t, "ratio" if ratio else "base_oi": number(row[1], nonnegative=True)})
    result.sort(key=lambda r: r["ts"])
    if len(result) < 2 or result[-1]["ts"]-result[-2]["ts"] != HOUR:
        raise ValueError("Insufficient contiguous hourly observations")
    if cutoff-result[-1]["ts"] > 2*HOUR:
        raise ValueError("Stale hourly observations")
    field = "ratio" if ratio else "base_oi"
    return {"records": result[-3:], "change_fraction": result[-1][field]/result[-2][field]-1 if result[-2][field] else None,
            "as_of_ms": result[-1]["ts"], "period_ms": HOUR}


def liquidations(message, exchange, info, received_ms):
    """Normalize samples; values are proxies, NOT actual account liquidation PnL."""
    symbol = info["instId"].replace("-", "").removesuffix("SWAP")
    rows = []
    if exchange == "OKX":
        if message.get("arg", {}).get("channel") != "liquidation-orders":
            return []
        for group in message.get("data", []):
            if group.get("instId") != info["instId"] or group.get("instType") != "SWAP":
                continue
            for r in group["details"]:
                side = {"sell": "LONG", "buy": "SHORT"}[r["side"]]
                if r.get("posSide") not in {None, "net", side.lower()}:
                    raise ValueError("Liquidation side conflict")
                rows.append((r["ts"], side, number(r["sz"], positive=True)*multiplier(info),
                             number(r["bkPx"], positive=True), "liquidation_mark_price_proxy"))
    else:
        if message.get("e") != "forceOrder":
            return []
        r = message["o"]
        if r["s"] != symbol or r.get("st", message.get("st", 1)) not in {1, "1"}:
            return []
        # z is cumulative executed quantity in this snapshot, never original q.
        if number(r["z"], nonnegative=True) == 0:
            return []
        rows.append((r["T"], {"SELL": "LONG", "BUY": "SHORT"}[r["S"]],
                     number(r["z"], positive=True), number(r["ap"], positive=True), "snapshot_average_fill"))
    out = []
    for ts, side, base, price, method in rows:
        value = base*price
        if not math.isfinite(value):
            raise ValueError("Liquidation conversion overflow")
        row = {"ts": stamp(ts, received_ms), "received_ms": received_ms, "exchange": exchange,
               "side_liquidated": side, "base_quantity": base, "sample_notional_usdt": value,
               "price": price, "valuation_method": method}
        row["event_key"] = digest({k: v for k,v in row.items() if k != "received_ms"})
        out.append(row)
    return out


async def _listen(exchange, info, seconds):
    result = {"exchange": exchange, "status": "DATA_UNAVAILABLE", "events": [], "segments": [],
              "issues": [], "coverage_fraction": None, "exchange_complete": False, "reconnects": 0}
    deadline = time.monotonic()+seconds
    symbol = info["instId"].replace("-", "").removesuffix("SWAP").lower()
    url = "wss://ws.okx.com:8443/ws/v5/public" if exchange == "OKX" else f"wss://fstream.binance.com/market/ws/{symbol}@forceOrder"
    result["source"] = url
    seen = set()
    try:
        from websockets.asyncio.client import connect
        for attempt in range(2):
            if deadline-time.monotonic() < 1:
                break
            segment = None
            try:
                async with connect(url, open_timeout=min(3, deadline-time.monotonic()), close_timeout=1,
                                   max_size=1_048_576, ping_interval=10, ping_timeout=5) as ws:
                    if exchange == "OKX":
                        await ws.send(json.dumps({"op":"subscribe", "args":[{"channel":"liquidation-orders", "instType":"SWAP"}]}))
                    else:
                        segment = {"start_ms":okx.utc_ms()}; result["segments"].append(segment)
                    while time.monotonic() < deadline and len(result["events"]) < 500:
                        try:
                            payload = await asyncio.wait_for(ws.recv(), min(2, max(.01,deadline-time.monotonic())))
                        except TimeoutError:
                            continue
                        message = json.loads(payload)
                        if message.get("event") == "error":
                            raise ValueError("Public subscription rejected")
                        if message.get("event") == "subscribe" and segment is None:
                            segment={"start_ms":okx.utc_ms()};result["segments"].append(segment)
                        for row in liquidations(message, exchange, info, okx.utc_ms()):
                            if row["event_key"] not in seen:
                                seen.add(row["event_key"]);result["events"].append(row)
                    break
            except Exception as exc:
                result["issues"].append(type(exc).__name__)
                if attempt == 0:
                    result["reconnects"] += 1
            finally:
                if segment is not None:
                    segment["end_ms"] = okx.utc_ms()
        if result["segments"]:
            result["status"] = "PARTIAL"  # even an uninterrupted socket cannot prove exchange completeness
    except ImportError:
        result["issues"].append("Installed websockets runtime unavailable; not installed automatically")
    result["issues"].append("Sampled feed; no historical backfill; disconnect gaps unknown; empty sample is not zero market liquidations")
    return result


def prepare_liquidations(symbol, day, config):
    seconds = int(config.get("okx_market_checks_liquidation_seconds", 10))
    if not 0 <= seconds <= 30:
        raise ValueError("Liquidation sampling must be 0..30 seconds")
    if day != datetime.now(UTC).strftime("%Y-%m-%d") or seconds == 0:
        return {"status":"DATA_UNAVAILABLE", "issues":["Historical or disabled sampling; no current substitution"]}
    try:
        info = okx.OKXPublicClient().instrument(symbol)
        async def run():
            return await asyncio.gather(_listen("OKX", info, seconds), _listen("BINANCE", info, seconds))
        # A running notebook loop must not be disrupted by asyncio.run.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return {"markets": asyncio.run(run()), "instrument":symbol, "status":"PARTIAL"}
        return {"status":"DATA_UNAVAILABLE", "issues":["Synchronous sampling unavailable inside running event loop"]}
    except Exception as exc:
        return {"status":"DATA_UNAVAILABLE", "issues":[type(exc).__name__]}


def funding(rows, cutoff, exchange, symbol):
    if exchange == "OKX":
        result = parse_funding_history(rows, symbol, cutoff)
    else:
        records=[]
        for r in rows:
            if r["symbol"] != symbol:
                raise ValueError("Funding instrument mismatch")
            records.append({"ts":stamp(r["fundingTime"],cutoff), "realized_rate":number(r["fundingRate"])})
        if len({r['ts'] for r in records}) != len(records):
            raise ValueError("Duplicate funding settlement")
        result={"records": sorted(records,key=lambda r:r['ts'])}
    records=result["records"]
    if len(records)<2 or cutoff-records[-1]['ts'] > 24*HOUR:
        raise ValueError("Insufficient or stale funding settlements")
    result['observed_intervals_hours']=[(b['ts']-a['ts'])/HOUR for a,b in zip(records,records[1:])]
    result['as_of_ms']=records[-1]['ts']
    result['scope']='Realized fractions per actual settlement, not forecast or annualized; gaps may be missing records, never assumed fixed schedule'
    return result


def collect_checks(bundle, config, samples=None, client=None, binance=None):
    cutoff=bundle['cutoff_ms']; symbol=bundle['symbol']; end=cutoff//HOUR*HOUR
    root={'version':'okx-market-checks-v1','exchange':'OKX','instrument':symbol,'core_snapshot_id':bundle['snapshot_id'],
          'research_cutoff_ms':cutoff,'fetched_at_ms':okx.utc_ms(),'ttl_seconds':300,'status':'DATA_UNAVAILABLE',
          'components':{},'comparison':None,'execution_enabled':False,
          'limitations':['Optional observations, not strategy signals or causal leadership',
            'Accounts, top-5-percent OKX positions, OI and taker flow are distinct populations',
            'No historical publication-time archive or historical liquidation replay',
            'Liquidation mark/snapshot notionals are samples, not full exchange losses or heatmaps',
            'Binance top-trader endpoints documented as MARKET_DATA/key-required; not queried',
            'Existing V2.1 flow has its own window; do not compare it with hourly volume as if synchronous']}
    if bundle.get('historical') or bundle['status']!='VALID':
        root['issues']=['Historical/invalid research: new current feeds not requested'];root['snapshot_id']=digest(root);return root
    client=client or okx.OKXPublicClient();binance=binance or BinancePublicClient();info=bundle['instrument']
    okx.validate_instrument(info,symbol)
    def capture(name, source, loader, scope):
        p={'source':source,'scope':scope,'fetched_at_ms':okx.utc_ms(),'status':'DATA_UNAVAILABLE','issues':[]}
        root['components'][name]=p
        try:
            data=loader();p.update(data=data,status='VALID',as_of_ms=data['as_of_ms'])
        except Exception as exc:
            p['issues']=[type(exc).__name__+': '+str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__]
        p['fetched_at_ms']=okx.utc_ms()
    for name,path in RATIOS.items():
        capture('okx_'+name,path,lambda path=path:series(client.get(path,{'instId':symbol,'period':'1H','end':str(cutoff),'limit':'3'}),cutoff,ratio=True),
                'contract net-long/net-short '+('position value, top 5% by open position value' if name=='top_positions' else 'account counts'+(', top 5% by open position value' if name=='top_accounts' else '')))
    def oi_okx():
        parsed=parse_oi_history(client.get('/api/v5/rubik/stat/contracts/open-interest-history',{'instId':symbol,'period':'1H','end':str(cutoff),'limit':'3'}),info,cutoff,'1h')
        return series([[r['ts'],r['base_quantity']] for r in parsed['records']],cutoff)
    capture('okx_oi','/api/v5/rubik/stat/contracts/open-interest-history',oi_okx,info['ctValCcy']+' OI, converted from contracts; changes at contiguous hourly points')
    existing=bundle.get('derivatives',{}).get('components',{}).get('funding_history',{})
    def okx_funding():
        if existing.get('status')=='VALID' and existing.get('data',{}).get('records'):
            records=existing['data']['records'][-3:]
            rows=[{'instId':symbol,'instType':'SWAP','fundingTime':str(r['ts']),'fundingRate':str(r['forecast_rate']),'realizedRate':str(r['realized_rate'])} for r in records]
        else:
            rows=client.get('/api/v5/public/funding-rate-history',{'instId':symbol,'after':str(cutoff+1),'limit':'3'})
        return funding(rows,cutoff,'OKX',symbol)
    capture('okx_funding','/api/v5/public/funding-rate-history',okx_funding,'settled funding, fraction per observed event; reuse existing data when available')
    bs=info['ctValCcy']+'USDT'
    try:
        server=int(binance.get('/fapi/v1/time')['serverTime'])
        if abs(server-okx.utc_ms())>60_000:
            raise ValueError('Binance clock skew')
        instruments=binance.get('/fapi/v1/exchangeInfo')['symbols']
        if not any(r['symbol']==bs and r['baseAsset']==info['ctValCcy'] and r['quoteAsset']=='USDT' and r['marginAsset']=='USDT' and r['contractType']=='PERPETUAL' and r['status']=='TRADING' for r in instruments):
            raise ValueError('No exact linear USDT perpetual mapping')
    except Exception as exc:
        root['components']['binance_identity']={'status':'DATA_UNAVAILABLE','issues':[type(exc).__name__],'source':'/fapi/v1/exchangeInfo'}
    else:
        params={'symbol':bs,'period':'1h','endTime':cutoff,'limit':3}
        def boi():
            rows=binance.get('/futures/data/openInterestHist',params)
            if any(r['symbol']!=bs for r in rows):raise ValueError('OI identity mismatch')
            return series([[r['timestamp'],r['sumOpenInterest']] for r in rows],cutoff)
        capture('binance_oi','/futures/data/openInterestHist',boi,info['ctValCcy']+' OI; hourly point observations')
        def bratio():
            rows=binance.get('/futures/data/globalLongShortAccountRatio',params)
            if any(r['symbol']!=bs for r in rows):raise ValueError('Ratio identity mismatch')
            return series([[r['timestamp'],r['longShortRatio']] for r in rows],cutoff,ratio=True)
        capture('binance_accounts','/futures/data/globalLongShortAccountRatio',bratio,'net long/short account counts, not position value')
        capture('binance_funding','/fapi/v1/fundingRate',lambda:funding(binance.get('/fapi/v1/fundingRate',{'symbol':bs,'startTime':cutoff-24*HOUR,'endTime':cutoff,'limit':100}),cutoff,'BINANCE',bs),'settled funding fraction, observed times; not account fees')
        def prices():
            child=bundle.get('timeframes',{}).get('1h',bundle if bundle.get('interval')=='1h' else {})
            if child.get('records'):
                # Original validated dataframe records use epoch ts and labelled OHLCV.
                df=okx.frame(child); a=[r for r in df.to_dict('records') if int(r['ts'])==end-HOUR]
                if len(a)!=1:raise ValueError('Missing matching OKX hour')
                a=a[0];oc=float(a['Close']);ov=float(a['Volume']);oq=float(a['VolumeQuote']);op=float(a['Open'])
            else:
                raw=client.candles(symbol,'1h',cutoff,2)
                df,_=okx.validate_candles(raw,'1h',cutoff,2,2,0)
                a=df.iloc[-1];oc=float(a.Close);ov=float(a.Volume);oq=float(a.VolumeQuote);op=float(a.Open)
            rows=binance.get('/fapi/v1/klines',{'symbol':bs,'interval':'1h','startTime':end-HOUR,'endTime':end-1,'limit':1})
            if len(rows)!=1:raise ValueError('Missing Binance matching hour')
            b=rows[0]
            if int(b[0])!=end-HOUR or int(b[6])!=end-1:raise ValueError('Unaligned/incomplete Binance hour')
            vals=[number(b[i],nonnegative=i in {5,7,9,10},positive=i in {1,2,3,4}) for i in [1,2,3,4,5,7,9,10]]
            bp,bh,bl,bc,bv,bq,buy,buyq=vals
            if not bl<=min(bp,bc)<=max(bp,bc)<=bh or buy>bv or buyq>bq:raise ValueError('Invalid Binance OHLC/taker units')
            return {'as_of_ms':end,'start_ms':end-HOUR,'end_ms':end,'okx_close':oc,'binance_close':bc,
                'binance_vs_okx_close_bps':(bc/oc-1)*10000,'okx_return':oc/op-1,'binance_return':bc/bp-1,
                'okx_volume_base':ov,'binance_volume_base':bv,'okx_volume_usdt':oq,'binance_volume_usdt':bq,
                'binance_taker_buy_base':buy,'binance_taker_sell_base':bv-buy,'binance_delta_base':2*buy-bv,
                'binance_delta_usdt':2*buyq-bq,'scope':'Same completed UTC hour; closes may be different trades, not executable arbitrage. No OKX hourly taker direction inferred.'}
        capture('prices_volume','OKX validated 1h + Binance /fapi/v1/klines',prices,'matching closed UTC hour; base coin and USDT')
    components=root['components']; comparison={}
    def data(name):
        p=components.get(name,{})
        return p.get('data') if p.get('status')=='VALID' else None
    oi_a,oi_b=data('okx_oi'),data('binance_oi')
    if oi_a and oi_b and [r['ts'] for r in oi_a['records'][-2:]]==[r['ts'] for r in oi_b['records'][-2:]]:
        comparison['aligned_oi']={'from_ms':oi_a['records'][-2]['ts'],'to_ms':oi_a['as_of_ms'],
            'okx_change_fraction':oi_a['change_fraction'],'binance_change_fraction':oi_b['change_fraction'],
            'unit':info['ctValCcy'],'note':'Separate venues; absolute positions are not added or treated as equal market shares'}
    p=data('prices_volume')
    if p:
        for venue in ['okx','binance']:
            oi=data(venue+'_oi')
            if oi and [r['ts'] for r in oi['records'][-2:]]==[p['start_ms'],p['end_ms']]:
                change=oi['change_fraction'];ret=p[venue+'_return']
                comparison[venue+'_price_oi']={'start_ms':p['start_ms'],'end_ms':p['end_ms'],
                    'price_return':ret,'oi_change_fraction':change,
                    'opposing_signs':change is not None and change*ret<0,
                    'interpretation':'Observed sign disagreement only; OI is not directional positioning'}
    fa,fb=data('okx_funding'),data('binance_funding')
    if fa and fb:
        a,b=fa['records'][-2:],fb['records'][-2:]
        if [r['ts'] for r in a]==[r['ts'] for r in b]:
            comparison['aligned_settlement']={'from_ms':a[0]['ts'],'to_ms':a[1]['ts'],
                'okx_rate':a[1]['realized_rate'],'binance_rate':b[1]['realized_rate'],
                'difference_fraction':b[1]['realized_rate']-a[1]['realized_rate']}
    root['comparison']=comparison or None
    liq=copy.deepcopy(samples or {'status':'DATA_UNAVAILABLE','issues':['No bounded sample collected']})
    for market in liq.get('markets',[]):
        events=market['events']
        if any(r['ts']>cutoff or r['received_ms']>cutoff for r in events) or any(s['end_ms']>cutoff for s in market['segments']):
            market.update(status='RESEARCH_INVALID',events=[],issues=['Sample beyond research cutoff']);continue
        market['summary']={'observed_events':len(events),'long_sample_usdt':sum(r['sample_notional_usdt'] for r in events if r['side_liquidated']=='LONG'),
            'short_sample_usdt':sum(r['sample_notional_usdt'] for r in events if r['side_liquidated']=='SHORT'),
            'minute_buckets':{str(t):sum(r['sample_notional_usdt'] for r in events if r['ts']//60000*60000==t) for t in sorted({r['ts']//60000*60000 for r in events})}}
        ordered=sorted(events,key=lambda r:r['ts'])
        market['summary']['observed_price_change']=ordered[-1]['price']/ordered[0]['price']-1 if len(ordered)>1 else None
        market['summary']['price_scope']='Sample event prices only; mixed mark/fill conventions across venues; not causal and not continuous market return'
    root['liquidations']=liq
    root['status']='PARTIAL'  # incomplete public liquidation coverage is intrinsic
    root['snapshot_id']=digest(root)
    return root


def evidence_view(raw, now_ms=None):
    now=okx.utc_ms() if now_ms is None else now_ms;view=copy.deepcopy(raw)
    if digest({k:v for k,v in raw.items() if k!='snapshot_id'})!=raw.get('snapshot_id'):
        return {'status':'RESEARCH_INVALID','issues':['Market checks fingerprint mismatch'],'execution_enabled':False}
    if now-raw['research_cutoff_ms']>raw['ttl_seconds']*1000 or raw['fetched_at_ms']>now+60000:
        view.update(status='STALE_DATA',comparison=None)
        for p in view['components'].values():p.update(status='STALE_DATA');p.pop('data',None)
        view['liquidations']={'status':'STALE_DATA','issues':['Original sample retained only in archive, not current evidence']}
    return view


def render_checks(raw):
    view=evidence_view(raw)
    for m in view.get('liquidations',{}).get('markets',[]):m.pop('events',None)
    return 'Optional public crowding / cross-exchange / liquidation samples\n'+json.dumps(view,ensure_ascii=False,allow_nan=False)
