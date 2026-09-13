"""
tests/test_failure_paths.py
Regression suite for docs/16 defects (C-1..C-4, A-1..A-3, M-1..M-9, O-1..O-3).

These tests reproduce the FAILURE paths that the original 25-test suite never
exercised. Every test here corresponds to a defect ID in
docs/16_[MIA전략_적대적재검토] ... .md and must stay green.
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from central_hub.hub_daemon import SingleWriterDB, CentralHubDaemon
from central_hub.mcp_server_adapter import MCPServerAdapter, MemoryVault
from central_hub.triage_classifier import TriageClassifier, ErrorCategory, TriagePrescription
from central_hub.stdio_wrapper import HeadlessAgentRunner
from harness.ollama_worker import OllamaWorker


class TestC1LockMutualExclusion(unittest.TestCase):
    """C-1: acquire_lock must be atomic. Exactly one concurrent winner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = MCPServerAdapter(
            db_path=os.path.join(self.tmp.name, "t.db"),
            vault_dir=os.path.join(self.tmp.name, "vault"),
        )

    def tearDown(self):
        self.adapter.close()
        self.tmp.cleanup()

    def test_concurrent_acquire_yields_exactly_one_winner(self):
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def contend(agent):
            barrier.wait()
            res = self.adapter._handle_acquire_lock(
                {"resource_path": "src/app.py", "holder_id": agent}
            )
            with lock:
                results.append(res["status"])

        threads = [threading.Thread(target=contend, args=(f"agent{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count("ACQUIRED"), 1, f"expected 1 winner, got {results}")
        self.assertEqual(results.count("LOCKED"), 7)

    def test_lock_is_not_stealable_by_another_holder(self):
        self.adapter._handle_acquire_lock({"resource_path": "p", "holder_id": "a"})
        res = self.adapter._handle_acquire_lock({"resource_path": "p", "holder_id": "b"})
        self.assertEqual(res["status"], "LOCKED")
        rows = self.adapter.db.execute_read(
            "SELECT holder_id FROM resource_locks WHERE resource_path = ?", ("p",)
        )
        self.assertEqual(rows[0][0], "a")

    def test_reentrant_acquire_by_same_holder_succeeds(self):
        self.adapter._handle_acquire_lock({"resource_path": "p", "holder_id": "a"})
        res = self.adapter._handle_acquire_lock({"resource_path": "p", "holder_id": "a"})
        self.assertEqual(res["status"], "ACQUIRED")
        self.assertTrue(res.get("reentrant"))

    def test_expired_lease_can_be_taken_over(self):
        self.adapter._handle_acquire_lock({"resource_path": "p", "holder_id": "dead"})
        # Backdate the lease beyond expiry
        self.adapter.db.execute_write(
            "UPDATE resource_locks SET acquired_at = ? WHERE resource_path = ?",
            (time.time() - (MCPServerAdapter.LOCK_LEASE_SECONDS + 60), "p"),
        )
        res = self.adapter._handle_acquire_lock({"resource_path": "p", "holder_id": "alive"})
        self.assertEqual(res["status"], "ACQUIRED")
        self.assertTrue(res.get("stale_takeover"))


class TestC2VaultPathTraversal(unittest.TestCase):
    """C-2: artifact_type must never escape the vault directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Nest the vault so its parent directories belong to this test, not to the
        # shared system temp directory (which other runs may have littered).
        self.sandbox = os.path.join(self.tmp.name, "a", "b", "c")
        self.vault_dir = os.path.join(self.sandbox, "vault")
        self.vault = MemoryVault(self.vault_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_traversal_payload_is_rejected(self):
        for payload in ["txt/../../../ESCAPED.txt", "../../x", "a/b", "..", "/abs", "x\\..\\y"]:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.vault.store("PWNED", artifact_type=payload)

    def test_no_file_is_created_outside_vault(self):
        try:
            self.vault.store("PWNED", artifact_type="txt/../../../ESCAPED.txt")
        except ValueError:
            pass
        for depth in range(4):
            probe = os.path.join(self.sandbox, *([".."] * depth), "ESCAPED.txt")
            self.assertFalse(os.path.exists(probe), f"escaped {depth} level(s) up")

    def test_valid_artifact_type_still_works(self):
        res = self.vault.store("hello", artifact_type="json")
        self.assertTrue(res["ref_uri"].startswith("ref://vault/"))
        self.assertEqual(self.vault.retrieve(res["ref_uri"]), "hello")

    def test_retrieve_rejects_traversal_uri(self):
        self.assertIsNone(self.vault.retrieve("ref://vault/../../etc/passwd"))


class TestC3TriageInversion(unittest.TestCase):
    """C-3: triage must not invert code-defect and infra verdicts."""

    def test_assertion_failure_wins_over_incidental_infra_word(self):
        log = (
            "tests/test_pay.py::test_refund FAILED\n"
            "E   AssertionError: assert 0 == 1\n"
            "E   payment gateway timed out\n"
        )
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.CODE_DEFECT.value)
        self.assertTrue(res["code_edit_permitted"])

    def test_retry_counter_wording_does_not_override_infra(self):
        """T-1: 'Retry 3 failed' is not test-runner output."""
        for log in [
            "Retry 3 failed: ConnectionRefusedError: Connection refused",
            "pip install: 2 failed downloads; OSError: No space left on device",
        ]:
            with self.subTest(log=log):
                res = TriageClassifier.classify(log, exit_code=1)
                self.assertEqual(res["category"], ErrorCategory.INFRA_ENVIRONMENT.value)
                self.assertFalse(res["code_edit_permitted"])

    def test_git_windows_held_handle_message_is_infra(self):
        """W-1: exact stderr measured from Git for Windows 2.55 with an open handle."""
        res = TriageClassifier.classify(
            "warning: failed to remove held.txt: Invalid argument", exit_code=1
        )
        self.assertEqual(res["category"], ErrorCategory.INFRA_ENVIRONMENT.value)
        self.assertFalse(res["code_edit_permitted"])

    def test_assertion_caused_by_strong_infra_failure_is_infra(self):
        log = (
            "E   AssertionError: expected 200\n"
            "E   sqlite3.OperationalError: database is locked\n"
        )
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.INFRA_ENVIRONMENT.value)

    def test_pytest_summary_banner_is_code_defect(self):
        log = "tests/test_x.py F\n========= 2 failed, 5 passed in 0.31s =========\n"
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.CODE_DEFECT.value)

    def test_unclassified_error_is_not_routed_to_code_repair(self):
        res = TriageClassifier.classify("RuntimeError: something weird happened", exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.UNKNOWN.value)
        self.assertEqual(res["prescription"], TriagePrescription.MANUAL_INSPECTION.value)
        self.assertFalse(res["code_edit_permitted"])

    def test_genuine_infra_error_still_blocks_code_edit(self):
        for log in [
            "OSError: [Errno 98] Address already in use",
            "sqlite3.OperationalError: database is locked",
            "ConnectionRefusedError: [Errno 111] Connection refused",
        ]:
            with self.subTest(log=log):
                res = TriageClassifier.classify(log, exit_code=1)
                self.assertEqual(res["category"], ErrorCategory.INFRA_ENVIRONMENT.value)
                self.assertFalse(res["code_edit_permitted"])

    def test_error_log_with_exit_code_zero_is_still_classified(self):
        res = TriageClassifier.classify("AssertionError: boom", exit_code=0)
        self.assertEqual(res["category"], ErrorCategory.CODE_DEFECT.value)

    def test_empty_log_is_unknown(self):
        res = TriageClassifier.classify("", exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.UNKNOWN.value)
        self.assertFalse(res["code_edit_permitted"])

    def test_spec_conflict_still_detected(self):
        res = TriageClassifier.classify(
            "TypeError: refund() missing 1 required positional argument: 'amount'", exit_code=1
        )
        self.assertEqual(res["category"], ErrorCategory.SPEC_CONFLICT.value)

    def test_scores_are_reported_for_auditability(self):
        res = TriageClassifier.classify("AssertionError: x", exit_code=1)
        self.assertIn("scores", res)


class TestC4InMemoryDefault(unittest.TestCase):
    """C-4: the default db_path must work rather than silently break."""

    def test_memory_db_is_usable_end_to_end(self):
        db = SingleWriterDB(":memory:")
        try:
            res = db.execute_write(
                "INSERT INTO message_bus (sender_id,target_id,event_type,payload,created_at)"
                " VALUES (?,?,?,?,?)",
                ("a", "b", "T", "{}", 1.0),
            )
            self.assertEqual(res["status"], "OK")
            rows = db.execute_read("SELECT sender_id FROM message_bus")
            self.assertEqual(rows[0][0], "a")
        finally:
            db.close()

    def test_two_memory_dbs_are_isolated_from_each_other(self):
        a, b = SingleWriterDB(":memory:"), SingleWriterDB(":memory:")
        try:
            a.execute_write(
                "INSERT INTO message_bus (sender_id,target_id,event_type,payload,created_at)"
                " VALUES (?,?,?,?,?)",
                ("only-in-a", "b", "T", "{}", 1.0),
            )
            self.assertEqual(len(b.execute_read("SELECT * FROM message_bus")), 0)
        finally:
            a.close()
            b.close()

    def test_daemon_default_db_path_constructs(self):
        d = CentralHubDaemon(port=0)
        try:
            res = d.publish_event("s", "ALL", "E", {"k": "v"})
            self.assertEqual(res["status"], "OK")
            self.assertEqual(len(d.get_events()), 1)
        finally:
            d.stop()


class TestM1WriteErrorPropagation(unittest.TestCase):
    """M-1: a failed/timed-out write must never look like success."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SingleWriterDB(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_failed_write_reports_error_status(self):
        res = self.db.execute_write("INSERT INTO no_such_table VALUES (?)", ("x",))
        self.assertEqual(res["status"], "ERROR")

    def test_write_result_always_has_status_key(self):
        res = self.db.execute_write(
            "INSERT INTO message_bus (sender_id,target_id,event_type,payload,created_at)"
            " VALUES (?,?,?,?,?)",
            ("a", "b", "T", "{}", 1.0),
        )
        self.assertIn("status", res)
        self.assertIn("rowcount", res)


class TestM4HubAuthentication(unittest.TestCase):
    """M-4: hub HTTP endpoints must reject unauthenticated writes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.daemon = CentralHubDaemon(
            port=0, db_path=os.path.join(cls.tmp.name, "t.db"), auth_token="s3cret"
        )
        cls.daemon.start()
        cls.base = f"http://127.0.0.1:{cls.daemon.port}"

    @classmethod
    def tearDownClass(cls):
        cls.daemon.stop()
        cls.tmp.cleanup()

    def _post(self, path, body, token=None):
        import urllib.request
        import urllib.error

        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_unauthenticated_publish_is_rejected(self):
        status, _ = self._post("/api/events", {"sender": "evil", "event_type": "X"})
        self.assertEqual(status, 401)

    def test_wrong_token_is_rejected(self):
        status, _ = self._post("/api/events", {"sender": "evil"}, token="wrong")
        self.assertEqual(status, 401)

    def test_correct_token_is_accepted(self):
        status, body = self._post(
            "/api/events", {"sender": "codex", "event_type": "TASK"}, token="s3cret"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "OK")

    def test_health_endpoint_stays_public(self):
        import urllib.request

        with urllib.request.urlopen(self.base + "/api/health", timeout=5) as r:
            self.assertEqual(r.status, 200)


class TestM5InboxDeliveryGuarantee(unittest.TestCase):
    """M-5: reading a card must lease it, not destroy it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = MCPServerAdapter(
            db_path=os.path.join(self.tmp.name, "t.db"),
            vault_dir=os.path.join(self.tmp.name, "vault"),
        )
        self.adapter._handle_send_card(
            {"sender": "codex", "target": "claude", "task_name": "t", "card_text": "do it"}
        )

    def tearDown(self):
        self.adapter.close()
        self.tmp.cleanup()

    def test_unacked_card_is_redelivered_after_lease_expiry(self):
        first = self.adapter._handle_read_inbox({"agent_id": "claude"})
        self.assertEqual(first["count"], 1)
        mid = first["cards"][0]["message_id"]

        # Immediately re-reading must NOT redeliver (lease still held)
        self.assertEqual(self.adapter._handle_read_inbox({"agent_id": "claude"})["count"], 0)

        # Simulate agent crash: expire the lease
        self.adapter.db.execute_write(
            "UPDATE inbox_acks SET leased_until = ? WHERE message_id = ? AND agent_id = ?",
            (time.time() - 1, mid, "claude"),
        )
        self.assertEqual(self.adapter._handle_read_inbox({"agent_id": "claude"})["count"], 1)

    def test_acked_card_is_never_redelivered(self):
        first = self.adapter._handle_read_inbox({"agent_id": "claude"})
        mid = first["cards"][0]["message_id"]
        res = self.adapter._handle_ack_card({"agent_id": "claude", "message_id": mid})
        self.assertEqual(res["status"], "ACKED")
        self.adapter.db.execute_write(
            "UPDATE inbox_acks SET leased_until = ? WHERE message_id = ? AND agent_id = ?",
            (time.time() - 1, mid, "claude"),
        )
        self.assertEqual(self.adapter._handle_read_inbox({"agent_id": "claude"})["count"], 0)

    def test_keep_alive_probes_never_reach_agent_inbox(self):
        self.adapter.db.execute_write(
            "INSERT INTO message_bus (sender_id,target_id,event_type,payload,created_at)"
            " VALUES (?,?,?,?,?)",
            ("hub_daemon", "ALL", "CACHE_KEEP_ALIVE_PROBE", "{}", time.time()),
        )
        cards = self.adapter._handle_read_inbox({"agent_id": "claude"})["cards"]
        self.assertNotIn("CACHE_KEEP_ALIVE_PROBE", [c["event_type"] for c in cards])


class TestM6Idempotency(unittest.TestCase):
    """M-6: same idempotency key must produce exactly one card."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = MCPServerAdapter(
            db_path=os.path.join(self.tmp.name, "t.db"),
            vault_dir=os.path.join(self.tmp.name, "vault"),
        )

    def tearDown(self):
        self.adapter.close()
        self.tmp.cleanup()

    def test_stale_in_flight_reservation_is_reclaimed(self):
        """T-3: a crashed caller must not swallow retries forever."""
        self.adapter.db.execute_write(
            "INSERT INTO idempotency_ledger (idempotency_key, result_json, created_at)"
            " VALUES (?, ?, ?)",
            ("K-crash", json.dumps({"status": "IN_FLIGHT"}),
             time.time() - MCPServerAdapter.IDEMPOTENCY_RESERVATION_SECONDS - 10),
        )
        res = self.adapter._handle_send_card(
            {"sender": "codex", "target": "claude", "task_name": "t",
             "card_text": "x", "idempotency_key": "K-crash"}
        )
        self.assertEqual(res["status"], "SENT")

    def test_fresh_in_flight_reservation_is_respected(self):
        self.adapter.db.execute_write(
            "INSERT INTO idempotency_ledger (idempotency_key, result_json, created_at)"
            " VALUES (?, ?, ?)",
            ("K-live", json.dumps({"status": "IN_FLIGHT"}), time.time()),
        )
        res = self.adapter._handle_send_card(
            {"sender": "codex", "target": "claude", "task_name": "t",
             "card_text": "x", "idempotency_key": "K-live"}
        )
        self.assertEqual(res["status"], "IN_FLIGHT")

    def test_concurrent_same_key_sends_create_one_card(self):
        barrier = threading.Barrier(6)

        def send():
            barrier.wait()
            self.adapter._handle_send_card(
                {
                    "sender": "codex",
                    "target": "claude",
                    "task_name": "t",
                    "card_text": "x",
                    "idempotency_key": "K1",
                }
            )

        threads = [threading.Thread(target=send) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = self.adapter.db.execute_read(
            "SELECT COUNT(*) FROM message_bus WHERE event_type='TASK_CARD'"
        )
        self.assertEqual(rows[0][0], 1)


class TestA2ProtocolNegotiation(unittest.TestCase):
    """A-2: initialize must negotiate the client's protocol version."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = MCPServerAdapter(
            db_path=os.path.join(self.tmp.name, "t.db"),
            vault_dir=os.path.join(self.tmp.name, "vault"),
        )

    def tearDown(self):
        self.adapter.close()
        self.tmp.cleanup()

    def _init(self, version):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version} if version else {},
        }
        return json.loads(self.adapter.handle_json_rpc(json.dumps(req)))

    def test_supported_client_version_is_echoed(self):
        self.assertEqual(self._init("2025-06-18")["result"]["protocolVersion"], "2025-06-18")

    def test_unknown_client_version_falls_back_to_latest_supported(self):
        res = self._init("1999-01-01")["result"]
        self.assertEqual(res["protocolVersion"], MCPServerAdapter.LATEST_PROTOCOL_VERSION)

    def test_default_is_not_legacy(self):
        self.assertNotEqual(MCPServerAdapter.LATEST_PROTOCOL_VERSION, "2024-11-05")

    def test_jsonrpc_batch_is_rejected(self):
        res = json.loads(self.adapter.handle_json_rpc('[{"jsonrpc":"2.0","id":1,"method":"ping"}]'))
        self.assertIn("error", res)


