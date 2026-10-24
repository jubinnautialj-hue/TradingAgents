"""Optional deterministic price/volatility context and bounded public book tape.

No changes to strategy rules, candidate prices, execution or historical ledgers.
All book metrics are segment-local, displayed liquidity, not order intentions.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import statistics
import time
from datetime import UTC, datetime
from decimal import Decimal

from . import okx
from .okx_derivatives import number, parse_book
from .okx_multitimeframe import analyze_timeframe, structure_rules
from .okx_orderflow import digest, parse_trade


def price_action(bundle, config):
    """Latest closed bar against pivots available BEFORE that bar opened."""
    rows=bundle['records'];step=okx.BARS[bundle['interval']][1]
    if bundle['status']!='VALID' or len(rows)<250:
        raise ValueError('Validated warmup required')
    if any(r['ts']+step>bundle['cutoff_ms'] for r in rows):
        raise ValueError('Future or open bar in price-action input')
    if any(b['ts']-a['ts']!=step for a,b in zip(rows,rows[1:])):
        raise ValueError('Missing or unordered bars')
    original=analyze_timeframe(bundle,structure_rules(config))
    # Reuse confirmed-pivot implementation, not a second swing detector.
    pivots=original['confirmed_pivots']
    last,prev=rows[-1],rows[-2]
    known={k:[p for p in v if p['confirmed_at_ms']<=last['ts']] for k,v in pivots.items()}
    highs,lows=known['highs'],known['lows'];pattern='INSUFFICIENT_PIVOTS'
    if len(highs)>=2 and len(lows)>=2:
        pattern='UP' if highs[-1]['price']>highs[-2]['price'] and lows[-1]['price']>lows[-2]['price'] else (
            'DOWN' if highs[-1]['price']<highs[-2]['price'] and lows[-1]['price']<lows[-2]['price'] else 'MIXED')
    signals=[]
    for points,up in [(highs,True),(lows,False)]:
        if not points:continue
        level=points[-1]['price'];close=last['Close']
        crossed=prev['Close']<=level<close if up else prev['Close']>=level>close
        swept=last['High']>level and close<=level if up else last['Low']<level and close>=level
        if crossed:
            kind='BOS' if pattern==('UP' if up else 'DOWN') else 'CHoCH' if pattern==('DOWN' if up else 'UP') else 'UNCLASSIFIED_STRUCTURE_BREAK'
            signals.append({'type':kind,'direction':'UP' if up else 'DOWN','level':level,
                'confirmed_at_ms':last['ts']+step,'next_observable_bar_open_ms':last['ts']+step,
                'invalidation':'Later confirmed close returns '+('at/below' if up else 'at/above')+' this level'})
        if swept:
            signals.append({'type':'PRICE_SWEEP_REJECTION_CLUE','direction':'UPPER' if up else 'LOWER',
                'level':level,'confirmed_at_ms':last['ts']+step,
                'invalidation':'Later confirmed close beyond swept level',
                'scope':'Price shape only; not proof of stop execution, institutional flow or manipulation'})
        prior=[p for p in points if p['confirmed_at_ms']<=prev['ts']]
        if prior:
            p=prior[-1]['price']
            failed=prev['Close']>p and close<=p if up else prev['Close']<p and close>=p
            if failed:
                signals.append({'type':'BREAKOUT_FAILED_NEXT_BAR','direction':'UP' if up else 'DOWN','level':p,
                    'confirmed_at_ms':last['ts']+step,'invalidation':'A later close reclaims the original breakout direction'})
    calc=okx.indicator_frame(bundle);atr=[float(v) for v in calc['atr'].iloc[-21:]]
    if len(atr)!=21 or not all(math.isfinite(v) and v>0 for v in atr):raise ValueError('Invalid ATR history')
    baseline=statistics.median(atr[:-1]);ratio=atr[-1]/baseline
    closes=[r['Close'] for r in rows[-21:]];travel=sum(abs(b-a) for a,b in zip(closes,closes[1:]))
    efficiency=abs(closes[-1]-closes[0])/travel if travel else 0.
    tr=max(last['High']-last['Low'],abs(last['High']-prev['Close']),abs(last['Low']-prev['Close']))
    vol=last['Volume'];base_vol=statistics.mean(r['Volume'] for r in rows[-21:-1])
    return {'status':'VALID','interval':bundle['interval'],'source':bundle['source'],'snapshot_id':bundle['snapshot_id'],
        'start_ms':rows[-21]['ts'],'latest_close_ms':last['ts']+step,'cutoff_ms':bundle['cutoff_ms'],
        'pivot_pattern_before_last_bar':pattern,'support':lows[-1] if lows else None,'resistance':highs[-1] if highs else None,
        'signals':signals,'existing_price_events':original['events'],
        'volume_ratio_to_prior20':vol/base_vol if base_vol else None,
        'atr':atr[-1],'atr_percent':100*atr[-1]/last['Close'],'atr_ratio_to_prior20_median':ratio,
        'volatility_state':'EXPANDING' if ratio>=1.25 else 'CONTRACTING' if ratio<=.8 else 'NORMAL',
        'path_efficiency_20':efficiency,'market_environment':'TRENDING' if efficiency>=.6 else 'RANGING' if efficiency<=.25 else 'MIXED',
        'abnormal_true_range':tr>=3*baseline,
        'applicability':'Descriptive environment only; no automatic parameter changes or proven strategy edge'}


class BookTape:
    """A segment is valid only after a snapshot and unbroken sequence linkage."""
    def __init__(self,info):
        self.info=info;self.sides={'asks':{},'bids':{}};self.seq=None;self.points=[];self.changes=[];self.last_ts=None
        self.threshold=None
        self.change_count=0

    def apply(self,action,row,received_ms):
        if action not in {'snapshot','update'}:raise ValueError('Unexpected book action')
        seq,prev=int(row['seqId']),int(row['prevSeqId']);ts=int(row['ts'])
        if seq<0 or ts<=0 or ts>received_ms+1000 or self.last_ts is not None and ts<self.last_ts:
            raise ValueError('Invalid/future/retrograde book time or sequence')
        if action=='snapshot':
            if self.seq is not None or prev!=-1:raise ValueError('Unexpected snapshot; new segment required')
        elif self.seq is None or prev!=self.seq or seq<self.seq:
            raise ValueError('Sequence gap/reset; fresh snapshot required')
        elif seq==self.seq:
            if row['asks'] or row['bids']:raise ValueError('Repeated sequence with nonempty updates')
            return  # official empty heartbeat, no fabricated new observation
        sides=copy.deepcopy(self.sides)
        for side in sides:
            prices=set()
            for level in row[side]:
                if len(level)!=4:raise ValueError('Malformed level')
                price=Decimal(level[0]);size=number(level[1],nonnegative=True)
                if not price.is_finite() or price<=0 or price%Decimal(self.info['tickSz']) or price in prices:
                    raise ValueError('Invalid/off-tick/duplicate level')
                prices.add(price)
                if size==0:
                    if action=='snapshot':raise ValueError('Zero snapshot quantity')
                    sides[side].pop(price,None)
                else:sides[side][price]=list(level)
            # Public books channel carries only 400 levels per side.
            sides[side]=dict(sorted(sides[side].items(),reverse=side=='bids')[:400])
        parsed=parse_book({'ts':str(ts),**{k:list(v.values()) for k,v in sides.items()}},self.info,received_ms+1000,10000)
        top={k:parsed[k][:20] for k in ['asks','bids']}
        amounts=[r['notional_usdt'] for v in top.values() for r in v]
        threshold=self.threshold or max(10000,5*statistics.median(amounts))
        changes=[]
        if self.points:
            mult=float(self.info['ctVal'])*float(self.info.get('ctMult') or 1)
            for side in sides:
                for price in set(sides[side])|set(self.sides[side]):
                    before=float(self.sides[side].get(price,['0','0'])[1]);after=float(sides[side].get(price,['0','0'])[1])
                    if max(before,after)*float(price)*mult>=threshold and before!=after:
                        changes.append({'ts':ts,'side':side,'price':float(price),'before_contracts':before,
                            'after_contracts':after,'phenomenon':'DISPLAYED_SIZE_INCREASE' if after>before else 'DISPLAYED_SIZE_DECREASE',
                            'scope':'May include fills, replacements, cancellations or depth-boundary effects; not identified order intent'})
        bid=sum(r['base_quantity'] for r in top['bids']);ask=sum(r['base_quantity'] for r in top['asks'])
        point={'ts':ts,'received_ms':received_ms,'seq':seq,'mid':parsed['mid'],'spread_bps':parsed['spread_bps'],
            'bid_top20_base':bid,'ask_top20_base':ask,'imbalance_top20':(bid-ask)/(bid+ask),
            'bid_levels':len(top['bids']),'ask_levels':len(top['asks'])}
        self.sides=sides;self.seq=seq;self.last_ts=ts;self.threshold=threshold
        self.points.append(point);self.change_count+=len(changes)
        self.changes.extend(changes[:max(0,200-len(self.changes))])
        return point

    def summary(self,trades):
        if len(self.points)<2:return {'status':'PARTIAL','issues':['At least two continuous book observations required']}
        a,b=self.points[0],self.points[-1]
        matched=[r for r in trades if a['ts']<=r['ts']<=b['ts']]
        buy=sum(r['base'] for r in matched if r['side']=='buy');sell=sum(r['base'] for r in matched if r['side']=='sell')
        move=(b['mid']/a['mid']-1)*10000
        first=a['bid_top20_base']+a['ask_top20_base'];last=b['bid_top20_base']+b['ask_top20_base']
        return {'status':'VALID','start_ms':a['ts'],'end_ms':b['ts'],'observations':len(self.points),
            'mid_change_bps':move,'spread_start_bps':a['spread_bps'],'spread_end_bps':b['spread_bps'],
            'top20_depth_change_fraction':last/first-1,'imbalance_change':b['imbalance_top20']-a['imbalance_top20'],
            'liquidity_deterioration_clue':last/first<=.75 and b['spread_bps']>1.5*a['spread_bps'],
            'observed_trade_count':len(matched),'observed_buy_base':buy,'observed_sell_base':sell,'observed_delta_base':buy-sell,
            'possible_absorption_clue':abs(buy-sell)/(buy+sell)>=.6 and abs(move)<=2 if buy+sell else None,
            'trade_coverage':'Observed public trade stream only, not reconciled complete volume; ordering across channels not guaranteed',
            'depth_scope':'Top 20 visible levels per side, changing price ranges; size loss is not identified cancellation',
            'large_level_threshold_usdt':self.threshold,'large_level_changes_retained':len(self.changes),
            'large_level_changes_observed':self.change_count,'large_level_changes_truncated':self.change_count>len(self.changes),
            'invalidation':'Any sequence gap invalidates this segment; never concatenate across reconnects'}


async def sample_book(info,seconds):
    result={'status':'DATA_UNAVAILABLE','source':'wss://ws.okx.com:8443/ws/v5/public',
        'channels':['books','trades'],'segments':[],'issues':[],'reconnects':0,
        'resource_limits':{'seconds':seconds,'messages':400,'trades':1000,'changes_per_segment':200,'levels_per_side':400},
        'coverage_fraction':None,'continuous_across_segments':False}
    deadline=time.monotonic()+seconds;messages=0
    try:
        from websockets.asyncio.client import connect
        for attempt in range(2):
            if deadline-time.monotonic()<1:break
            tape=BookTape(info);trades=[];seen=set();valid=True;issue=[]
            segment={'start_received_ms':okx.utc_ms()}
            try:
                async with connect(result['source'],open_timeout=min(3,deadline-time.monotonic()),close_timeout=1,
                                   max_size=1_048_576,ping_interval=10,ping_timeout=5) as ws:
                    await ws.send(json.dumps({'op':'subscribe','args':[{'channel':c,'instId':info['instId']} for c in result['channels']]}))
                    while time.monotonic()<deadline and messages<400 and len(trades)<1000:
                        try:payload=await asyncio.wait_for(ws.recv(),min(2,max(.01,deadline-time.monotonic())))
                        except TimeoutError:continue
                        messages+=1;message=json.loads(payload);received=okx.utc_ms()
                        if message.get('event')=='error':raise ValueError('Public subscription rejected')
                        arg=message.get('arg',{})
                        if arg.get('instId')!=info['instId']:continue
                        if arg.get('channel')=='books':
                            for row in message.get('data',[]):tape.apply(message['action'],row,received)
                        elif arg.get('channel')=='trades':
                            for row in message.get('data',[]):
                                parsed=parse_trade(row,info,received+1000)
                                if parsed['trade_id'] not in seen and len(trades)<1000:
                                    seen.add(parsed['trade_id']);parsed['received_ms']=received;trades.append(parsed)
            except Exception as exc:
                valid=False;issue=[type(exc).__name__+': '+str(exc)[:120] if isinstance(exc,ValueError) else type(exc).__name__]
                if attempt==0:result['reconnects']+=1
            segment.update(end_received_ms=okx.utc_ms(),status='VALID' if valid and len(tape.points)>=2 else 'PARTIAL',
                issues=issue,points=tape.points,changes=tape.changes,trades=trades,
                summary=tape.summary(trades) if valid else {'status':'RESEARCH_INVALID','issues':['Segment continuity lost; time-series metrics suppressed']})
            result['segments'].append(segment)
            if len(trades)>=1000:result['issues'].append('Trade budget reached; incomplete observed stream')
            if valid or messages>=400:break
    except ImportError:result['issues'].append('Installed websockets runtime unavailable; not auto-installed')
    result['messages']=messages
    if messages>=400:result['issues'].append('Message budget reached; bounded sample only')
    if any(s['status']=='VALID' for s in result['segments']):result['status']='PARTIAL' if result['issues'] or result['reconnects'] else 'VALID'
    return result


def prepare_depth(symbol,day,config):
    seconds=int(config.get('okx_price_depth_seconds',10))
    if not 0<=seconds<=30:raise ValueError('Depth sample must be 0..30 seconds')
    if seconds==0 or day!=datetime.now(UTC).strftime('%Y-%m-%d'):
        return {'status':'DATA_UNAVAILABLE','issues':['Historical/disabled depth: no current substitution']}
    try:
        info=okx.OKXPublicClient().instrument(symbol)
        try:asyncio.get_running_loop()
        except RuntimeError:return asyncio.run(sample_book(info,seconds))
        return {'status':'DATA_UNAVAILABLE','issues':['Running event loop; synchronous depth sampler skipped']}
    except Exception as exc:return {'status':'DATA_UNAVAILABLE','issues':[type(exc).__name__]}


def collect_price_depth(bundle,config,sample=None,flow=None):
    root={'version':'okx-price-depth-v1','source':'OKX validated closed candles and public books/trades WS',
        'core_snapshot_id':bundle['snapshot_id'],'cutoff_ms':bundle['cutoff_ms'],'fetched_at_ms':okx.utc_ms(),
        'ttl_seconds':300,'status':'DATA_UNAVAILABLE','timeframes':{},'dynamic_book':copy.deepcopy(sample),
        'execution_enabled':False,'limitations':['Observations, not proof of institutional intent, manipulation or iceberg orders',
          'No automatic strategy adjustment; no demonstrated predictive improvement',
          'Hour/day volatility and second-level book samples have different windows; no causal conjunction']}
    if bundle['status']=='VALID':
        children=bundle.get('timeframes') or {bundle['interval']:bundle}
        for interval,child in children.items():
            try:root['timeframes'][interval]=price_action(child,config)
            except Exception as exc:root['timeframes'][interval]={'status':'DATA_UNAVAILABLE','issues':[type(exc).__name__]}
        valid=[v for v in root['timeframes'].values() if v['status']=='VALID']
        root['environment_conflict']=len({v['market_environment'] for v in valid})>1
        root['volatility_conflict']=len({v['volatility_state'] for v in valid})>1
        patterns={v['pivot_pattern_before_last_bar'] for v in valid}
        root['opposing_structure_conflict']={'UP','DOWN'}.issubset(patterns)
        root['structure_unresolved']=bool(patterns & {'MIXED','INSUFFICIENT_PIVOTS'})
        root['status']='VALID' if len(valid)==len(children) and valid else 'PARTIAL'
    if bundle.get('historical'):
        root['dynamic_book']={'status':'DATA_UNAVAILABLE','issues':['Historical book reconstruction unavailable; live replacement forbidden']}
    book=root.get('dynamic_book') or {'status':'DATA_UNAVAILABLE','issues':['No book sample']}
    for s in book.get('segments',[]):
        if s['end_received_ms']>root['cutoff_ms'] or any(p['ts']>root['cutoff_ms'] for p in s['points']) or any(t['ts']>root['cutoff_ms'] or t['received_ms']>root['cutoff_ms'] for t in s['trades']):
            s.update(status='RESEARCH_INVALID',points=[],changes=[],trades=[],summary={'status':'RESEARCH_INVALID','issues':['Beyond research cutoff']})
            book['status']='RESEARCH_INVALID'
    root['dynamic_book']=book
    root['orderflow_joint_status']='NOT_ALIGNED_OR_UNAVAILABLE'
    if flow:
        from .okx_orderflow import evidence_view
        f=evidence_view(flow)
        root['orderflow_reference']={'snapshot_id':f.get('snapshot_id'),'status':f['status'],'start_ms':f.get('start_ms'),'end_ms':f.get('end_ms')}
        root['orderflow_joint_status']='INDEPENDENT_WINDOWS_NO_SAME_BAR_CONFIRMATION'
        for interval,child in (bundle.get('timeframes') or {bundle['interval']:bundle}).items():
            m=f.get('markets',{}).get('swap',{})
            if f['status']=='VALID' and m.get('statistics') and child.get('records') and (
                f['start_ms']==child['records'][-1]['ts'] and f['end_ms']==child['latest_close_ms']):
                root.setdefault('same_bar_orderflow',{})[interval]={'start_ms':f['start_ms'],'end_ms':f['end_ms'],
                    'delta_base':m['statistics']['delta_base'],'volume_units':f.get('base_unit'),
                    'scope':'Exact same closed bar only; taker flow evidence, not proof of institutional intent'}
                root['orderflow_joint_status']='EXACT_WINDOW_AVAILABLE_FOR_LABELLED_TIMEFRAME_ONLY'
    if book['status']!='VALID' and root['status']=='VALID':root['status']='PARTIAL'
    root['snapshot_id']=digest(root)
    return root


def evidence_view(raw,now_ms=None):
    view=copy.deepcopy(raw);now=okx.utc_ms() if now_ms is None else now_ms
    if digest({k:v for k,v in raw.items() if k!='snapshot_id'})!=raw.get('snapshot_id'):
        return {'status':'RESEARCH_INVALID','issues':['Price/depth fingerprint mismatch'],'execution_enabled':False}
    if now-raw['cutoff_ms']>raw['ttl_seconds']*1000 or raw['fetched_at_ms']>now+60000:
        view.update(status='STALE_DATA',dynamic_book={'status':'STALE_DATA'},timeframes={},
            issues=['Original closed-bar analysis retained in archive; not current evidence'])
        for key in ['same_bar_orderflow','orderflow_reference','environment_conflict','volatility_conflict',
                    'opposing_structure_conflict','structure_unresolved']:
            view.pop(key,None)
        view['orderflow_joint_status']='STALE_DATA'
    return view


def render_price_depth(raw):
    view=evidence_view(raw)
    for s in view.get('dynamic_book',{}).get('segments',[]):
        for k in ['points','changes','trades']:s.pop(k,None)
    return 'Optional price action / volatility / continuous book segments\n'+json.dumps(view,ensure_ascii=False,allow_nan=False)
