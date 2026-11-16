"""Explicitly approved, private HTTP entity capture. Never reconstruct SDK events."""
import hashlib
import importlib.metadata
import json
import os
import stat
import subprocess
import threading
import time
import re
from functools import lru_cache
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import urlsplit

from tradingagents.dataflows.audit_file import append_audit
from tradingagents.forward_validation import digest

VERSION = 'market-raw-sse-v1'
ENV = 'TA_MARKET_RAW_SSE_APPROVAL'
MAX_BYTES = 4 * 1024**2  # Independent raw entity cap; audit retains its own cap.


def fail(code):
    raise RuntimeError('RAW_SSE_' + code)


@lru_cache(maxsize=1)
def windows_sid():
    value = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True)
    return re.search(r'S-1-[0-9-]+', value)[0]


def windows_acl(path):
    """Read owner/DACL through Win32; no shell or process on the stream path."""
    import ctypes as c
    from ctypes import wintypes as w
    adv = c.WinDLL('advapi32', use_last_error=True)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    get = adv.GetNamedSecurityInfoW
    get.argtypes = [w.LPWSTR, w.DWORD, w.DWORD] + [c.POINTER(c.c_void_p)] * 5
    get.restype = w.DWORD
    convert = adv.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.POINTER(w.ULONG)]
    convert.restype = w.BOOL
    kernel.LocalFree.argtypes = [c.c_void_p]
    owner, group, dacl, sacl, sd = [c.c_void_p() for _ in range(5)]
    if get(str(path), 1, 5, c.byref(owner), c.byref(group), c.byref(dacl), c.byref(sacl), c.byref(sd)):
        fail('ACL_CHECK')
    text = w.LPWSTR()
    try:
        if not dacl.value or not convert(sd, 1, 5, c.byref(text), None):
            fail('ACL_CHECK')
        sddl = text.value
        allowed = {windows_sid(), 'SY'}
        actual_owner = re.search(r'O:(.*?)(?:G:|D:|S:|$)', sddl)
        aces = re.findall(r'\(([^()]*)\)', sddl)
        if not actual_owner or actual_owner[1] not in allowed or not aces:
            fail('PERMISSIONS')
        for ace in aces:
            fields = ace.split(';')
            if len(fields) != 6 or fields[0] not in {'A', 'D'} or (fields[0] == 'A' and fields[-1] not in allowed):
                fail('PERMISSIONS')
    finally:
        if text: kernel.LocalFree(c.cast(text, c.c_void_p))
        kernel.LocalFree(sd)


def private(path, directory=False):
    path = Path(path)
    for parent in (path, *path.parents):
        st = parent.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
            fail('REPARSE_POINT')
    st = path.stat()
    if not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)):
        fail('FILE_TYPE')
    if not directory and st.st_nlink != 1:
        fail('FILE_IDENTITY')
    if os.name == 'nt':
        windows_acl(path)
    elif st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != (0o700 if directory else 0o600):
        fail('PERMISSIONS')
    return (st.st_dev, st.st_ino)


def code_identity():
    root = Path(__file__).resolve().parents[2]
    names = subprocess.check_output(['git', 'ls-files', 'tradingagents', 'cli'], cwd=root, text=True).splitlines()
    paths = {root / n for n in names if n.endswith('.py')}
    paths.add(Path(__file__).resolve())
    files = {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest() for p in sorted(paths)}
    return {'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
            'source_LF_sha256': files}


def stamp():
    return {'utc': datetime.now(UTC).isoformat(), 'monotonic_ns': time.monotonic_ns()}


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try: os.fsync(fd)
        finally: os.close(fd)


def prepare(ledger, run_id, scope, config, audit):
    if not os.environ.get(ENV):
        return None
    try:
        return _prepare(ledger, run_id, scope, config, audit)
    except BaseException:
        append_audit(audit, (json.dumps({'kind': 'RAW_SSE_CAPTURE', 'version': VERSION,
            'state': 'APPROVAL_OR_SETUP_FAILED'}) + '\n').encode(), MAX_BYTES)
        raise


