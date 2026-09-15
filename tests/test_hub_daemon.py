"""
tests/test_hub_daemon.py
Unit and concurrency integration tests for Central Hub Daemon and SingleWriterDB.
"""

import unittest
import threading
import time
import json
import urllib.request
import os
import tempfile
from central_hub.hub_daemon import SingleWriterDB, CentralHubDaemon


class TestSingleWriterDB(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_wal.db")
        self.db = SingleWriterDB(self.db_path)

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_concurrent_writes_zero_lock_error(self):
        """Stress test: 20 concurrent threads writing to SQLite WAL via SingleWriter queue."""
        num_threads = 20
        writes_per_thread = 15
        errors = []

        def worker(thread_idx: int):
            for i in range(writes_per_thread):
                res = self.db.execute_write(
                    "INSERT INTO message_bus (sender_id, target_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (f"agent_{thread_idx}", "ALL", "CONCURRENT_TEST", json.dumps({"idx": i}), time.time())
                )
                if res.get("status") != "OK":
                    errors.append(res)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Encountered write errors: {errors}")
        rows = self.db.execute_read("SELECT count(*) FROM message_bus WHERE event_type='CONCURRENT_TEST'")
        self.assertEqual(rows[0][0], num_threads * writes_per_thread)


class TestCentralHubDaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.temp_dir.name, "test_hub.db")
        cls.port = 8991
        cls.token = "test-token"
        cls.daemon = CentralHubDaemon(port=cls.port, db_path=cls.db_path, auth_token=cls.token)
        cls.daemon.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.daemon.stop()
        cls.temp_dir.cleanup()

    def test_api_health(self):
        url = f"http://127.0.0.1:{self.port}/api/health"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "HEALTHY")

    def test_publish_and_retrieve_events(self):
        pub_url = f"http://127.0.0.1:{self.port}/api/events"
        payload = {
            "sender": "codex_orchestrator",
            "target": "worker_verifier",
            "event_type": "TASK_DISPATCH",
            "payload": {"task": "verify_ast_schema", "priority": "P0"}
        }
        req = urllib.request.Request(
            pub_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(res["status"], "OK")

        # Now GET events
        get_url = f"http://127.0.0.1:{self.port}/api/events?since_id=0"
        get_req = urllib.request.Request(
            get_url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(get_req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            events = data["events"]
            self.assertGreaterEqual(len(events), 1)
            last_event = events[-1]
            self.assertEqual(last_event["sender_id"], "codex_orchestrator")
            self.assertEqual(last_event["event_type"], "TASK_DISPATCH")

    def test_heartbeat_and_state(self):
        hb_url = f"http://127.0.0.1:{self.port}/api/heartbeat"
        hb_payload = {
            "agent_id": "gemini_antigravity",
            "status": "RUNNING",
            "worktree": "worktree_gemini_01"
        }
        req = urllib.request.Request(
            hb_url,
            data=json.dumps(hb_payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)

        # GET /api/state
        state_url = f"http://127.0.0.1:{self.port}/api/state"
        state_req = urllib.request.Request(
            state_url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(state_req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            sessions = {s["agent_id"]: s for s in data["sessions"]}
            self.assertIn("gemini_antigravity", sessions)
            self.assertEqual(sessions["gemini_antigravity"]["status"], "RUNNING")
            self.assertEqual(sessions["gemini_antigravity"]["worktree"], "worktree_gemini_01")


if __name__ == "__main__":
    unittest.main()
