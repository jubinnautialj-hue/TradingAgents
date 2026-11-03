"""Offline shared-writer tests. Stdlib path executes unchanged writer AST on Linux."""
import ast
import hashlib
import importlib.util
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import re
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shared_audit", ROOT / "tradingagents/dataflows/audit_file.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
LIMIT = 4194304


def writers():
    # OS-only tests need no framework installation: compile the exact production
    # functions, without rewriting their bodies or replacing the append protocol.
    ns = dict(os=os, json=json, hashlib=hashlib, logging=logging, re=re, time=time,
              append_audit=audit.append_audit, logger=logging.getLogger(__name__))
    for relative, names in (
        ("tradingagents/agents/analysts/market_contract.py", {"VERSION", "AUDIT_LIMIT", "FIELDS", "_identifier", "diagnose"}),
        ("scripts/role_observer.py", {"AUDIT_LIMIT", "write"}),
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        selected = [n for n in tree.body if
                    isinstance(n, ast.FunctionDef) and n.name in names or
                    isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
        exec(compile(ast.Module(body=selected, type_ignores=[]), relative, "exec"), ns)
    return ns


def mixed_worker(path, gate, queue, label, rounds):
    os.environ["TA_SINGLE_AUDIT_FILE"] = path
    ns = writers()
    gate.wait(10)
    try:
        for i in range(rounds):
            key = f"{label}_{i}"
            ns["write"]({"kind": "MODEL_START", "callback_id": key})
            ns["diagnose"]({"id": key, "name": "get_indicators", "args": {}}, "TOOLNODE_ENTRY")
            ns["write"]({"kind": "MODEL_END", "callback_id": key})
        queue.put("OK")
    except Exception as exc:
        queue.put(str(exc))


def capacity_worker(path, gate, queue, kind):
    os.environ["TA_SINGLE_AUDIT_FILE"] = path
    ns = writers()
    gate.wait(10)
    try:
        if kind == "role":
            ns["write"]({"kind": "MODEL_START", "callback_id": "fixture"})
        else:
            ns["diagnose"]({"id": "fixture", "name": "get_indicators", "args": {}}, "TOOLNODE_ENTRY")
        queue.put("OK")
    except RuntimeError as exc:
        queue.put(str(exc))


def hold_lock(path, ready):
    fd = os.open(path + ".lock", os.O_RDWR)
    try:
        with audit._exclusive(fd, 1):
            ready.set()
            time.sleep(30)
    finally:
        os.close(fd)


class SharedWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shared-audit-FIXTURE-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "metadata.jsonl"
        self.env = patch.dict(os.environ, {"TA_SINGLE_AUDIT_FILE": str(self.path)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.ns = writers()

    def processes(self, target, arguments):
        ctx = mp.get_context("spawn")
        barrier, queue = ctx.Barrier(len(arguments)), ctx.Queue()
        ps = [ctx.Process(target=target, args=(str(self.path), barrier, queue, *a)) for a in arguments]
        try:
            for p in ps: p.start()
            for p in ps:
                p.join(20)
                self.assertFalse(p.is_alive(), "deadlock")
                self.assertEqual(p.exitcode, 0)
            return [queue.get(timeout=2) for _ in ps]
        finally:
            for p in ps:
                if p.is_alive(): p.terminate()
                p.join(5)

    def test_mixed_process_order_complete_unique(self):
        self.assertEqual(self.processes(mixed_worker, [(str(i), 20) for i in range(4)]), ["OK"] * 4)
        rows = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        self.assertEqual(len(rows), 240)
        seen = set()
        for r in rows:
            key = r.get("callback_id", r.get("call_id"))
            self.assertNotIn((key, r["kind"]), seen)
            if r["kind"] == "MARKET_TOOL_DIAGNOSTIC": self.assertIn((key, "MODEL_START"), seen)
            if r["kind"] == "MODEL_END": self.assertIn((key, "MARKET_TOOL_DIAGNOSTIC"), seen)
            seen.add((key, r["kind"]))
        self.assertLessEqual(self.path.stat().st_size, LIMIT)
        if os.name != "nt": self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_mixed_capacity_never_exceeds_four_mib(self):
        # Leave 400 bytes: either record fits alone, both cannot fit together.
        size = LIMIT - 400
        padding = b'{"padding":"' + b"x" * (size - 15) + b'"}\n'
        self.assertEqual(len(padding), size)
        audit.append_audit(self.path, padding, LIMIT)
        results = self.processes(capacity_worker, [("role",), ("market",)])
        self.assertEqual(results.count("OK"), 1)
        self.assertEqual(sum("CAPACITY" in r for r in results), 1)
        self.assertLessEqual(self.path.stat().st_size, LIMIT)
        print("mixed_capacity_bytes", self.path.stat().st_size)
        self.assertEqual(len(self.path.read_bytes().splitlines()), 2)

    def test_both_writers_surface_failure_once(self):
        for code in ("LOCK_TIMEOUT", "FILE_IDENTITY", "FILE_PERMISSIONS", "CAPACITY"):
            for name, args in (("write", ({"kind": "MODEL_START"},)),
                               ("diagnose", ({"id": "fixture", "args": {}}, "MODEL_PARSED"))):
                with self.subTest(code=code, writer=name):
                    with patch.dict(self.ns, append_audit=unittest.mock.Mock(side_effect=RuntimeError(code))):
                        with self.assertRaisesRegex(RuntimeError, code): self.ns[name](*args)
                        self.ns["append_audit"].assert_called_once()

    def test_replaced_file_rejected_by_both(self):
        self.ns["write"]({"kind": "MODEL_START"})
        old = self.path.with_suffix(".old")
        self.path.rename(old)
        self.path.touch(mode=0o600)
        for name, args in (("write", ({"kind": "MODEL_END"},)),
                           ("diagnose", ({"id": "fixture", "args": {}}, "MODEL_PARSED"))):
            with self.assertRaisesRegex(RuntimeError, "IDENTITY"): self.ns[name](*args)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_lock_timeout_and_killed_holder_for_both_writers(self):
        self.ns["write"]({"kind": "MODEL_START"})
        ctx = mp.get_context("spawn")
        ready = ctx.Event()
        holder = ctx.Process(target=hold_lock, args=(str(self.path), ready))
        holder.start()
        try:
            self.assertTrue(ready.wait(10))
            before = self.path.read_bytes()
            def bounded(path, payload, limit):
                return audit.append_audit(path, payload, limit, timeout=0.05)
            with patch.dict(self.ns, append_audit=bounded):
                for name, args in (("write", ({"kind": "MODEL_END"},)),
                                   ("diagnose", ({"id": "fixture", "args": {}}, "MODEL_PARSED"))):
                    with self.assertRaisesRegex(RuntimeError, "LOCK_TIMEOUT"): self.ns[name](*args)
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            holder.terminate(); holder.join(5)
        self.assertFalse(holder.is_alive())
        self.ns["diagnose"]({"id": "after_exit", "args": {}}, "TOOLNODE_ENTRY")
        self.ns["write"]({"kind": "MODEL_END"})
        self.assertEqual(len(self.path.read_bytes().splitlines()), 3)

    @unittest.skipIf(os.name == "nt", "POSIX modes exercised on Linux")
    def test_shared_permission_failure_and_symlink(self):
        self.ns["write"]({"kind": "MODEL_START"})
        before = self.path.read_bytes()
        self.path.chmod(0o644)
        for name, args in (("write", ({"kind": "MODEL_END"},)),
                           ("diagnose", ({"id": "fixture", "args": {}}, "MODEL_PARSED"))):
            with self.assertRaisesRegex(RuntimeError, "PERMISSIONS"): self.ns[name](*args)
        self.path.chmod(0o600)
        self.assertEqual(self.path.read_bytes(), before)
        target = self.path.with_suffix(".original")
        self.path.rename(target); self.path.symlink_to(target)
        for name, args in (("write", ({"kind": "MODEL_END"},)),
                           ("diagnose", ({"id": "fixture", "args": {}}, "MODEL_PARSED"))):
            with self.assertRaises((OSError, RuntimeError)): self.ns[name](*args)
        self.assertEqual(target.read_bytes(), before)


class FrameworkObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import langchain_core
        except ImportError:
            raise unittest.SkipTest("Framework unavailable in OS-only Linux interpreter; tested with locked Windows runtime")
        spec = importlib.util.spec_from_file_location("versioned_observer", ROOT / "scripts/role_observer.py")
        cls.observer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.observer)

    def test_framework_propagates_audit_failure(self):
        from langchain_core.callbacks import CallbackManager
        from langchain_core.messages import HumanMessage
        manager = CallbackManager.configure(inheritable_callbacks=[self.observer.RoleObserver()])
        with patch.object(self.observer, "write", side_effect=RuntimeError("FIXTURE_AUDIT_FAILURE")) as sink:
            with self.assertRaisesRegex(RuntimeError, "FIXTURE_AUDIT_FAILURE"):
                manager.on_chat_model_start({}, [[HumanMessage(content="PRIVATE_SENTINEL")]])
            sink.assert_called_once()

    def test_real_modules_share_sink_and_preserve_event_order(self):
        from tradingagents.agents.analysts.market_contract import diagnose
        from langchain_core.messages import HumanMessage
        with tempfile.TemporaryDirectory(prefix="framework-audit-FIXTURE-") as directory:
            path = Path(directory) / "metadata.jsonl"
            with patch.dict(os.environ, TA_SINGLE_AUDIT_FILE=str(path)):
                handler = self.observer.RoleObserver()
                handler.on_chat_model_start({}, [[HumanMessage(content="PRIVATE_SENTINEL")]], run_id="fixture")
                diagnose({"id": "fixture", "name": "get_indicators", "args": {"indicator": "PRIVATE_SENTINEL"}}, "MODEL_PARSED")
                handler.on_llm_error(ValueError("PRIVATE_SENTINEL"), run_id="fixture")
            data = path.read_bytes()
            self.assertNotIn(b"PRIVATE_SENTINEL", data)
            self.assertEqual([json.loads(r)["kind"] for r in data.splitlines()],
                             ["MODEL_START", "MARKET_TOOL_DIAGNOSTIC", "MODEL_ERROR"])
            self.assertTrue(Path(str(path) + ".lock").is_file())

    def test_metadata_schema_and_redaction(self):
        from langchain_core.messages import HumanMessage, AIMessage
        from langchain_core.outputs import LLMResult, ChatGeneration
        events = []
        handler = self.observer.RoleObserver()
        with patch.object(self.observer, "write", side_effect=events.append):
            handler.on_chat_model_start({}, [[HumanMessage(content="PRIVATE_SENTINEL")]], run_id="fixture",
                                        metadata={"langgraph_node": "Market Analyst", "secret": "PRIVATE_SENTINEL"})
            handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="PRIVATE_SENTINEL"))]]), run_id="fixture")
            handler.on_llm_error(ValueError("PRIVATE_SENTINEL"), run_id="fixture")
        self.assertEqual([e["kind"] for e in events], ["MODEL_START", "MODEL_END", "MODEL_ERROR"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(events))
        self.assertEqual(events[0]["role"], "Market Analyst")
        self.assertIsNone(events[1]["total_tokens"])
        self.assertTrue(handler.raise_error)
        self.assertTrue(handler.run_inline)


if __name__ == "__main__":
    unittest.main(verbosity=2)
