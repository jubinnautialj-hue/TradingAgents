"""R1.3 isolated deterministic fixtures. Not production forward samples."""
import copy
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from tradingagents import paper_instruments as r, forward_paper as p, paper_timing
from tradingagents.forward_validation import digest, atomic, read
from tests.test_forward_paper import state, opening, bar, proof, funding, T, HOUR
from tests.test_forward_validation import setup as f1_setup, register as register_f1
from tests.test_perpetual_plan import research
from tests.test_observation_eligibility import CandleClient, bar as rawbar
from tests.test_perpetual_plan import PlanClient, CONFIG, choice, NOW, DAY


def profile(symbol, value='0.1', lot='0.01', enabled=True):
    info = state().info.copy()
    base = symbol.split('-')[0]
    info.update(instId=symbol, instFamily=base+'-USDT', ctValCcy=base,
                ctVal=value, lotSz=lot, minSz=lot)
    return r.InstrumentProfile(instrument=symbol, enabled=enabled,
        certificate=r.Certificate(instrument=symbol, fetched_ms=T-HOUR,
                                  specification=r.specification(info,symbol), public_evidence_hash='a'*64))


def scoped(tmp_path, symbol='ETH-USDT-SWAP', value='0.1', lot='0.01', direction='LONG'):
    config = profile(symbol,value,lot)
    core = r.engine(config,tmp_path/'registry')
    s = state(direction)
    s.info = config.certificate.specification.copy()
    s.identity.update(instrument=symbol, paper_protocol_hash=core.POLICY_HASH)
    s.revalidation['instrument_specification'] = {'specification':s.info.copy(),
        'source':'/api/v5/public/instruments','fetched_ms':s.revalidation['checked_ms']}
    spec = s.revalidation['instrument_specification']; spec['fingerprint']=digest(spec)
    return core, s


def rates(symbol, start=T, end=T+HOUR):
    result = funding(start,end)
    rows = result['evidence']['pages'][0]['rows']
    for row in rows: row['instId']=symbol
    result['evidence']['pages'][0]['hash']=digest(rows)
    return result


@pytest.mark.parametrize('symbol,value,lot',[
    ('ETH-USDT-SWAP','0.1','0.01'),('SOL-USDT-SWAP','1','0.1'),
    ('DOGE-USDT-SWAP','1000','0.001')])
@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('win',[False,True])
def test_generic_units_fill_exit_costs(tmp_path,symbol,value,lot,direction,win):
    core,s=scoped(tmp_path,symbol,value,lot,direction)
    opened=core.fill(s,opening(),T+1000)
    pos=opened.position
    assert opened.info['instId']==symbol  # no fake BTC identity ever enters math
    assert pos['base_quantity']==pytest.approx(pos['contracts']*float(value))
    assert pos['contracts']/float(lot)==pytest.approx(round(pos['contracts']/float(lot)))
    assert pos['entry_price']*pos['base_quantity']<=1000
    assert pos['entry_fee']==pytest.approx(pos['entry_price']*pos['base_quantity']*.0005)
    assert pos['entry_ms']==T+1000 and not opened.simulation_admitted
    opened=core.advance_bar(opened,bar(),T+HOUR)
    long=direction=='LONG'
    b=bar(T+HOUR,high=125 if (long and win) or (not long and not win) else 105,
          low=75 if (not long and win) or (long and not win) else 95)
    exited=core.advance_bar(opened,b,T+2*HOUR)
    result=core.settle(exited,rates(symbol,end=T+2*HOUR),[bar(),b],as_of_ms=T+20*HOUR)
    assert result.status=='SETTLED'
    assert (result.exit['net_pnl']>0)==win
    assert result.exit['net_pnl']==pytest.approx(result.exit['gross_pnl_after_slippage']-
        result.position['entry_fee']-result.exit['exit_fee']+result.funding['cashflow_usdt'])


