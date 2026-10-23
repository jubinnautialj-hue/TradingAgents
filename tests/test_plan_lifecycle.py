import copy
import pytest
from tradingagents.dataflows.plan_lifecycle import (
    HOUR, timing, evaluate_signal, prepare_historical_signal, entry_evidence_ready,
)
from tradingagents.dataflows import perpetual_plan

CUTOFF = HOUR * 100
PLAN = {"direction":"LONG", "entry":{"price":100}, "stop":{"price":90}, "target":{"price":120}}

def bar(i, close=102, low=101):
    return {"ts":CUTOFF+i*HOUR,"open":102,"high":max(104,close),"low":min(low,close),"close":close,"confirm":"1"}

def test_next_bar_retest_and_longer_than_15_minutes():
    clock=timing(CUTOFF,{})
    a=evaluate_signal(PLAN,clock,[bar(0)],CUTOFF+HOUR)
    assert a.state=="PENDING_RETEST" and a.next_tradable_ms is None
    b=evaluate_signal(PLAN,clock,[bar(0),bar(1,low=99)],CUTOFF+2*HOUR)
    assert b.state=="ENTRY_ELIGIBLE"
    assert b.next_tradable_ms==CUTOFF+2*HOUR
    assert not b.allow_new_risk and not b.execution_enabled
    assert not entry_evidence_ready({"cutoff_ms":CUTOFF},b,{}, {},{},CUTOFF+2*HOUR)

def test_multiple_bars_before_retest():
    rows=[bar(0),bar(1),bar(2),bar(3,low=99)]
    assert evaluate_signal(PLAN,timing(CUTOFF,{}),rows,CUTOFF+4*HOUR).retest_confirmed_ms==CUTOFF+4*HOUR

def test_short():
    plan={"direction":"SHORT","entry":{"price":100},"stop":{"price":110},"target":{"price":80}}
    rows=[dict(bar(0),open=98,high=99,low=96,close=98),dict(bar(1),open=98,high=101,low=97,close=99)]
    assert evaluate_signal(plan,timing(CUTOFF,{}),rows,CUTOFF+2*HOUR).state=="ENTRY_ELIGIBLE"

@pytest.mark.parametrize("kind",["expired","stop","structure","quality"])
def test_invalidations(kind):
    result=evaluate_signal(PLAN,timing(CUTOFF,{}),[bar(0,close=89)] if kind=="stop" else [],
        CUTOFF+(6 if kind=="expired" else 1)*HOUR,structure_valid=kind!="structure",data_quality="PARTIAL" if kind=="quality" else "VALID")
    assert result.state=={"expired":"EXPIRED","stop":"INVALIDATED","structure":"INVALIDATED","quality":"WAIT"}[kind]

@pytest.mark.parametrize("rows,asof",[
    ([bar(0)],CUTOFF+HOUR-1),
    ([dict(bar(0),confirm="0")],CUTOFF+HOUR),
    ([bar(0),bar(0)],CUTOFF+2*HOUR),
    ([bar(0),bar(2)],CUTOFF+3*HOUR),
    ([dict(bar(0),close=float("nan"))],CUTOFF+HOUR),
])
def test_bad_or_future_bars(rows,asof):
    with pytest.raises(ValueError): evaluate_signal(PLAN,timing(CUTOFF,{}),rows,asof)

def test_partial_cutoff_bar_excluded():
    clock=timing(CUTOFF+1000,{})
    with pytest.raises(ValueError): evaluate_signal(PLAN,clock,[bar(0)],CUTOFF+HOUR)
    assert clock.hypothesis_expires_ms==CUTOFF+7*HOUR

def historical():
    return {"historical":True,"snapshot_id":"past","status":"VALID","allow_new_risk":True,"cutoff_ms":CUTOFF,
       "timeframes":{k:{"cutoff_ms":CUTOFF,"latest_close_ms":CUTOFF,"status":"VALID"} for k in ("1h","4h","1d")}}

