"""Stdlib-only OS tests: executable directly on Linux without model packages."""
import importlib.util
import json
import multiprocessing as mp
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

SOURCE = Path(__file__).resolve().parents[1] / "tradingagents/dataflows/audit_file.py"
spec = importlib.util.spec_from_file_location("audit_under_test", SOURCE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def row(key):
    return (json.dumps({"id": key, "code": "FIXTURE"}, sort_keys=True) + "\n").encode()


def write_worker(path, count, label, gate, queue, limit):
    if gate is not None:
        gate.wait(10)
    try:
        for i in range(count):
            audit.append_audit(path, row(f"{label}_{i}"), limit)
        queue.put("APPENDED")
    except Exception as exc:
        queue.put(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)


def hold_worker(lockpath, ready, release):
    fd = os.open(lockpath, os.O_RDWR)
    try:
        with audit._exclusive(fd, 1):
            ready.set()
            release.wait(10)
    finally:
        os.close(fd)


def legacy_worker(path, limit, gate, queue):
    # Exact check/append ordering of the reviewed legacy critical section.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        observed = os.fstat(fd).st_size
        gate.wait(10)
        payload = row("legacy")
        if observed + len(payload) > limit:
            queue.put("CAPACITY")
        else:
            os.write(fd, payload)
            os.fsync(fd)
            queue.put("APPENDED")
    finally:
        os.close(fd)


class AuditFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="market-audit-FIXTURE-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit.jsonl"
        self.ctx = mp.get_context("spawn")

    def start(self, target, args):
        p = self.ctx.Process(target=target, args=args)
        p.start()
        def cleanup():
            if p.is_alive():
                p.terminate()
            p.join(5)
        self.addCleanup(cleanup)
        return p

    def join(self, processes):
        for p in processes:
            p.join(15)
            self.assertFalse(p.is_alive(), "deadlock")
            self.assertEqual(p.exitcode, 0)

    def prime(self):
        audit.append_audit(self.path, row("first"), 4194304)

    def test_old_race_and_fixed_capacity(self):
        limit = 4194304
        legacy_size = None
        if os.name != "nt":  # The reported capacity race uses POSIX O_APPEND.
            self.path.write_bytes(b"\n" * (limit - len(row("legacy")) - 1))
            os.chmod(self.path, 0o600)
            barrier = self.ctx.Barrier(2); q = self.ctx.Queue()
            ps = [self.start(legacy_worker, (str(self.path), limit, barrier, q)) for _ in range(2)]
            self.join(ps)
            self.assertEqual([q.get(timeout=2) for _ in ps], ["APPENDED", "APPENDED"])
            self.assertGreater(self.path.stat().st_size, limit)
            legacy_size = self.path.stat().st_size
        # New fixture, not truncation or replacement of a bound audit file.
        fixed = self.path.with_name("fixed.jsonl")
        prefix = b'{"padding":"'
        suffix = b'"}\n'
        target_size = limit - len(row("same_0")) - 1
        padding = prefix + b"x" * (target_size - len(prefix) - len(suffix)) + suffix
        audit.append_audit(fixed, padding, limit)
        barrier = self.ctx.Barrier(2); q = self.ctx.Queue()
        ps = [self.start(write_worker, (str(fixed), 1, "same", barrier, q, limit)) for _ in range(2)]
        self.join(ps)
        results = [q.get(timeout=2) for _ in ps]
        self.assertEqual(results.count("APPENDED"), 1)
        self.assertEqual(results.count("MARKET_DIAGNOSTIC_CAPACITY"), 1)
        self.assertLessEqual(fixed.stat().st_size, limit)
        print(json.dumps({"case": "capacity_race", "limit": limit,
                          "legacy_size": legacy_size, "fixed_size": fixed.stat().st_size,
                          "results": sorted(results)}), flush=True)

    def test_high_concurrency_unique_complete_records(self):
        threaded = self.path.with_name("threaded.jsonl")
        def write_thread(label):
            for i in range(20):
                audit.append_audit(threaded, row(f"thread{label}_{i}"), 4194304)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(write_thread, i) for i in range(4)]
            for future in futures:
                future.result(timeout=15)
        thread_rows = [json.loads(line) for line in threaded.read_bytes().splitlines()]
        self.assertEqual(len(thread_rows), 80)
        self.assertEqual(len({r["id"] for r in thread_rows}), 80)
        q = self.ctx.Queue(); barrier = self.ctx.Barrier(8)
        ps = [self.start(write_worker, (str(self.path), 40, f"p{i}", barrier, q, 4194304)) for i in range(8)]
        self.join(ps)
        self.assertEqual([q.get(timeout=2) for _ in ps], ["APPENDED"] * 8)
        records = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        self.assertEqual(len(records), 320)
        self.assertEqual(len({r["id"] for r in records}), 320)
        print(json.dumps({"case": "high_concurrency", "threads": 4, "thread_records": 80, "processes": 8,
                          "complete_records": len(records), "unique_ids": 320}), flush=True)

    def test_lock_timeout_and_crash_release(self):
        self.prime(); before = self.path.read_bytes()
        ready = self.ctx.Event(); release = self.ctx.Event()
        p = self.start(hold_worker, (str(self.path) + ".lock", ready, release))
        self.assertTrue(ready.wait(5))
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "LOCK_TIMEOUT"):
            audit.append_audit(self.path, row("denied"), 4194304, timeout=0.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.path.read_bytes(), before)
        p.terminate(); p.join(5)
        audit.append_audit(self.path, row("after_crash"), 4194304, timeout=1)
        self.assertEqual(len(self.path.read_bytes().splitlines()), 2)

    def test_audit_replacement_rejected_across_calls(self):
        self.prime(); old = self.path.with_name("old.jsonl")
        self.path.rename(old); self.path.write_bytes(row("replacement")); self.path.chmod(0o600)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "FILE_IDENTITY"):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(old.read_bytes(), row("first"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_bound_file_not_recreated(self):
        self.prime(); self.path.rename(self.path.with_name("saved.jsonl"))
        with self.assertRaises(OSError):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertFalse(self.path.exists())

    def test_lock_replacement_rejected(self):
        self.prime(); lock = Path(str(self.path) + ".lock")
        lock.rename(lock.with_name("saved.lock")); lock.write_bytes((lock.with_name("saved.lock")).read_bytes()); lock.chmod(0o600)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "LOCK_STATE"):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(self.path.read_bytes(), before)

    def test_empty_lock_replacement_cannot_adopt_old_inode(self):
        self.prime(); before = self.path.read_bytes()
        lock = Path(str(self.path) + ".lock")
        lock.rename(lock.with_name("saved.lock")); lock.write_bytes(b""); lock.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "UNBOUND_FILE"):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(self.path.read_bytes(), before)

    def test_preexisting_unbound_audit_never_adopted(self):
        self.path.write_bytes(row("legacy")); self.path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "UNBOUND_FILE"):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(self.path.read_bytes(), row("legacy"))

    def test_replacement_during_acquisition_rejected(self):
        self.prime(); before = self.path.read_bytes(); original = audit._check
        count = [0]
        def check(fd, path):
            if str(path).endswith(".lock"):
                count[0] += 1
                if count[0] == 2:
                    path.rename(path.with_name("saved.lock")); path.write_bytes(b""); path.chmod(0o600)
            return original(fd, path)
        # Windows denies rename of a held file; POSIX allows it and our identity
        # check must reject the replaced name. Both must fail before append.
        with patch.object(audit, "_check", check), self.assertRaises((RuntimeError, OSError)) as error:
            audit.append_audit(self.path, row("denied"), 4194304)
        if os.name != "nt":
            self.assertIn("FILE_IDENTITY", str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)

    def test_no_truncation_when_full(self):
        self.prime(); before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "CAPACITY"):
            audit.append_audit(self.path, row("denied"), len(before))
        self.assertEqual(self.path.read_bytes(), before)

    def test_partial_write_is_failure_and_torn_tail_blocks_next(self):
        self.prime(); before = self.path.read_bytes(); original = os.write
        def partial(fd, data):
            if os.fstat(fd).st_ino == self.path.stat().st_ino:
                return original(fd, data[:5])
            return original(fd, data)
        with patch.object(audit.os, "write", partial), self.assertRaisesRegex(RuntimeError, "INCOMPLETE"):
            audit.append_audit(self.path, row("partial"), 4194304)
        self.assertTrue(self.path.read_bytes().startswith(before))
        damaged = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "INCOMPLETE_TAIL"):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(self.path.read_bytes(), damaged)

    @unittest.skipIf(os.name == "nt", "POSIX inode ownership/mode checks tested on Linux")
    def test_mode_0600_and_permission_failure(self):
        self.prime()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(self.path) + ".lock").stat().st_mode), 0o600)
        before = self.path.read_bytes(); self.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "FILE_PERMISSIONS"):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "POSIX links tested on Linux")
    def test_symlinks_and_hardlinks_rejected(self):
        target = self.path.with_name("target"); target.write_bytes(row("saved")); target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(RuntimeError):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(target.read_bytes(), row("saved"))
        self.path.unlink(); os.link(target, self.path)
        with self.assertRaisesRegex(RuntimeError, "FILE_IDENTITY"):
            audit.append_audit(self.path, row("denied"), 4194304)

    def test_io_failure_not_success(self):
        self.prime(); before = self.path.read_bytes()
        with patch.object(audit.os, "write", side_effect=OSError("FIXTURE")), self.assertRaises(OSError):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(self.path.read_bytes(), before)

    def test_lock_os_failure_not_retried(self):
        self.prime(); before = self.path.read_bytes()
        original = audit.os.name
        if original == "nt":
            target = "msvcrt.locking"
        else:
            target = "fcntl.flock"
        import errno
        with patch(target, side_effect=OSError(errno.EIO, "FIXTURE")) as mocked:
            with self.assertRaisesRegex(RuntimeError, "LOCK_FAILED"):
                audit.append_audit(self.path, row("denied"), 4194304)
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_sidecar_never_reinitialized(self):
        self.prime(); before = self.path.read_bytes()
        lock = Path(str(self.path) + ".lock")
        lock.write_bytes(b"partial{")
        with self.assertRaisesRegex(RuntimeError, "LOCK_STATE"):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(lock.read_bytes(), b"partial{")
        self.assertEqual(self.path.read_bytes(), before)

    def test_fsync_failure_never_reports_success(self):
        self.prime(); before = self.path.read_bytes()
        with patch.object(audit.os, "fsync", side_effect=OSError("FIXTURE")), self.assertRaises(OSError):
            audit.append_audit(self.path, row("uncertain"), 4194304)
        # A complete write may precede failed durability confirmation. Preserve
        # evidence, never replay it or claim it was durably successful.
        self.assertEqual(self.path.read_bytes(), before + row("uncertain"))

    @unittest.skipIf(os.name == "nt", "POSIX mode checks tested on Linux")
    def test_private_lock_permissions_rechecked_after_acquisition(self):
        self.prime(); before = self.path.read_bytes(); original = audit._check
        count = [0]
        def changed(fd, path):
            if str(path).endswith(".lock"):
                count[0] += 1
                if count[0] == 2:
                    path.chmod(0o644)
            return original(fd, path)
        with patch.object(audit, "_check", changed), self.assertRaisesRegex(RuntimeError, "FILE_PERMISSIONS"):
            audit.append_audit(self.path, row("denied"), 4194304)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions tested on Linux")
    def test_untrusted_parent_permissions_fail_before_append(self):
        self.prime(); before = self.path.read_bytes()
        parent = self.path.parent
        try:
            parent.chmod(0o777)
            with self.assertRaisesRegex(RuntimeError, "PARENT_PERMISSIONS"):
                audit.append_audit(self.path, row("denied"), 4194304)
        finally:
            parent.chmod(0o700)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
