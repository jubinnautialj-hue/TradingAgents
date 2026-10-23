import base64
import copy
import csv
import hashlib
import io
import zipfile
from datetime import datetime, UTC
import pytest
from tradingagents import okx_continuous as c, okx_backtest as bt
from tests.test_okx_backtest import START, CONFIG, INFO, bars, funding, decision
from tests.test_okx_diagnostics import evidence


def archive(rows,month="2025-10"):
    name="BTC-USDT-SWAP-fundingrates-"+month
    out=io.BytesIO()
    text="instrument_name,funding_rate,funding_time\n"+"".join(f"BTC-USDT-SWAP,{r['realized_rate']},{r['ts']}\n" for r in rows)
    with zipfile.ZipFile(out,"w") as z:z.writestr(name+".csv",text)
    blob=out.getvalue()
    return {"url":"https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/"+month.replace("-","")+"/"+name+".zip", "zip_sha256":hashlib.sha256(blob).hexdigest(),"zip_base64":base64.b64encode(blob).decode()}


def test_frozen_strategy_hash():
    assert len(c.strategy_fingerprint())==64
    assert c.fixed_config().taker_fee_bps==5 and c.fixed_config().slippage_bps==2


def test_official_archive_schema_and_timezone():
    start=int(datetime.fromisoformat("2025-10-01T00:00:00+08:00").timestamp()*1000)
    file=archive([{"ts":start,"realized_rate":0.0001}])
    assert c.parse_archive(base64.b64decode(file["zip_base64"]),file["url"])[0]["ts"]==start
    wrong=archive([{"ts":start-bt.HOUR,"realized_rate":0.0001}])
    with pytest.raises(ValueError,match="partition"):c.parse_archive(base64.b64decode(wrong["zip_base64"]),wrong["url"])


def test_archive_actual_rate_overlap_and_corruption():
    start=int(datetime(2025,10,1,tzinfo=UTC).timestamp()*1000)
    fs,rest=evidence([start,start+8*bt.HOUR,start+16*bt.HOUR])
    normalized=[{"ts":r["ts"],"realized_rate":r["realized_rate"]} for r in fs]
    file=archive(normalized)
    e={"type":"OFFICIAL_MONTHLY_ARCHIVE_PLUS_REST","files":[file],"rest":rest}
    assert c.verify_archive_evidence(normalized,e,start,start+16*bt.HOUR)["matched_overlap"]==3
    bad=copy.deepcopy(e);bad["files"][0]["zip_sha256"]="broken"
    with pytest.raises(ValueError,match="hash"):c.verify_archive_evidence(normalized,bad,start,start+16*bt.HOUR)
    wrong=[{**r,"realized_rate":0.01} for r in normalized]
    bad={**e,"files":[archive(wrong)]}
    with pytest.raises(ValueError,match="disagreement"):c.verify_archive_evidence(normalized,bad,start,start+16*bt.HOUR)


@pytest.mark.parametrize("width",[1,2,3,7,8,13,24])
def test_cross_batch_position_funding_equity_identical(width):
    rows=bars(24);fs=funding();ds=[decision()]
    # Position opens at hour 2, crosses the 8h funding boundary, exits at hour 14.
    original=bt.simulate(rows,fs,ds,INFO,CONFIG,START,START+24*bt.HOUR)
    chunks=[rows[i:i+width] for i in range(0,len(rows),width)]
    continuous=c.continuous_simulation(chunks,fs,ds,INFO,CONFIG,START,START+24*bt.HOUR)
    assert continuous==original
    assert len(continuous["trades"])==1 and len([e for e in continuous["events"] if e["type"]=="FUNDING"])==1


@pytest.mark.parametrize("kind",["overlap","gap","reverse"])
def test_chunk_boundary_errors_blocked(kind):
    rs=bars();chunks=[rs[:4],rs[4:]]
    if kind=="overlap":chunks[1]=[rs[3]]+chunks[1]
    if kind=="gap":chunks[1]=chunks[1][1:]
    if kind=="reverse":chunks.reverse()
    with pytest.raises(ValueError):c.merge_chunks(chunks)


def test_continuous_crosses_14day_limit_without_reset():
    rs=bars(15*24)
    fs=[{"ts":START+i*8*bt.HOUR,"realized_rate":0.0001} for i in range(46)]
    ds=[decision()]
    with pytest.raises(ValueError):bt.simulate(rs,fs,ds,INFO,CONFIG,START,rs[-1]["ts"])
    result=c.continuous_simulation([rs[:14*24],rs[14*24:]],fs,ds,INFO,CONFIG,START,rs[-1]["ts"])
    assert len(result["trades"])==1 and result["metrics"]["initial_equity"]==CONFIG.initial_equity


def test_historical_mark_valuation_no_trade_price_fallback():
    rs=bars(16);marks={r["ts"]:110. for r in rs}
    result=c.continuous_simulation([rs[:8],rs[8:]],funding(),[decision()],INFO,CONFIG,START,rs[-1]["ts"],mark_prices=marks)
    t=result["trades"][0]
    assert t["funding_cashflow"]==pytest.approx(-t["base_quantity"]*110*0.0001)
    assert next(e for e in result["events"] if e["type"]=="FUNDING")["price_source"]=="HISTORICAL_MARK_OPEN_PROXY"
    del marks[START+8*bt.HOUR]
    with pytest.raises(ValueError,match="fallback forbidden"):c.continuous_simulation([rs],funding(),[decision()],INFO,CONFIG,START,rs[-1]["ts"],mark_prices=marks)


def test_no_historical_observation_called_unseen_holdout():
    p=c.validation_protocol(START,START+bt.HOUR)
    assert p["all_current_inspection_is_development"]
    assert p["future_holdout"]["status"]=="NOT_RUN_NOT_YET_AVAILABLE"