def test_historical_explicit_assumptions_without_book(monkeypatch):
    monkeypatch.setattr(perpetual_plan.okx,"check_bundle",lambda b,*a:b)
    monkeypatch.setattr(perpetual_plan,"candidates",lambda b,c:{"long":PLAN})
    result=prepare_historical_signal(historical(),{},"long",{"taker_fee_bps":5,"slippage_bps_per_fill":3})
    assert result["simulated_fills_not_actual_orders"] and not result["plan_valid"]
    assert result["assumptions"]["fill_rule"]=="NEXT_BAR_OPEN_ADVERSE_SLIPPAGE"

@pytest.mark.parametrize("required",["requires_orderbook","requires_oi"])
def test_missing_strategy_dependency(required):
    with pytest.raises(ValueError,match="Required historical"):
        prepare_historical_signal(historical(),{},"long",{"taker_fee_bps":5,"slippage_bps_per_fill":3,required:True})

@pytest.mark.parametrize("mutation",["today","future","current","quality"])
def test_historical_protection(mutation):
    b=historical()
    if mutation=="today": b["historical"]=False
    if mutation=="future": b["timeframes"]["1h"]["latest_close_ms"]+=HOUR
    if mutation=="current": b["derivatives"]={"components":{"oi_current":{"data":{"contracts":1}}}}
    if mutation=="quality": b["status"]="PARTIAL"
    with pytest.raises(ValueError): prepare_historical_signal(b,{},"long",{"taker_fee_bps":5,"slippage_bps_per_fill":3})


def test_stale_replay_cannot_remain_eligible():
    result=evaluate_signal(PLAN,timing(CUTOFF,{}),[bar(0),bar(1,low=99)],CUTOFF+4*HOUR)
    assert result.state=="WAIT" and result.next_tradable_ms is None


def test_timing_schema_export(monkeypatch):
    from tests.test_perpetual_plan import choice, CONFIG, SYMBOL
    from tradingagents.dataflows import perpetual_plan as p
    from tests.test_perpetual_plan import PlanClient, NOW, DAY, d
    monkeypatch.setattr(p.okx,"utc_ms",lambda:NOW)
    monkeypatch.setattr(d,"utc_ms",lambda:NOW)
    bundle=p.okx.build_bundle(SYMBOL,DAY,CONFIG,PlanClient())
    result,_=p.finalize_result({"company_of_interest":SYMBOL,"portfolio_choice":choice()},bundle,CONFIG)
    assert result.signal_timing["hypothesis_expires_ms"] > bundle["cutoff_ms"]+900000
    assert result.signal_state=="PENDING_BREAKOUT"


@pytest.mark.parametrize("fault",[None,"expired_book","pre_signal","ttl","bad_core","changed"])
def test_live_revalidation_gate(monkeypatch,fault):
    now=CUTOFF+2*HOUR+1000
    signal=evaluate_signal(PLAN,timing(CUTOFF,{}),[bar(0),bar(1,low=99)],now)
    original={"cutoff_ms":CUTOFF,"symbol":"BTC-USDT-SWAP"}
    current={"cutoff_ms":now,"fetched_at_ms":now,"symbol":"BTC-USDT-SWAP"}
    validation={"checked_at_ms":now,"expires_at_ms":now+300000}
    if fault=="expired_book": validation["expires_at_ms"]=now
    if fault=="pre_signal": validation["checked_at_ms"]=CUTOFF+HOUR
    if fault=="ttl": validation["checked_at_ms"]=now-60000
    monkeypatch.setattr(perpetual_plan,"compare_revalidation",lambda *a:["changed"] if fault=="changed" else [])
    monkeypatch.setattr(perpetual_plan.okx,"check_bundle",lambda *a:{"allow_new_risk":fault!="bad_core"})
    assert entry_evidence_ready(original,signal,validation,current,{},now)==(fault is None)
