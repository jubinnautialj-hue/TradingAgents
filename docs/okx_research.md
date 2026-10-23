# OKX USDT perpetual research (phase one)

This mode uses unauthenticated OKX V5 public GET requests. It has no account,
signing, order, cancellation, leverage, transfer or MCP execution client. It
does not read OKX credentials. `execution_enabled` is always false, including
when a research dataset is valid. `allow_new_risk` is a research-quality permit,
not permission to submit orders. No order/execution module is included.

## Windows / Python 3.13

From `D:\_01trading_agents`, validate live public data without any LLM calls:

```powershell
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 1h --data-only
```

For the full existing research/debate/risk/portfolio workflow (uses your existing
configured LLM provider, not OKX credentials):

```powershell
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h
```

`--interval 1d` requests OKX `1Dutc`, not UTC+8 `1D`. `--date YYYY-MM-DD`
pins a historical UTC day; absent `--date`, use UTC today. Historical studies
end at UTC midnight after that date; current studies end at server time.
Only confirmed candles whose entire interval ends before the cutoff are used.
Full research reuses remembered CLI model/provider/language choices without
writing them; explicit environment settings take precedence. Reports have
unique names under the existing results directory. Data-only mode
does not change preferences or contact an LLM. Exit code 2 means blocked data;
exit code 1 means a configuration/runtime error. A model failure returns exit
code 3, LLM_UNAVAILABLE and a fault report, separately from market-data quality.
The normal stock CLI is unchanged.

Python API:

```python
from copy import deepcopy
from datetime import UTC, datetime
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

config = deepcopy(DEFAULT_CONFIG)
config.update(market_mode="okx_usdt_perpetual", okx_interval="1h")
graph = TradingAgentsGraph(selected_analysts=["market"], config=config)
state, rating = graph.propagate(
    "BTC-USDT-SWAP", datetime.now(UTC).strftime("%Y-%m-%d"), asset_type="crypto"
)
report = graph.save_reports(state, "BTC-USDT-SWAP")
```

Explicit mode can also be configured using `TRADINGAGENTS_MARKET_MODE` and
`TRADINGAGENTS_OKX_INTERVAL`. Only the market analyst is supported in phase one;
unmapped Yahoo news/social and stock fundamentals are rejected in OKX mode.
The existing multi-stage research and risk debate remains. Yahoo-based memory
outcome settlement is disabled in this mode; OKX decisions are logged separately
and never fed into the original stock-return evaluation.

## Data and quality

`OKXPublicClient` hardcodes `https://www.okx.com` and allows only:

- `/api/v5/public/time`
- `/api/v5/public/instruments`
- `/api/v5/market/history-candles`

There is no user-supplied host or authenticated session. Ambient netrc auth and
HTTP redirects are disabled. Requests are paced at least 250ms apart within a
process, with three attempts for transient timeouts, connections, HTTP 429/5xx,
and selected transient OKX codes. Backoff has jitter; numeric Retry-After is
honored up to 30 seconds. After failure the preflight stops the run; model tools
do not refetch. This is not a cross-process rate limiter: do not run many copies
concurrently against one IP. There is no disk cache in phase one; the immutable
run bundle is the cache and is shared across every indicator and verification
tool. A new run fetches a new bundle.

The contract must be live, linear, USDT settled and have the matching instrument
family and base currency; ctVal, minSz, lotSz and tickSz must be positive and
finite. SWAP quoteCcy is commonly empty: quote identity is verified via actual
instFamily and settleCcy. No conversion to a spot/Yahoo ticker is permitted.
Instrument settings are current exchange metadata, explicitly labelled as such;
historical tick/lot sizes and listing-state changes are not reconstructed.

All nine candle fields are checked. `Volume` is volCcy (base currency),
`VolumeContracts` is vol (contracts), and `VolumeQuote` is volCcyQuote (USDT).
Base volume is checked against contracts * ctVal * ctMult. No price or volume
is gap-filled. Precision is retained in data and rendered with meaningful
digits, not rounded to two decimal places.

