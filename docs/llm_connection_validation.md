# ChatGPT stream transport diagnosis (2026-10-22)

## Evidence and limits

The original full traceback was initially unavailable, and was subsequently
provided as a pasted-text attachment. Diagnosis now includes that full traceback,
installed source, sanitized runtime inspection, mocked transport errors and
actual model/network tests. No claim is made about which remote component closed
that original connection, nor that one success proves long-term reliability.

### Follow-up review of the supplied full traceback

The attachment contains 600 lines, starting with the successful 1h data-only
command and then the failed 4h full research command. The response-body path is
httpcore2 HTTP/1.1 receive_response_body / h11 read_eof, followed by httpx2
iter_raw/iter_bytes, OpenAI SSE decoding, and LangChain _stream_responses.
The concrete protocol error is incomplete chunked read: the peer closed an
HTTP message before its body was complete. This establishes a response-stream
transport failure, not a failure fetching OKX data or establishing the initial
connection. It does not prove that any text/tool delta had already been delivered.

The final APIConnectionError/OpenAIConnectionError are wrappers of that protocol
failure. No authentication rejection or invalid-model response is shown. The
trace lacks response status/headers, timing, server/proxy logs and request progress
needed to identify the closing component. Its application frames match the
pre-fix path and do not contain the added ChatGPTPlanChatOpenAI._stream boundary.
This evidence supports the existing no-replay rule for interrupted response
bodies. No additional model calls, proxy changes or dependency updates were
needed for this follow-up review.

The effective CLI configuration selects the `chatgpt` provider and
`gpt-6.1-sol` for both tiers, at the pinned api.openai.com endpoint. Credential
handling is a callable using the existing subscription sign-in. No credential
file contents, token, key or account identity were printed or copied. The
application's existing authentication code reads/refreshes its own credential
only as part of normal model requests.

Installed versions:

| Package | Version |
|---|---|
| Python | 3.13.13 |
| openai | 3.36.1 |
| langchain-openai | 1.7.2 |
| langchain-core | 1.6.13 |
| httpx / httpcore | 0.28.1 / 1.0.9 |
| httpx2 / httpcore2 | 2.16.0 / 2.16.0 |
| h11 | 0.16.0 |

OpenAI 3.36.1 requires httpx2>=2.12,<3; LangChain selects httpx2 for OpenAI>=3;
httpx2 requires matching httpcore2 2.16.0. Finding httpx2 in the traceback is
normal here, not evidence of an incompatible mixture. No dependency was upgraded.

All uppercase/lowercase HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY and
OPENAI_PROXY variables were absent in the inspected process. However, Windows
WinINET proxy is enabled, urllib.getproxies() resolves HTTP/HTTPS to the local
Clash listener 127.0.0.1:7897, and Mihomo is listening there. WinHTTP reporting
direct access does not describe this Python route. LangChain detects the system
proxy and preserves proxy auto-detection. No global proxy or Clash setting changed.

An unauthenticated TLS GET to OpenAI through the system proxy returned the
expected HTTP 401 in 0.67s (no credentials sent). An isolated direct-route GET
timed out connecting after about 10s. TLS verification remained enabled. This
supports retaining the working proxy route; it does not prove Clash caused the
original truncated response. The discovered recent Clash application log was
empty; it supplied no matching transport evidence.

## Confirmed program gaps

1. ChatGPTPlanChatOpenAI forces streaming; the user call therefore reads SSE
   even though it invokes a normal LangChain invoke(). The installed SDK retries
   request setup, not an already opened response stream.
2. request_timeout was None. The installed LangChain constructs its HTTP client
   with timeout=None; the SDK inherits it. Both actual root timeout and underlying
   HTTP timeout were unlimited, despite the SDK's nominal 600-second default.
3. No application boundary classified/recorded failed model streams; a model
   exception aborted OKX research without a fault report.
4. invoke_structured previously caught every failure and made another free-text
   call, including ambiguous or partially received model requests.
5. LangChain can finalize a stream after a clean EOF without an explicit completed
   response; incomplete output needed an additional completion check.

RemoteProtocolError/incomplete chunked read identifies an unfinished HTTP
response body. It does not by itself establish invalid credentials, a wrong
model, a dependency defect, or whether the service/proxy/network closed it.

## Minimal correction

- ChatGPT subscription synchronous calls now default to a supported 120-second
  HTTP timeout. Streaming and subscription authentication remain enabled.