def _prepare(ledger, run_id, scope, config, audit):
    path = os.environ.get(ENV)
    if not path:
        return None
    private(path)
    if Path(path).stat().st_size > 65536:
        fail('APPROVAL_SIZE')
    approval_bytes = Path(path).read_bytes()
    approved = json.loads(approval_bytes)
    fields = {'version', 'approved', 'purpose', 'authorization_id', 'ledger_root', 'policy_hash',
              'config_hash', 'code_identity_hash', 'expires_ms', 'directory', 'max_bytes'}
    if set(approved) != fields or approved['version'] != VERSION or approved['approved'] is not True:
        fail('APPROVAL')
    identity = code_identity()
    if (approved['purpose'] != 'market-protocol-probe-v1' or ledger.load()['policy']['run_purpose'] != approved['purpose']
        or approved['authorization_id'] != scope['authorization_id']
        or approved['ledger_root'] != str(ledger.root)
        or approved['policy_hash'] != digest(ledger.load()['policy'])
        or approved['config_hash'] != digest(config)
        or approved['code_identity_hash'] != digest(identity)
        or type(approved['expires_ms']) is not int or ledger.clock() >= approved['expires_ms']
        or type(approved['max_bytes']) is not int or not 1 <= approved['max_bytes'] <= MAX_BYTES):
        fail('APPROVAL_BINDING')
    binding = json.loads((ledger.root / 'probe/binding.json').read_text())
    claim = ledger.root / 'probe/claim.json'
    claim_hash = hashlib.sha256(claim.read_bytes()).hexdigest()
    if binding['run_id'] != run_id or binding['claim_sha256'] != claim_hash:
        fail('CLAIM_BINDING')
    root = Path(approved['directory'])
    if not root.is_absolute():
        fail('DIRECTORY')
    private(root, True)
    # One consumed capture directory for this approval, shared by all processes.
    folder = root / 'evidence'
    resolved = folder.resolve()
    ledger_root = ledger.root.resolve()
    if resolved == ledger_root or resolved in ledger_root.parents or ledger_root in resolved.parents:
        fail('LEDGER_OVERLAP')
    try:
        # Windows 3.13's mkdir(0700) adds explicit Administrators/OWNER RIGHTS
        # ACEs; inherit only the already validated private parent DACL instead.
        folder.mkdir(mode=0o777 if os.name == 'nt' else 0o700)
    except FileExistsError:
        fail('ALREADY_CONSUMED')
    sync_directory(root)
    return Capture(folder, approved['max_bytes'], audit, {
        'version': VERSION, 'approval_sha256': hashlib.sha256(approval_bytes).hexdigest(),
        'run_id': run_id, 'claim_sha256': claim_hash, 'code_identity': identity,
        'model': ledger.load()['policy']['model'], 'sdk_versions': {
            p: importlib.metadata.version(p) for p in ('openai', 'httpx2', 'langchain-openai', 'langchain-core')},
        'policy_hash': approved['policy_hash'], 'config_hash': approved['config_hash'],
        'approval_deadline_ms': approved['expires_ms']})