class TestA1KeepAliveHonesty(unittest.TestCase):
    """A-1: the keep-alive probe must be disabled by default and must not lie."""

    def test_keep_alive_is_disabled_by_default(self):
        d = CentralHubDaemon(port=0)
        try:
            self.assertFalse(d.keep_alive_enabled)
        finally:
            d.stop()

    def test_probe_is_marked_local_only(self):
        d = CentralHubDaemon(port=0, keep_alive_enabled=True)
        try:
            d._emit_keep_alive_probe()
            events = d.get_events()
            probe = [e for e in events if e["event_type"] == "CACHE_KEEP_ALIVE_PROBE"][0]
            self.assertFalse(probe["payload"]["refreshes_remote_cache"])
        finally:
            d.stop()


class TestA3SpawnConcurrencyCap(unittest.TestCase):
    """A-3: headless CLI spawning must be capped."""

    def test_concurrent_spawns_never_exceed_cap(self):
        runner = HeadlessAgentRunner("mock", max_concurrency=2)
        peak = {"value": 0}
        live = {"value": 0}
        guard = threading.Lock()

        original = runner._run_subprocess

        def instrumented(*args, **kwargs):
            with guard:
                live["value"] += 1
                peak["value"] = max(peak["value"], live["value"])
            try:
                time.sleep(0.05)
                return {"status": "SUCCESS", "exit_code": 0, "stdout": "", "stderr": ""}
            finally:
                with guard:
                    live["value"] -= 1

        runner._run_subprocess = instrumented
        threads = [threading.Thread(target=lambda: runner.execute_task("p")) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        runner._run_subprocess = original

        self.assertLessEqual(peak["value"], 2)


class TestO1SkeletonNeverLeaksRawSource(unittest.TestCase):
    """O-1: a parse failure must not silently dump the full raw source."""

    def test_unparsable_source_does_not_return_original(self):
        bad = "def broken(:\n    this is not python at all\n" * 5
        res = OllamaWorker.extract_ast_skeleton(bad)
        self.assertIsInstance(res, dict)
        self.assertEqual(res["status"], "PARSE_FAILED")
        self.assertNotIn("this is not python at all", res["skeleton"])

    def test_valid_source_is_reduced(self):
        src = (
            "def calc(data: list[float], scale: float = 1.0) -> dict:\n"
            '    """Doc."""\n'
            "    total = sum(data) * scale\n"
            "    return {'mean': total / len(data)}\n"
        )
        res = OllamaWorker.extract_ast_skeleton(src)
        self.assertEqual(res["status"], "OK")
        self.assertIn("def calc", res["skeleton"])
        self.assertNotIn("sum(data)", res["skeleton"])


class TestO3RtkQualifiedExceptions(unittest.TestCase):
    """O-3: dotted/qualified exception names must be parsed."""

    def test_qualified_exception_name_is_extracted(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "central_hub/hub_daemon.py", line 42, in execute_read\n'
            "    cursor.execute(query, params)\n"
            "sqlite3.OperationalError: database is locked\n"
        )
        res = OllamaWorker.extract_rtk_error_vector(tb)
        self.assertEqual(res["exception_type"], "sqlite3.OperationalError")
        self.assertEqual(res["message"], "database is locked")
        self.assertEqual(res["line"], 42)

    def test_plain_exception_name_still_works(self):
        tb = (
            'File "core/calc.py", line 7, in run\n'
            "ZeroDivisionError: division by zero\n"
        )
        res = OllamaWorker.extract_rtk_error_vector(tb)
        self.assertEqual(res["exception_type"], "ZeroDivisionError")


if __name__ == "__main__":
    unittest.main()