- By default SDK request replay is disabled for this client. A maximum of two
  retries (three attempts) is allowed only for classified ConnectError,
  ConnectTimeout or PoolTimeout before the first response chunk, with jittered
  backoff. TLS failures and auth failures are not retried. If the user explicitly
  configures SDK max_retries, no additional outer budget is added.
- Read/write ambiguity, interrupted streams and partial responses are not
  automatically replayed. A missing response.completed or an incomplete status
  is LLM_UNAVAILABLE, and invoke cannot return partial text/tool arguments.
- Structured-output network/service/auth failures propagate without a second
  free-text call. Parsing/unsupported-output fallbacks remain available.
- OKX graph nodes preserve the last completed public research state on model
  failure. Data quality, model status and workflow status are separate. A model
  failure forces REVIEW, allow_new_risk=false and execution_enabled=false,
  and creates an explicit fault report. Report revalidation cannot reset that
  model-failure block. CLI uses exit code 3 for model failure, 2 for invalid data.

No OKX public adapter, credentials, SSL settings, endpoint or model was changed.
No account or execution interface is used. The stream recovery change covers the
synchronous path used by this CLI; async stream recovery is not added.

## Validation

- 18 transport/fault-report tests passed, including partial stream interruption,
  read timeout, pre-connect retry limits, TLS/auth classification, silent EOF,
  no structured free-text replay, real LangGraph blocking and CLI fault reports.
- 180 related stock/OKX/CLI/report/structured-output tests passed (including
  the 18 transport tests).
- 66 related client configuration/checkpoint tests passed.
- One real minimal model call used the same configured subscription/model,
  requested exactly OK with no tools and low reasoning effort, and completed in
  5.71 seconds with response status completed and the expected reply.
- The original BTC-USDT-SWAP 4h full research command then completed successfully
  using the saved gpt-6.1-sol subscription configuration and its existing default
  reasoning setting. The full run took approximately nine minutes. The default
  reasoning setting was not changed to make the full run pass.

Saved full-run state:

| Component | Actual saved result |
|---|---|
| Instrument / interval | BTC-USDT-SWAP / 4h |
| Market data quality | VALID |
| Model | AVAILABLE |
| Workflow | COMPLETED |
| allow_new_risk | true (research quality only) |
| execution_enabled | false |
| Sections | All nine expected market/research/trader/risk/portfolio sections nonempty |
| Markdown / HTML | Both created in a new unique directory |

Report:
`C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_4h_57a860d47197\complete_report.html`

State log:
`C:\Users\jerry_su\.tradingagents\logs\BTC-USDT-SWAP\TradingAgentsStrategy_logs\okx_4h_2026-10-22_9d1510999a66.json`

The Markdown report is 46,878 bytes and the HTML report 81,649 bytes. The state
snapshot ID is b44ed7c75211e0da3bdc5a26aaadd45e8c0fc64edfaf472d9f5a85c2e0bbb74f.
No further real model calls were made after this successful full run. A single
successful run does not prove that an intermittent upstream interruption can
never recur; such failures now stop safely and produce a distinct fault report.

Only high-level status and exception type names are retained. No raw exception
bodies, request headers or credential-bearing URLs are written to this document.

## Files changed for this follow-up

- tradingagents/llm_clients/reliability.py (new): safe classification/failure type.
- tradingagents/llm_clients/openai_client.py: finite subscription timeout,
  limited connection retries and completed-stream validation.
- tradingagents/agents/structured.py: prevent ambiguous request replay.
- tradingagents/graph/setup.py: OKX model-failure boundaries and stage capture.
- tradingagents/graph/trading_graph.py: preserve failure state and write reports.
- tradingagents/agents/state.py and graph/propagation.py: separate model/workflow status.
- tradingagents/dataflows/okx_research.py: retain deterministic risk block on model failure.
- tradingagents/reporting.py and report_html.py: expose model/workflow status.
- cli/main.py: model-failure status and exit code.
- tests/test_llm_stream_reliability.py (new): simulated error and graph/CLI tests.
- docs/llm_connection_validation.md (new): this evidence record.
- docs/okx_research.md: document the distinct model-failure exit code.

The first-phase changes remain uncommitted on feature/okx-usdt-perpetual. Existing
reports are retained and new reports receive unique directories. No push/commit.

Official reference: https://developers.openai.com/api/docs/guides/streaming-responses
describes SSE response lifecycle. Existing project subscription behavior was
also checked against https://developers.openai.com/siwc; it does not establish
the cause of this particular transport interruption.