def test_original_btc_functions_hashes_and_no_global_mutation(tmp_path):
    before=p.POLICY.copy();function=p.fill
    btc=r.engine(profile('BTC-USDT-SWAP'),tmp_path)
    eth,_=scoped(tmp_path)
    assert btc.fill is p.fill and btc.Ledger is p.Ledger
    assert btc.CODE_HASH==p.CODE_HASH and btc.POLICY_HASH==p.POLICY_HASH
    assert eth.advance_bar.__code__ is p.advance_bar.__code__
    assert eth.settle.__code__ is p.settle.__code__
    assert eth.scan.__code__ is p.scan.__code__
    assert eth.Ledger.append.__code__ is p.Ledger.append.__code__
    assert p.fill is function and p.POLICY==before
    assert eth.POLICY_HASH!=p.POLICY_HASH and eth.PROTOCOL.version==r.VERSION
    assert r.funding_verifier('ETH-USDT-SWAP').__code__ is r.diagnostics.verify_funding_evidence.__code__


@pytest.mark.parametrize('change',['early','late','missed','stale','backfill','unarmed','expired',
                                  'spec','units','min','tick','scope','proof','budget'])
def test_generic_guards_fail_closed(tmp_path,change):
    core,s=scoped(tmp_path);o=opening();now=T+1000;equity=10000
    if change=='early':o['request_started_ms']=T-1
    if change in {'late','missed'}:o['observed_ms']=now=T+16000
    if change=='stale':s.revalidation=proof(T-100000)
    if change=='backfill':o['collection_mode']='BACKFILL'
    if change=='unarmed':s.identity.pop('schedule_persisted_ms')
    if change=='expired':s.trigger['hypothesis_expires_ms']=T
    if change=='spec':s.info['ctVal']='1'
    if change=='units':s.info['ctValCcy']='BTC'
    if change=='min':s.info['minSz']='999999'
    if change=='tick':s.plan['stop']['price']=90.01
    if change=='scope':s.identity['instrument']='SOL-USDT-SWAP'
    if change=='proof':s.revalidation['instrument_specification']['fetched_ms']-=100000
    if change=='budget':equity=1
    with pytest.raises(ValueError):core.fill(s,o,now,equity)
    assert not s.position


@pytest.mark.parametrize('change',['gap','duplicate','future','unconfirmed','order'])
def test_generic_candle_guards(tmp_path,change):
    core,s=scoped(tmp_path);s=core.fill(s,opening(),T+1000);b=bar();when=T+HOUR
    if change in {'gap','duplicate','order'}:b['ts']+=HOUR if change=='gap' else -HOUR
    if change=='future':when-=1
    if change=='unconfirmed':b['confirm']='0'
    with pytest.raises(ValueError):core.advance_bar(s,b,when)


def test_actual_rate_events_not_fixed_schedule_and_not_forecast(tmp_path):
    core,s=scoped(tmp_path)
    check=core.verify_funding_evidence
    f=rates('ETH-USDT-SWAP',end=T+8*HOUR)
    assert check(f['records'],f['evidence'],T,T+8*HOUR)
    f['evidence']['pages'][0]['rows'][0]['instId']='BTC-USDT-SWAP'
    f['evidence']['pages'][0]['hash']=digest(f['evidence']['pages'][0]['rows'])
    with pytest.raises(ValueError):check(f['records'],f['evidence'],T,T+8*HOUR)
    f=rates('ETH-USDT-SWAP');f['evidence']['pages'][0]['rows'][0].pop('realizedRate')
    f['evidence']['pages'][0]['hash']=digest(f['evidence']['pages'][0]['rows'])
    with pytest.raises((ValueError,KeyError)):check(f['records'],f['evidence'],T,T+HOUR)


def test_independent_books_restart_idempotence_tamper_and_policy(tmp_path):
    books=[]
    for symbol in ['ETH-USDT-SWAP','SOL-USDT-SWAP']:
        core,s=scoped(tmp_path,symbol);ledger=core.Ledger(tmp_path/symbol,'FIXTURE')
        events=ledger.load()
        with p.locked(ledger.path):
            ledger.append(events,s.registration_id,'SCHEDULED',{'state':s.model_dump()},T-1000)
            ledger.append(events,s.registration_id,'SCHEDULED',{'state':s.model_dump()},T-1000)
        reopened=r.engine(profile(symbol),tmp_path/'registry').Ledger(tmp_path/symbol,'FIXTURE')
        assert len(reopened.load())==1
        books.append((core,s,ledger))
    assert books[0][0].POLICY_HASH!=books[1][0].POLICY_HASH
    raw=read(books[0][2].path);raw['events'][0]['payload']['state']['info']['ctVal']='9'
    atomic(books[0][2].path,raw)
    with pytest.raises(ValueError,match='fingerprint'):books[0][2].load()
    assert len(books[1][2].load())==1