400 bars are fetched by default (configurable up to 5000); at least 250 confirmed
continuous bars are required, including warmup for the 200-period SMA. Latest
values of the supported indicators must be finite. Duplicate bars, invalid
timestamps/OHLC/numbers/units and missing intervals block research. API reverse
chronology is normal; shuffled responses are sorted and annotated. Unconfirmed
and cutoff-exceeding bars are excluded and annotated. Expected latest closed
bar must be present by default (`okx_max_lag_bars=0`). Clock skew over 60 seconds
blocks research. Each bundle has provenance, cutoff, units, timestamps, quality
issues and an integrity hash; TTL is 900 seconds by default.

Indicators are computed from that bundle with stockstats. Their periods are
bars, not days. Indicator output omits early warmup rows and marks insufficient
lookback coverage. A tool requesting another as-of date is refused, rather than
being given the current snapshot. A limited window is not a complete archive.

`preflight` precedes all analysts and memory nodes. Invalid data returns REVIEW
with an explicit RESEARCH_INVALID report; no LLM is invoked. `final_gate` reruns
validation after the portfolio manager, before logging, and at report output.
It replaces even a model Buy with REVIEW and `allow_new_risk=false` when invalid.
States include VALID, PARTIAL, DATA_UNAVAILABLE, STALE_DATA and RESEARCH_INVALID;
failure is never presented as a neutral Hold or a market direction. No existing
position is changed. Checkpoint reuse cannot bypass final freshness checks.

## MCP assessment (2026-10-22)

Codex exposes okx-trade-mcp 1.4.9 with market/account/spot/swap enabled.
Capabilities reported hasAuth=true, demo=false, readOnly=false. This describes
the server, not the actual key's permissions. No account request was needed or
performed; key permissions remain unverified. Do not give that connection to
research agents. Use a separate read-only key only if account research becomes
necessary; verify its permissions before account access.

Codex can invoke the connected MCP tools, but TradingAgents has no such MCP
runtime transport/session. Reusing it would couple the Python service to a
desktop session and expose unnecessary writes unless separately isolated.
Therefore this implementation uses public REST, without installing an MCP tool,
copying keys, or changing global MCP settings.

Market tools expose instruments, ticker, candles with historical pagination,
funding (current/history), OI (current/history), order book, mark/index data,
indicators and screening. Account tools include balances, positions, bills,
configuration and fees, but also transfer and set-position-mode writes. Spot
and swap modules expose order/fill/position reads plus placement/amend/cancel,
leverage and swap close-position writes. Tool inventory is recorded alongside
this document. Only system_get_capabilities was invoked for the assessment.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_okx_research.py tests/test_okx_graph.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_okx_public_integration.py -m integration -q -s
.\.venv\Scripts\python.exe -m pytest -q
```

Normal tests block network via the existing conftest. Only explicitly selected
integration tests access the three public paths. No test calls an account/write
endpoint. Public integration tests validate 400 real bars for each timeframe.
They also validate an explicitly historical 1h cutoff on 2026-10-01.

## Limits and next steps

Funding, OI, mark/index series, order-book depth, liquidity/slippage, long/short
statistics, liquidation data, WebSockets, scanning and opportunity discovery are
not implemented in this adapter. Add public endpoint-specific schemas next,
with their documented retention and units. Persist historical observations with
both event/available-at time; never substitute current OI/depth into past dates.
Funding needs settled vs estimated classification. Historical liquidations must
only be claimed if an official endpoint actually provides that coverage.

There is no futures cost/PnL simulator. A future paper executor must be a separate
in-memory/local ledger with fees, funding and slippage and no production client.
Official demo support requires separate demo credentials/environment, tests
asserting that separation, and an explicit human approval design. A research Buy
is never an order authorization.

Rollback: switch market_mode back to stock or run the original CLI. No provider
was removed, no report replaced, no credential changed. Revert the feature's
reviewed changes if needed; do not delete existing history_report or environment.
