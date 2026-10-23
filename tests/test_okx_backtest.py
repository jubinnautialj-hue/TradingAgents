from decimal import Decimal
import copy
import json
import pytest
from tradingagents.okx_backtest import SimulationConfig, simulate, save_run, feedback, HOUR, SYMBOL
from tests.test_okx_research import INFO

START=HOUR*24000
CONFIG=SimulationConfig(taker_fee_bps=5,slippage_bps=2,contract_parameter_assumption="CURRENT_SPEC_ASSUMED_CONSTANT_SHORT_WINDOW")


def bars(n=8):
    return [{"ts":START+i*HOUR,"open":100.,"high":103.,"low":97.,"close":101.,"confirm":"1"} for i in range(n+1)]


def funding():
    return [{"ts":START+i*8*HOUR,"realized_rate":0.0001} for i in range(4)]


def decision(side="LONG"):
    return {"cutoff_ms":START,"snapshot_id":"fixed","evidence_latest_ms":START,"candidate":{
        "direction":side,"entry":{"price":100.},"stop":{"price":90. if side=="LONG" else 110.},"target":{"price":110. if side=="LONG" else 90.}}}


def run(rows=None,ds=None,config=CONFIG):
    rows=rows or bars()
    return simulate(rows,funding(),[decision()] if ds is None else ds,INFO,config,START,rows[-1]["ts"])


@pytest.mark.parametrize("side,win",[("LONG",True),("LONG",False),("SHORT",True),("SHORT",False)])
def test_directional_fills(side,win):
    rows=bars()
    if side=="SHORT":
        for b in rows:b.update(open=99.,close=98.,high=99.,low=97.)
    # first bar confirms breakout, second retests. Enter at third OPEN only.
    if side=="SHORT": rows[1]["high"]=101.
    if side=="LONG": rows[1]["low"]=99.
    rows[2].update(high=112. if (side=="LONG")==win else 105.,low=95. if (side=="LONG")==win else 88.)
    r=run(rows,[decision(side)])
    assert len(r["trades"])==1
    t=r["trades"][0]
    assert t["entry_ms"]==START+2*HOUR
    assert (t["net_pnl"]>0)==win
    assert r["metrics"]["net_pnl"]==pytest.approx(t["net_pnl"])
    assert t["fees_usdt"]>0 and t["slippage_usdt"]>0


def test_wait_and_no_breakout_are_distinct():
    d=decision(); d["candidate"]=None
    assert run(ds=[d])["events"][0]["type"]=="WAIT"
    rows=bars()
    for b in rows:b["close"]=99.
    r=run(rows)
    assert not r["trades"] and any(e["type"]=="EXPIRED" for e in r["events"])
    assert r["status"]=="NO_TRADES" and r["metrics"]["win_rate"] is None


def test_ambiguous_stop_first():
    rows=bars(); rows[2].update(high=112.,low=88.)
    r=run(rows)
    assert r["trades"][0]["exit_reason"]=="STOP_FIRST_AMBIGUOUS"
    assert r["trades"][0]["net_pnl"]<0


def test_funding_and_equity():
    rows=bars(16)
    r=run(rows)
    t=r["trades"][0]
    assert t["funding_cashflow"]==-t["base_quantity"]*rows[8]["open"]*0.0001
    assert r["metrics"]["final_equity"]==pytest.approx(CONFIG.initial_equity+t["net_pnl"])
    assert r["equity_curve"][0]["equity"]==CONFIG.initial_equity
    assert r["metrics"]["max_drawdown"]>0
    assert not feedback(r,t["resolved_at_ms"]-1)
    assert len(feedback(r,t["resolved_at_ms"]))==1


@pytest.mark.parametrize("bad",["funding","gap","future","confirm","nan"])
def test_invalid_data(bad):
    rows=bars(); fs=funding(); d=decision()
    if bad=="funding": fs.pop(1)
    if bad=="gap": rows.pop(1)
    if bad=="future": d["evidence_latest_ms"]+=HOUR
    if bad=="confirm": rows[0]["confirm"]="0"
    if bad=="nan": rows[0]["open"]=float("nan")
    with pytest.raises(ValueError): simulate(rows,fs,[d],INFO,CONFIG,START,START+8*HOUR)


def test_save_idempotent(tmp_path):
    r=run(); inputs={"fixed":1}
    a=save_run(tmp_path,inputs,r); b=save_run(tmp_path,inputs,r)
    assert a==b and len(list(tmp_path.glob("*/result.json")))==1
    assert json.loads((a/"result.json").read_text())["metrics"]==r["metrics"]


def test_quantity_and_cost_math():
    t=run()["trades"][0]
    assert Decimal(str(t["contracts"]))%Decimal(INFO["lotSz"])==0
    assert t["base_quantity"]==pytest.approx(t["contracts"]*float(INFO["ctVal"])*float(INFO.get("ctMult") or 1))
    assert t["fees_usdt"]==pytest.approx(t["base_quantity"]*(t["entry_price"]+t["exit_price"])*5/10000)
    assert t["net_pnl"]==pytest.approx(t["sign"]*t["base_quantity"]*(t["exit_price"]-t["entry_price"])-t["fees_usdt"]+t["funding_cashflow"])