def test_disabled_or_unknown_profiles_and_false_permissions(tmp_path):
    core,s=scoped(tmp_path)
    disabled=r.engine(profile('ETH-USDT-SWAP',enabled=False),tmp_path)
    assert not disabled.instrument_allowed('ETH-USDT-SWAP')
    with pytest.raises(ValueError):disabled.fill(s,opening(),T+1000)
    assert not core.instrument_allowed('XRP-USDT-SWAP')
    for field in ['simulation_admitted','allow_new_risk','execution_enabled']:
        with pytest.raises(ValueError):r.InstrumentConfig.model_validate({'profiles':[profile('BTC-USDT-SWAP').model_dump()],field:True})
    # The research watchlist does not grant paper permission.
    with pytest.raises(ValueError):r.InstrumentConfig(profiles=[profile('ETH-USDT-SWAP')])


@pytest.mark.parametrize('field,value',[('ctValCcy','USDT'),('ctVal','NaN'),('ctMult','NaN'),('minSz','0'),
                                       ('lotSz','-1'),('tickSz','0'),('state','suspend'),('instId','BTC-USDT-SWAP')])
def test_bad_specs_are_not_certifiable(field,value):
    config=profile('SOL-USDT-SWAP').model_dump();config['certificate']['specification'][field]=value
    with pytest.raises(ValueError):r.InstrumentProfile.model_validate(config)


def test_native_admission_guards_only_whitelist_replaced(f1_setup,tmp_path):
    record=register_f1(f1_setup)
    core=r.engine(profile('ETH-USDT-SWAP'),tmp_path)
    # Mismatched symbol remains rejected even though the old BTC-only reason is removed.
    reasons=core.admission(record,None,f1_setup[4][0])
    assert 'PAPER_INSTRUMENT_SCOPE_MISMATCH' in reasons and 'NO_VERIFIED_F2_CONDITION' in reasons
    assert 'PAPER_FUNDING_AUDIT_BTC_ONLY' not in reasons
    for update in [{'action':'WAIT'},{'workflow_status':'FAILED'},{'plan_valid':False},
                   {'origin':'HISTORICAL_IMPORT'},{'data_quality':'STALE_DATA'}]:
        assert core.admission(record.model_copy(update=update),None,f1_setup[4][0])


def test_registry_scope_verifies_before_filter_and_no_source_writes(f1_setup,tmp_path):
    record=register_f1(f1_setup);registry=f1_setup[0]
    source=tmp_path/'f2';source.mkdir();clock=f1_setup[4]
    files={path.name:path.read_bytes() for path in registry.root.iterdir() if path.is_file()}
    core=r.engine(profile('ETH-USDT-SWAP'),registry.root)
    class Market:
        def now(self):return clock[0]
    result=core.scan(registry.root,source,tmp_path/'extension',market=Market())
    assert result['research']['total']==0 and result['paper']['trades']==0 and result['paper']['win_rate'] is None
    assert files=={path.name:path.read_bytes() for path in registry.root.iterdir() if path.is_file()}
    path=registry.root/(record.registration_id+'.json');value=read(path);value['instrument']='ETH-USDT-SWAP';atomic(path,value)
    with pytest.raises(ValueError):core.scan(registry.root,source,tmp_path/'extension',market=Market())


def test_routes_keep_btc_path_and_separate_assets_and_configs(tmp_path):
    cfg=r.InstrumentConfig(profiles=[profile(s) for s in ['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP']])
    source={'name':'live','registry':str(tmp_path/'registry'),'f2':str(tmp_path/'f2'),'f3':str(tmp_path/'f3/live')}
    rows=r.routes(cfg,[source]);assert rows[0]['f3']==source['f3']
    assert len({row['f3'] for row in rows})==3
    eth_hash=rows[1]['_paper_engine'].POLICY_HASH
    cfg.profiles.append(profile('DOGE-USDT-SWAP','1000'))
    assert r.routes(cfg,[source])[1]['_paper_engine'].POLICY_HASH==eth_hash


def test_current_provenance_required_even_if_original_revalidation_hash_valid(tmp_path):
    core,s=scoped(tmp_path)
    s.revalidation.pop('instrument_specification')
    assert p.revalidation_ready(s.revalidation,s.trigger,T+1000,before=T)
    with pytest.raises(ValueError):core.fill(s,opening(),T+1000)