@pytest.mark.parametrize("url",["http://static.okx.com/cdn/okex/traderecords/swaprates/monthly/x.zip","https://evil.test/x.zip","https://static.okx.com/private/x.zip"])
def test_zip_url_allowlist(url):
    with pytest.raises(ValueError):c.validate_archive_url(url)


@pytest.mark.parametrize("offset",[328,335])
def test_position_or_pending_signal_crosses_real_fourteen_day_boundary(offset):
    rs=bars(15*24);fs=[{"ts":START+i*8*bt.HOUR,"realized_rate":0.0001} for i in range(46)]
    d=decision();d["cutoff_ms"]=START+offset*bt.HOUR;d["evidence_latest_ms"]=d["cutoff_ms"]
    boundary=START+14*24*bt.HOUR
    result=c.continuous_simulation([rs[:336],rs[336:]],fs,[d],INFO,CONFIG,START,rs[-1]["ts"])
    assert len(result["trades"])==1
    t=result["trades"][0]
    if offset==328:
        assert t["entry_ms"]<boundary<t["exit_ms"]
        assert len([e for e in result["events"] if e["type"]=="FUNDING" and e["ts"]==boundary])==1
    else:assert d["cutoff_ms"]<boundary<t["entry_ms"]
    assert len([e for e in result["events"] if e["type"]=="ENTRY"])==1
    assert t["exit_reason"]=="MAX_HOLDING"
    assert result["metrics"]["final_equity"]==pytest.approx(CONFIG.initial_equity+t["net_pnl"])


def test_future_holdout_protected_before_network(monkeypatch,tmp_path):
    def network(*args):raise AssertionError("No future holdout query permitted")
    monkeypatch.setattr(c.diag,"CachedPublicClient",network)
    with pytest.raises(ValueError,match="Future validation"):
        c.fetch_history(c.DEVELOPMENT_END,c.DEVELOPMENT_END+48*bt.HOUR,tmp_path)


def test_cached_official_zip_requires_no_network(tmp_path,monkeypatch):
    ts=int(datetime(2025,10,1,tzinfo=UTC).timestamp()*1000)
    file=archive([{"ts":ts,"realized_rate":0.0001}])
    folder=tmp_path/"funding_zips";folder.mkdir()
    path=folder/(bt.digest(file["url"])+".zip")
    path.write_bytes(base64.b64decode(file["zip_base64"]))
    path.with_suffix(".json").write_text(__import__('json').dumps({"sha256":file["zip_sha256"],"downloaded_at_utc":"2026-10-23T00:00:00Z"}))
    def no_network(*args):raise AssertionError("Cached ZIP must not refetch")
    monkeypatch.setattr(c.requests,"Session",no_network)
    assert c.download_archive(file["url"],tmp_path)["zip_sha256"]==file["zip_sha256"]
    path.write_bytes(b"tamper")
    with pytest.raises(ValueError,match="corruption"):c.download_archive(file["url"],tmp_path)


def test_cropped_processing_history_produces_identical_candidates():
    from tests.test_perpetual_plan import PlanClient, NOW
    start=NOW//(4*bt.HOUR)*(4*bt.HOUR);end=start+24*bt.HOUR
    client=PlanClient()
    data={"symbol":bt.SYMBOL,"instrument":INFO,"server_ms":end+bt.HOUR,"candles":{
        p:sorted([r for r in client.candles(bt.SYMBOL,p,end+bt.HOUR,500) if r[8]=="1"],key=lambda r:int(r[0])) for p in mtf_periods()}}
    full=bt.regenerate_decisions(data,start,end,c.fixed_config(),diagnostics=True)
    cropped=bt.regenerate_decisions(c.decision_history(data,start,end),start,end,c.fixed_config(),diagnostics=True)
    assert full==cropped
    assert any(d["candidate"] for d in full)


def mtf_periods():return ("1h","4h","1d")


def test_full_continuous_pipeline_is_idempotent_across_processing_days(tmp_path):
    from tests.test_okx_multitimeframe import MultiClient
    start=int(datetime(2025,10,1,tzinfo=UTC).timestamp()*1000);end=start+48*bt.HOUR
    client=MultiClient(pending=False)
    data={"symbol":bt.SYMBOL,"server_ms":end+2*bt.HOUR,"instrument":INFO,"coverage":{},"availability":{"original_publication_timestamps":"UNKNOWN"},
        "candles":{p:sorted(client.candles(bt.SYMBOL,p,end+bt.HOUR,500),key=lambda r:int(r[0])) for p in mtf_periods()}}
    fs,rest=evidence(list(range(start,end+bt.HOUR,8*bt.HOUR)))
    data["funding"]=[{"ts":r["ts"],"realized_rate":r["realized_rate"]} for r in fs]
    data["funding_evidence"]={"type":"OFFICIAL_MONTHLY_ARCHIVE_PLUS_REST","files":[archive(data["funding"])],"rest":rest}
    data["mark_records"]=[{"ts":int(r[0]),"close_ms":int(r[0])+bt.HOUR,**dict(zip(("open","high","low","close"),map(float,r[1:5])))} for r in data["candles"]["1h"] if start<=int(r[0])<=end]
    a,one=c.run_continuous(data,start,end,tmp_path,processing_days=1,progress=lambda s:None)
    b,two=c.run_continuous(data,start,end,tmp_path,processing_days=14,progress=lambda s:None)
    assert a==b and one==two
    assert len(list(tmp_path.glob("*/result.json")))==1
    assert not one["execution_enabled"] and not one["allow_new_risk"]