def test_short_replay_window_and_invalidation():
    rows=bars(); rows[0]["close"]=89.; rows[0]["low"]=88.
    assert any(e["type"]=="INVALIDATED" for e in run(rows)["events"])


def test_no_leverage():
    c=CONFIG.model_copy(update={"notional_usdt":20000})
    with pytest.raises(ValueError,match="No leverage"):run(config=c)


def test_confirmation_expiry_blocks_entry():
    c=CONFIG.model_copy(update={"confirmation_bars":2})
    r=run(config=c)
    assert not r["trades"] and any(e["type"]=="EXPIRED" for e in r["events"])


def test_unfilled_window_separate_from_expiry():
    d=decision(); d["cutoff_ms"]=START+7*HOUR; d["candidate"]["entry"]["price"]=105
    r=run(ds=[d])
    assert r["events"][-1]["type"]=="UNFILLED_WINDOW_END"


def test_adverse_tick_rounding():
    t=run()["trades"][0]
    assert Decimal(str(t["entry_price"]))%Decimal(INFO["tickSz"])==0
    assert Decimal(str(t["exit_price"]))%Decimal(INFO["tickSz"])==0
    assert t["entry_price"]>=t["entry_reference"]
    assert t["exit_price"]<=t["exit_reference"]


def test_non_deterministic_rewrite_rejected(tmp_path):
    r=run();save_run(tmp_path,{"fixed":1},r)
    other=copy.deepcopy(r); other["metrics"]["net_pnl"]+=1
    with pytest.raises(ValueError,match="Non-deterministic"):save_run(tmp_path,{"fixed":1},other)


def test_future_prices_do_not_change_historical_levels():
    from tests.test_okx_multitimeframe import MultiClient, NOW
    from tradingagents.okx_backtest import historical_bundle
    from tradingagents.dataflows.vendors import okx
    cutoff=NOW//(4*HOUR)*(4*HOUR)
    client=MultiClient(pending=False)
    data={"instrument":INFO,"server_ms":NOW+HOUR,"candles":{
        p:client.candles(SYMBOL,p,cutoff+8*HOUR,500) for p in ("1h","4h","1d")}}
    cfg={"okx_multi_timeframe":True,"okx_interval":"1h"}
    before=historical_bundle(data,cutoff,cfg)
    for period,rows in data["candles"].items():
        step=okx.BARS[period][1]
        for row in rows:
            if int(row[0])+step>cutoff:row[1:5]=["99999","99999","99999","99999"]
    after=historical_bundle(data,cutoff,cfg)
    for p in ("1h","4h","1d"):
        assert before["timeframe_analysis"][p]["indicators"]==after["timeframe_analysis"][p]["indicators"]
        assert before["timeframe_analysis"][p]["previous_range"]==after["timeframe_analysis"][p]["previous_range"]


def test_reported_drawdown_matches_curve():
    r=run(); peak=CONFIG.initial_equity; expected=0
    for point in r["equity_curve"]:
        peak=max(peak,point["equity"])
        expected=max(expected,(peak-point["equity"])/peak)
    assert r["metrics"]["max_drawdown"]==expected


def test_empty_csv_has_schema(tmp_path):
    d=decision(); d["candidate"]=None
    root=save_run(tmp_path,{"wait":1},run(ds=[d]))
    assert "entry_utc" in (root/"trades.csv").read_text(encoding="utf-8-sig")


def test_full_cached_pipeline_is_reproducible():
    from tests.test_okx_multitimeframe import MultiClient, NOW
    from tradingagents.okx_backtest import run_data
    start=NOW//(8*HOUR)*(8*HOUR); end=start+8*HOUR
    client=MultiClient(pending=False)
    data={"symbol":SYMBOL,"instrument":INFO,"server_ms":end+2*HOUR,"candles":{
        p:sorted(client.candles(SYMBOL,p,end+HOUR,500),key=lambda r:int(r[0])) for p in ("1h","4h","1d")},
        "funding":[{"ts":start+i*8*HOUR,"realized_rate":0.0001} for i in range(2)],"coverage":{}}
    a=run_data(data,start,end,CONFIG); b=run_data(data,start,end,CONFIG)
    assert a==b and a["status"]=="NO_TRADES"


def test_cached_source_time_and_identity_blocked():
    from tradingagents.okx_backtest import run_data
    with pytest.raises(ValueError,match="source identity"):
        run_data({"symbol":SYMBOL,"server_ms":START},START,START+8*HOUR,CONFIG)


def test_execution_permission_cannot_be_enabled():
    with pytest.raises(ValueError):SimulationConfig(**{**CONFIG.model_dump(),"execution_enabled":True})