def test_schema_and_accepted_config():
    assert read(r.native.ROOT/'docs/paper_instruments_r13.schema.json')==TypeAdapter(
        r.InstrumentConfig|r.ExtensionProtocol|r.ExtensionJournal|r.ExtensionReport).json_schema()
    cfg=r.load_config(r.DEFAULT_CONFIG)
    assert [v.instrument for v in cfg.profiles if v.enabled]==['BTC-USDT-SWAP','ETH-USDT-SWAP','SOL-USDT-SWAP']


@pytest.mark.parametrize('symbol',['ETH-USDT-SWAP','SOL-USDT-SWAP'])
def test_full_native_research_f1_f2_schedule_fill_settlement(tmp_path_factory,monkeypatch,symbol):
    """Only temporary fixtures, never mixed into the actual forward registry."""
    root=tmp_path_factory.mktemp('r13')
    cfg=profile(symbol,'0.1' if symbol.startswith('ETH') else '1')
    cfg.certificate.fetched_ms=NOW-1000
    class AssetClient(PlanClient):
        def instrument(self,requested):
            assert requested==symbol
            return cfg.certificate.specification.copy()
        def candles(self,requested,interval,cutoff,count):
            rows=super().candles('BTC-USDT-SWAP',interval,cutoff,count)
            for row in rows:
                row[6]=str(float(row[5])*float(cfg.certificate.specification['ctVal']))
                row[7]=str(float(row[6])*float(row[4]))
            return rows
        def get(self,path,params=None):
            rows=super().get(path,params)
            mult=float(cfg.certificate.specification['ctVal'])
            for row in rows:
                if isinstance(row,dict) and 'instId' in row:
                    row['instId']=symbol.removesuffix('-SWAP') if path==p.deriv.INDEX else symbol
                    if path==p.deriv.OI:row.update(oiCcy=str(float(row['oi'])*mult),oiUsd=str(float(row['oi'])*mult*1000))
                if path==p.deriv.OI_HISTORY:row[2]=str(float(row[1])*mult)
            return rows
    clock=[NOW];monkeypatch.setattr(p.okx,'utc_ms',lambda:clock[0]);monkeypatch.setattr(p.deriv,'utc_ms',lambda:clock[0])
    bundle=p.okx.build_bundle(symbol,DAY,CONFIG,AssetClient())
    assert bundle['status']=='VALID',bundle
    research_state={'company_of_interest':symbol,'trade_date':DAY,'market_snapshot':bundle,'position_context':'UNKNOWN',
                    'research_id':symbol+'-isolated-fixture','portfolio_choice':choice(),'llm_status':'AVAILABLE','plan_mode':True}
    result,_=p.plans.finalize_result(research_state,bundle,CONFIG)
    assert result.plan_valid,result.missing_data
    research_state.update(research_result=result.model_dump(mode='json'),data_quality='VALID',workflow_status='COMPLETED')
    directory=root/'report';directory.mkdir();(directory/'complete_report.md').write_text('ISOLATED FIXTURE')
    atomic(directory/'research_result.json',research_state['research_result']);atomic(directory/'multitimeframe_snapshot.json',bundle)
    from tradingagents import forward_validation as f1
    monkeypatch.setattr(f1,'utc_ms',lambda:clock[0])
    registry=f1.Registry(root/'f1');clock[0]=NOW-1000;token=registry.begin(symbol,CONFIG);clock[0]=NOW+2000
    record=registry.register(directory,research_state,token=token,completed_ms=NOW+1000)
    assert record.origin=='PROSPECTIVE' and record.plan_valid and not record.allow_new_risk
    core=r.engine(cfg,registry.root)
    live=core.Market(AssetClient(now=clock[0]),p.f2.ScanLimits()).refresh(record,{},clock[0])
    assert live['validation']['status']=='VALID'
    assert live['instrument_specification']['specification']==cfg.certificate.specification
    assert live['instrument_specification']['source']=='/api/v5/public/instruments'
    entry=record.original_result['entry_reference']['price'];stop=record.original_result['stop_loss']['price'];gap=entry-stop
    f2root=root/'f2';clock[0]=record.first_check_ms+1000
    tracking=p.f2
    tracking.scan(registry.root,f2root,client=CandleClient(clock,[rawbar(record.first_bar_open_ms,entry,gap)]))
    clock[0]+=HOUR
    tracking.scan(registry.root,f2root,client=CandleClient(clock,[rawbar(record.first_bar_open_ms+HOUR,entry,gap,True)]))
    f1_original={x.name:x.read_bytes() for x in registry.root.iterdir() if x.is_file()}
    f2_original={x.name:x.read_bytes() for x in (f2root/'production').iterdir() if x.is_file()}
    core=r.engine(cfg,registry.root)
    target=record.original_result['take_profit'][0]['price']
    class Market:
        planned=None
        def now(self):return clock[0]
        def refresh(self,*args):
            result=proof(clock[0]-1)
            spec={'specification':cfg.certificate.specification.copy(),'source':cfg.certificate.source,'fetched_ms':clock[0]-1}
            spec['fingerprint']=digest(spec);result['instrument_specification']=spec
            return result
        def opening(self,requested,when):
            assert requested==symbol;self.planned=when
            return {**opening(when),'reference':entry+gap*.2}
        def bars(self,requested,start,end):
            assert requested==symbol
            rows=[]
            for ts in range(start,end,HOUR):
                h=entry+gap*.4;c=entry+gap*.3
                if self.planned is not None and ts>=self.planned+HOUR:h=target+gap;c=target+gap*.1
                rows.append(bar(ts,open_=entry+gap*.2,low=entry-gap*.05 if self.planned is None else entry+gap*.1,high=h,close=c))
            return {'bars':rows,'fetched_at_ms':clock[0]}
        def funding(self,requested,start,end):
            assert requested==symbol
            return rates(symbol,start,end)
    market=Market();output=root/'f3'
    value=core.scan(registry.root,f2root,output,market=market)
    assert value['research']['paper_scheduled']==1,value
    ledger=core.Ledger(output);scheduled=next(iter(ledger.states(ledger.load()).values()))
    from tests.test_automation import cfg_for
    monkeypatch.setattr(paper_timing.r11,'utc_ms',lambda:clock[0])
    source={'name':'isolated/'+symbol,'registry':str(registry.root),'f2':str(f2root),'f3':str(output),'_paper_engine':core}
    automation_config=cfg_for(root,registry.root)
    clock[0]=scheduled.planned_entry_ms-10000
    refreshed=paper_timing.step(automation_config,source,record.registration_id,'refresh',market,scheduled_for_ms=clock[0])
    assert refreshed['status']=='SCHEDULED',refreshed
    clock[0]=scheduled.planned_entry_ms+1000
    filled=paper_timing.step(automation_config,source,record.registration_id,'open',market,scheduled_for_ms=clock[0])
    assert filled['status']=='OPEN',filled
    value=core.report([record],ledger.load())
    assert value['paper']['trades']==1,value
    clock[0]+=2*HOUR;core.scan(registry.root,f2root,output,market=market)
    clock[0]=record.expires_ms+12*HOUR
    value=core.scan(registry.root,f2root,output,market=market)
    assert value['paper']['settled']==1,value
    assert not value['execution_enabled'] and not value['simulation_admitted']
    assert all(s['identity']['instrument']==symbol for s in value['trades'])
    count=len(ledger.load());core.scan(registry.root,f2root,output,market=market)
    assert len(ledger.load())==count
    assert f1_original=={x.name:x.read_bytes() for x in registry.root.iterdir() if x.is_file()}
    assert f2_original=={x.name:x.read_bytes() for x in (f2root/'production').iterdir() if x.is_file()}


