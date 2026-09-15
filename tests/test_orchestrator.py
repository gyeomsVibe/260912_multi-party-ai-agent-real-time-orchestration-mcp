"""
tests/test_orchestrator.py
Unit and integration tests for TrinityOrchestrator cross-relay pipeline.
"""

import unittest
import os
import tempfile
from central_hub.trinity_orchestrator import TrinityOrchestrator, OrganismState


class TestTrinityOrchestrator(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "orch_test.db")
        self.vault_dir = os.path.join(self.temp_dir.name, "vault")
        self.orchestrator = TrinityOrchestrator(
            db_path=self.db_path,
            vault_dir=self.vault_dir
        )

    def tearDown(self):
        self.orchestrator.close()
        self.temp_dir.cleanup()

    def test_full_cross_relay_pipeline(self):
        """Verify full 4-stage execution trace from Codex through Antigravity."""
        sample_code = "def add(a: int, b: int) -> int:\n    return a + b\n"
        res = self.orchestrator.execute_cross_relay(
            task_title="Verify Addition Function",
            task_instruction="Audit type annotations and return types",
            target_file="src/math_ops.py",
            source_code=sample_code,
            mock_mode=True
        )

        self.assertEqual(res["overall_status"], "SUCCESS")
        self.assertTrue(res["tx_id"].startswith("tx_"))
        self.assertEqual(len(res["trace"]), 4)

        # Check stage agents
        self.assertEqual(res["trace"][0]["agent"], "Codex (Brain)")
        self.assertEqual(res["trace"][1]["agent"], "Ollama (Slave)")
        self.assertEqual(res["trace"][2]["agent"], "Claude Code (Immune)")
        self.assertEqual(res["trace"][3]["agent"], "Antigravity (Sensory)")

        # Verify briefing is generated
        self.assertIn("[Trinity-ACE 완결]", res["briefing"])
        self.assertIn("src/math_ops.py", res["briefing"])

    def test_ollama_fallback_to_two_alive(self):
        """When local slave is unreachable, verify fail-fast transition to 2-ALIVE."""
        # Unreachable port for Ollama
        orch_fallback = TrinityOrchestrator(
            db_path=os.path.join(self.temp_dir.name, "fallback.db"),
            vault_dir=self.vault_dir,
            ollama_base_url="http://127.0.0.1:59999"
        )
        try:
            res = orch_fallback.execute_cross_relay(
                task_title="Test Fallback",
                task_instruction="Verify failover",
                source_code="x = 10",
                mock_mode=False
            )
            self.assertEqual(res["overall_status"], "SUCCESS")
            self.assertEqual(res["organism_state"], OrganismState.TWO_ALIVE.value)
            self.assertEqual(res["trace"][1]["status"], "FALLBACK_CLOUD")
        finally:
            orch_fallback.close()

    def test_lock_release_after_execution(self):
        """Verify that exclusive lock on target file is completely released after execution."""
        target = "src/critical_service.py"
        self.orchestrator.execute_cross_relay(
            task_title="Lock Verification",
            task_instruction="Check lock lifecycle",
            target_file=target,
            mock_mode=True
        )
        # Lock should be free, so acquiring it now should return ACQUIRED
        res_lock = self.orchestrator.adapter.execute_tool("trinity_acquire_lock", {
            "resource_path": target,
            "holder_id": "test_checker"
        })
        self.assertEqual(res_lock["status"], "ACQUIRED")


if __name__ == "__main__":
    unittest.main()
