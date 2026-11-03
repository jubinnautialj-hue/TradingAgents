"""Bounded append-only JSONL, serialized by a permanent private sidecar lock.

Every writer/rotation actor must use this protocol. Neither file may be
deleted/replaced by a cooperating caller; replacement is detected, not adopted.
"""
import errno
import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path


def _fail(code):
    raise RuntimeError("MARKET_DIAGNOSTIC_" + code)


def _identity(st):
    return [st.st_dev, st.st_ino]


def _directory(path):
    st = os.stat(path)
    if (not stat.S_ISDIR(st.st_mode) or (os.name != "nt"
            and (st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o022))):
        _fail("PARENT_PERMISSIONS")
    return _identity(st)


def _check(fd, path):
    opened = os.fstat(fd)
    named = os.lstat(path)
    if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
            or _identity(opened) != _identity(named) or opened.st_nlink != 1):
        _fail("FILE_IDENTITY")
    if os.name != "nt" and (opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600):
        _fail("FILE_PERMISSIONS")
    return opened


@contextmanager
def _exclusive(fd, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EACCES, errno.EDEADLK}:
                _fail("LOCK_FAILED")
            if time.monotonic() >= deadline:
                _fail("LOCK_TIMEOUT")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)


def _open(path, flags):
    # lstat also rejects links on platforms lacking O_NOFOLLOW.
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            _fail("FILE_IDENTITY")
    except FileNotFoundError:
        pass
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                   | getattr(os, "O_BINARY", 0), 0o600)


def append_audit(path, row, limit, *, timeout=5.0):
    """Return only after one complete, fsynced append; never retry a write.

    A torn previous tail, inode replacement or I/O failure needs manual review.
    No truncation, deletion, automatic repair, or reinitialization is performed.
    """
    if not isinstance(row, bytes) or not row.endswith(b"\n") or b"\n" in row[:-1]:
        _fail("INVALID_RECORD")
    path = Path(path)
    # Canonical parent means aliases share a sidecar; the audit leaf stays
    # un-resolved so a symlink at that leaf cannot be accidentally followed.
    parent = path.parent.resolve(strict=True)
    path = parent / path.name
    parent_identity = _directory(parent)
    lockpath = path.with_name(path.name + ".lock")
    lockfd = _open(lockpath, os.O_RDWR | os.O_CREAT)
    try:
        _check(lockfd, lockpath)
        with _exclusive(lockfd, timeout):
            lockstat = _check(lockfd, lockpath)
            if _directory(parent) != parent_identity:
                _fail("PARENT_IDENTITY")
            if lockstat.st_size > 512:
                _fail("LOCK_STATE")
            os.lseek(lockfd, 0, os.SEEK_SET)
            raw = os.read(lockfd, 513)
            if raw:
                try:
                    binding = json.loads(raw)
                except (ValueError, UnicodeError):
                    _fail("LOCK_STATE")
                if (type(binding) is not dict or binding.get("version") != "market-audit-file-lock-v1"
                        or binding.get("lock") != _identity(lockstat)):
                    _fail("LOCK_STATE")
            else:
                binding = None
            # Once bound, a missing audit file must never be recreated.
            created = False
            if binding is None:
                try:
                    fd = _open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL)
                    created = True
                except FileExistsError:
                    fd = _open(path, os.O_RDWR | os.O_APPEND)
            else:
                fd = _open(path, os.O_RDWR | os.O_APPEND)
            try:
                auditstat = _check(fd, path)
                if binding is not None and binding.get("audit") != _identity(auditstat):
                    _fail("FILE_IDENTITY")
                if auditstat.st_size + len(row) > limit:
                    _fail("CAPACITY")
                if binding is None and not created:
                    # An empty/replaced sidecar must not silently adopt an old
                    # audit inode and bypass a writer still holding the old lock.
                    _fail("UNBOUND_FILE")
                if auditstat.st_size:
                    os.lseek(fd, -1, os.SEEK_END)
                    if os.read(fd, 1) != b"\n":
                        _fail("INCOMPLETE_TAIL")
                if binding is None:
                    binding = {"version": "market-audit-file-lock-v1", "lock": _identity(lockstat),
                               "audit": _identity(auditstat)}
                    data = json.dumps(binding, sort_keys=True).encode()
                    os.lseek(lockfd, 0, os.SEEK_SET)
                    if os.write(lockfd, data) != len(data):
                        _fail("LOCK_STATE")
                    os.fsync(lockfd)
                    if os.name != "nt":
                        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                # Revalidate names and size after acquiring/binding the lock.
                _check(lockfd, lockpath)
                if _directory(parent) != parent_identity:
                    _fail("PARENT_IDENTITY")
                if _check(fd, path).st_size != auditstat.st_size:
                    _fail("CONCURRENT_MODIFICATION")
                if os.write(fd, row) != len(row):
                    _fail("INCOMPLETE")
                os.fsync(fd)
                _check(lockfd, lockpath)
                if _directory(parent) != parent_identity:
                    _fail("PARENT_IDENTITY")
                if _check(fd, path).st_size != auditstat.st_size + len(row):
                    _fail("CONCURRENT_MODIFICATION")
            finally:
                os.close(fd)
    finally:
        os.close(lockfd)