class Capture:
    def __init__(self, root, limit, audit, binding):
        self.root, self.limit, self.audit = Path(root), limit, audit
        self.root_id = private(root, True)
        self.meta = {**binding, 'state': 'UNFINALIZED', 'created': stamp(), 'representation':
                     'HTTP_ENTITY_BEFORE_CONTENT_DECODING_NOT_TLS_OR_HTTP_FRAMES', 'max_entity_bytes': limit}
        self.used = False
        self.lock = threading.Lock()
        self.fd = None
        self.size = 0
        self.hash = hashlib.sha256()
        self.save()

    def check(self):
        if private(self.root, True) != self.root_id:
            fail('DIRECTORY_IDENTITY')
        if self.fd is not None:
            st = os.fstat(self.fd)
            named = os.lstat(self.root / 'entity.bin')
            if (st.st_dev, st.st_ino) != (named.st_dev, named.st_ino) or st.st_nlink != 1 or st.st_size != self.size:
                fail('FILE_IDENTITY')
            if private(self.root / 'entity.bin') != (st.st_dev, st.st_ino):
                fail('FILE_IDENTITY')

    def save(self):
        self.check()
        data = json.dumps(self.meta, sort_keys=True, allow_nan=False).encode()
        if len(data) > 262144:
            fail('METADATA_CAPACITY')
        path = self.root / 'metadata.tmp'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            private(path)
        except BaseException:
            os.close(fd)
            raise
        # A leftover tmp is evidence of an incomplete commit, never auto-repaired.
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(path, self.root / 'metadata.json')
        sync_directory(self.root)
        private(self.root / 'metadata.json')

    def event(self, state):
        append_audit(self.audit, (json.dumps({'kind': 'RAW_SSE_CAPTURE', 'version': VERSION,
            'state': state, 'bytes': self.size,
            'binding_sha256': digest({k: self.meta[k] for k in ('run_id', 'claim_sha256')})}) + '\n').encode(), MAX_BYTES)

    def before_request(self, request, wire_id):
        if time.time_ns() // 1_000_000 >= self.meta['approval_deadline_ms']:
            fail('APPROVAL_EXPIRED')
        with self.lock:
            if getattr(self, 'request_started', False):
                fail('SECOND_REQUEST')
            self.request_started = True
        body = json.loads(request.content)
        if (body.get('stream') is not True or body.get('store') is not False
                or body.get('model') != self.meta['model']):
            fail('REQUEST_CONFIG')
        self.meta.update(request_start=stamp(), wire_id=wire_id)
        self.save()

    def attach(self, request, response, wire_id):
        try:
            return self._attach(request, response, wire_id)
        except BaseException:
            try:
                try: self.finish('INTERRUPTED')
                finally: self.event('CAPTURE_SETUP_FAILED')
            finally:
                response.close()
            raise

    def _attach(self, request, response, wire_id):
        import httpx2
        with self.lock:
            if self.used:
                fail('SECOND_RESPONSE')
            self.used = True
        if not isinstance(response, httpx2.Response) or response.is_stream_consumed:
            fail('UNSUPPORTED_STREAM')
        url = urlsplit(str(request.url))
        if url.query or url.fragment or url.username or url.password:
            fail('ENDPOINT')
        if any(len(str(x)) > 4096 for x in [str(request.url), *response.headers.values(), request.headers.get('x-client-request-id', '')]):
            fail('HEADER_CAPACITY')
        self.meta.update(state='UNFINALIZED', wire_id=wire_id, headers_received=stamp(),
            endpoint=url.scheme + '://' + url.netloc + url.path, http_status=response.status_code,
            response_headers={k: response.headers.get(k) for k in
                              ('x-request-id', 'x-client-request-id', 'content-type', 'content-encoding', 'date')},
            sent_x_client_request_id=request.headers.get('x-client-request-id'),
            decoded_representation='SDK iter_bytes content-decoded SSE; not separately persisted')
        self.save()
        self.fd = os.open(self.root / 'entity.bin', os.O_RDWR | os.O_CREAT | os.O_EXCL
                          | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
        sync_directory(self.root)
        self.check()
        self.event('STARTED')
        owner, source = self, response.stream

        class Tee(httpx2.SyncByteStream):
            def __iter__(self):
                with owner.lock:
                    if getattr(owner, 'iterated', False):
                        fail('SECOND_CONSUMER')
                    owner.iterated = True
                try:
                    for chunk in source:
                        owner.check()
                        if owner.size + len(chunk) > owner.limit:
                            fail('CAPACITY')
                        # Short/failed writes are not retried or yielded as successful evidence.
                        if os.write(owner.fd, chunk) != len(chunk):
                            fail('SHORT_WRITE')
                        os.fsync(owner.fd)
                        owner.size += len(chunk); owner.hash.update(chunk)
                        owner.meta.setdefault('first_body_chunk', stamp())
                        yield chunk
                    owner.finish('ENTITY_EOF')
                except BaseException:
                    try:
                        owner.finish('INTERRUPTED')
                    finally:
                        owner.event('CAPTURE_FAILED_OR_INTERRUPTED')
                    raise

            def close(self):
                try:
                    if owner.fd is not None:
                        owner.finish('INTERRUPTED')
                finally:
                    source.close()
        response.stream = Tee()

    def finish(self, state):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            st = os.fstat(fd)
            if private(self.root / 'entity.bin') != (st.st_dev, st.st_ino):
                fail('FILE_IDENTITY')
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            actual = hashlib.sha256()
            actual_size = 0
            while True:
                block = os.read(fd, 65536)
                if not block: break
                actual_size += len(block); actual.update(block)
        finally:
            os.close(fd)
        if state == 'ENTITY_EOF' and (actual_size != self.size or actual.hexdigest() != self.hash.hexdigest()):
            fail('ENTITY_INTEGRITY')
        self.size = actual_size
        # ENTITY_EOF does not assert response.completed, SDK success or confirmed usage.
        self.meta.update(state=state, captured_bytes=actual_size, captured_sha256=actual.hexdigest(),
                         finished=stamp())
        self.save()
        self.event(state)
