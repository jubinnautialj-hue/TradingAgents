# Market raw SSE capture v1

Default **off**. This is a private evidence artifact, not a tool-call approval,
SDK event reconstruction, model retry mechanism, research mode or trading signal.
No existing Probe or historical ledger is migrated or resumed.

## Entry and approval

Only the existing `market-protocol-probe-v1` worker can bind capture. Set
`TA_MARKET_RAW_SSE_APPROVAL` to an independently reviewed private JSON file;
setting the variable is insufficient without an exact, valid approval. Ordinary
research rejects this variable. There is no new online CLI or approval command.

The approval has exactly these fields:

- `version`: `market-raw-sse-v1`
- `approved`: boolean; installation drafts must use false
- `purpose`: `market-protocol-probe-v1`
- `authorization_id`: the separately authorized native Probe approval identity
- `ledger_root`: exact native ledger root
- `policy_hash`, `config_hash`: native normalized policy/config digests
- `code_identity_hash`: digest of `code_identity()` (Git HEAD plus tracked project
  Python LF SHA256 values and this helper, including before it is committed)
- `expires_ms`: integer approval deadline
- `directory`: absolute, already provisioned private artifact directory
- `max_bytes`: integer 1..4194304 for the HTTP entity

Binding happens only after the existing native approval, atomic claim, run_id,
binding and unique worker checks. It checks claim SHA256 and native run identity.
The approval is separate from model/research authorization: neither grants the
other. A native production invoker is never replaced by a fixture.

An exclusive `evidence` subdirectory consumes this capture destination exactly
once, including failed preparation. Threads/processes cannot adopt/retry an old
destination. Do not remove it to retry. No live approval has been created as part
of this implementation. Operators must choose new storage, never old Probe or
research archives. Raw evidence is never put into the shared JSONL audit file.

## Capture point and semantics

The existing Session HTTP meter creates a wire identity first. For MODEL only,
capture records request-start clocks and verifies `stream:true`, `store:false`
and the approved model without changing the request. After the original HTTP
send/status accounting, it wraps the **same httpx2 Response.stream** lazily.
No additional HTTP request, eager read, thread, consumer, event conversion,
parameter repair or retry is added. Its source close/error is propagated.

`entity.bin` contains the HTTP entity bytes before Content-Encoding decoding,
not TCP/TLS/HTTP framing. Identity encoding gives the original SSE event/data
lines, including comments, CRLF, whitespace and fragmentation content. With gzip
the file is the original compressed entity; Content-Encoding is retained.
httpx2 continues normal decoding for the SDK, with unchanged input bytes/order.
No separately persisted decoded file or SDK-object-derived pseudo-wire is made.
An offline export can decompress a **copy**, explicitly label it a derived
content-decoded representation and preserve hashes of both; do not overwrite raw.

Metadata contains x-request-id and response x-client-request-id if present,
the actually sent X-Client-Request-Id if present (None if missing), status,
Content-Type/Encoding and Date, endpoint without credentials/query, model,
SDK versions, code/config identities and UTC/monotonic clocks. Authorization,
Cookie, Set-Cookie and arbitrary response headers are not retained. No request
body or arbitrary exception text enters metadata or audit.

`UNFINALIZED` is persisted first. `ENTITY_EOF` only means entity consumption
reached EOF and the captured bytes/hash were committed: it does **not** mean
response.completed, valid tool events, SDK success or confirmed Token usage.
Read interruption/early close becomes `INTERRUPTED` with the actual captured
prefix/hash. Crash can leave UNFINALIZED, a metadata.tmp, or an unclosed entity;
never automatically repair/replay it. Short writes, disk/ACL/identity errors,
capacity refusal and audit failures propagate; a fixed, bounded audit event is
attempted. If the audit sink itself fails there is no fiction that the failure
was durably logged: the original worker must stop under existing failure rules.

Native UNKNOWN usage/PAUSED reconciliation and no retry remain unchanged.
Capture failures may prevent a successful response from being accepted; this is
intentional fail-closed behavior. Even a 200/EOF cannot rescue unknown usage.

## Private storage

Linux: current uid, directories exactly 0700, files exactly 0600. Windows: native
owner/DACL inspection accepts only the current user and SYSTEM allow ACEs;
chmod is not used as a substitute for ACL security. Provision storage and the
approval externally with that DACL. The library does not broaden permissions.
Python 3.13 Windows mkdir(0700) injects extra ACEs, so child directories instead
inherit the already verified private parent. The raw fd is explicitly binary.

Reparse/symlink paths, hardlinks, file/directory identity changes, size drift and
insecure permissions are rejected. Entity writes are fsynced before yielding;
short writes are not retried. Metadata uses an exclusive temporary file,
fsync and atomic replacement, plus POSIX directory fsync. The entity has its own
4MiB maximum; metadata has a separate 256KiB maximum; the existing audit retains
its independent 4MiB cap and permanent cross-process side lock. No truncation,
rotation, deletion, overwrite of an old entity or automatic adoption is allowed.

Raw content can include private text, tool arguments or echoed instructions.
It is intentionally not scrubbed (otherwise it would not be raw). Access and
retention require explicit operator control; encryption at rest is not supplied
by this module. There is no automatic upload/export or secret redaction promise
for raw artifacts. For support, review a restricted copy under a new explicit
export approval; any redacted representation must be labeled not-original.

## Rollback and limits

Without the environment variable no capture directory is created and ordinary
flow keeps its existing behavior. Disable before a new authorized run; never
hot-swap code/approval, clear a consumed destination, reuse a claim or reset a
ledger. These production source changes alter the native implementation hash
through guarded_research.py. New Linux byte fingerprints and native environment
acceptance are required; Windows hashes are not Linux approval.

Tests are offline httpx2 MockTransport, including the real SDK, native fixture
Session/Meter/Probe worker, fragmented Market tool events, gzip, interruption,
UNKNOWN pause, file permissions, write/audit failures and process competition.
They create only TEST_FIXTURE ledgers and synthetic content. Linux POSIX tests,
real disk/power loss, mounted filesystems and real latency remain separate
acceptance work. Fsync/DACL checks add backpressure; zero timing impact is not
claimed. Resource caps and no-retry rules must remain in effect. First release
supports only the existing synchronous Probe client; other HTTP client types or
pre-consumed response bodies fail closed rather than fabricate an original stream.