def test_export_routes_only_opt_in_paper_and_no_install(tmp_path):
    from tests.test_automation import cfg_for
    cfg=cfg_for(tmp_path,tmp_path/'registry')
    profiles=r.load_config(r.DEFAULT_CONFIG)
    output=tmp_path/'export'
    result=r.export(cfg,profiles,Path('docs/automation_r12.json'),r.DEFAULT_CONFIG,output)
    assert not result['installed']
    import xml.etree.ElementTree as ET
    ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    for phase in ('research','observe','paper','timely'):
        args=ET.parse(output/('r12-'+phase+'.xml')).find('.//t:Arguments',ns).text
        assert ('tradingagents.paper_instruments' in args)==(phase in {'paper','timely'})
        if phase in {'paper','timely'}:assert '--expected-profile-hash' in args and '--expected-config-hash' in args
    assert '15' not in (output/'r12-timely.timer').read_text()  # no protocol TTL reconfiguration


def test_simultaneous_asset_scopes_do_not_patch_native_globals(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    pairs=[scoped(tmp_path,symbol) for symbol in ('ETH-USDT-SWAP','SOL-USDT-SWAP')]
    original=p.fill
    def invoke(index):
        core,s=pairs[index%2]
        return core.fill(s,opening(),T+1000).info['instId']
    with ThreadPoolExecutor(4) as pool:
        result=list(pool.map(invoke,range(40)))
    assert result==['ETH-USDT-SWAP','SOL-USDT-SWAP']*20
    assert p.fill is original


def append_worker(arguments):
    folder,symbol=arguments
    core,s=scoped(Path(folder),symbol)
    ledger=core.Ledger(folder,'FIXTURE')
    with p.locked(ledger.path):
        events=ledger.load()
        ledger.append(events,s.registration_id,'SCHEDULED',{'state':s.model_dump()},T)
    return len(ledger.load())


def test_multiprocess_restart_same_asset_no_duplicate_journal(tmp_path_factory):
    import multiprocessing
    folder=tmp_path_factory.mktemp('r13p')
    args=(str(folder),'ETH-USDT-SWAP')
    with multiprocessing.get_context('spawn').Pool(2) as pool:
        assert pool.map(append_worker,[args,args])==[1,1]


@pytest.mark.parametrize('symbol',['ETH-USDT-SWAP','SOL-USDT-SWAP'])
def test_generic_market_actual_funding_fetch_preserves_symbol_and_interval(tmp_path,monkeypatch,symbol):
    core,_=scoped(tmp_path,symbol)
    f=rates(symbol,end=T+8*HOUR);server=f['evidence']['server_ms']
    monkeypatch.setattr(p.okx,'utc_ms',lambda:server)
    class Client:
        def get(self,path,params=None):
            if path=='/api/v5/public/time':return [{'ts':str(server)}]
            assert path==p.deriv.FUNDING_HISTORY and params['instId']==symbol
            return f['evidence']['pages'][0]['rows']
    result=core.Market(Client(),p.f2.ScanLimits()).funding(symbol,T,T+8*HOUR)
    assert result['records']==f['records']
    assert core.verify_funding_evidence(result['records'],result['evidence'],T,T+8*HOUR)=={'4.0':3}
    with pytest.raises(ValueError):core.Market(Client(),p.f2.ScanLimits()).funding('BTC-USDT-SWAP',T,T+HOUR)


def test_certified_actual_spec_quantity_and_price_precision(tmp_path):
    cfg=r.load_config(r.DEFAULT_CONFIG)
    prices={'BTC-USDT-SWAP':100000,'ETH-USDT-SWAP':3000,'SOL-USDT-SWAP':200}
    for item in cfg.profiles:
        core=r.engine(item,tmp_path)
        s=state();s.info=item.certificate.specification.copy();price=prices[item.instrument]
        s.plan={'direction':'LONG','entry':{'price':price},'stop':{'price':price*.9},'target':{'price':price*1.2}}
        s.identity.update(instrument=item.instrument,paper_protocol_hash=core.POLICY_HASH)
        spec={'specification':s.info.copy(),'source':item.certificate.source,'fetched_ms':s.revalidation['checked_ms']}
        spec['fingerprint']=digest(spec);s.revalidation['instrument_specification']=spec
        result=core.fill(s,{**opening(),'reference':price},T+1000)
        from decimal import Decimal
        assert Decimal(str(result.position['entry_price']))%Decimal(s.info['tickSz'])==0
        assert Decimal(str(result.position['contracts']))%Decimal(s.info['lotSz'])==0
        assert result.position['contracts']>=float(s.info['minSz'])
        assert result.position['base_quantity']==pytest.approx(result.position['contracts']*p.deriv.multiplier(s.info))


def test_book_removal_is_not_silent_exposure_loss(tmp_path):
    cfg=r.InstrumentConfig(profiles=[profile('BTC-USDT-SWAP')])
    source={'name':'live','registry':str(tmp_path/'registry'),'f2':str(tmp_path/'f2'),'f3':str(tmp_path/'f3/live')}
    (tmp_path/'f3/instruments-r13/live/ETH-USDT-SWAP').mkdir(parents=True)
    with pytest.raises(ValueError,match='omitted'):r.routes(cfg,[source])
