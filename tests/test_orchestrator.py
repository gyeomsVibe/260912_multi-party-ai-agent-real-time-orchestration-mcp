"""
tests/test_orchestrator.py
Unit and integration tests for TrinityOrchestrator cross-relay pipeline.

The pipeline has no real executor or verifier yet, so these tests pin the P0 contract:
no run may report SUCCESS without executor and verification evidence.
"""

import json
import unittest
import os
import tempfile
from central_hub.trinity_orchestrator import (
    TrinityOrchestrator,
    OrganismState,
    derive_overall_status,
)


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

    def test_mock_pipeline_is_simulated_not_success(self):
        """A mock run traverses all four stages but must never claim SUCCESS."""
        sample_code = "def add(a: int, b: int) -> int:\n    return a + b\n"
        res = self.orchestrator.execute_cross_relay(
            task_title="Verify Addition Function",
            task_instruction="Audit type annotations and return types",
            target_file="src/math_ops.py",
            source_code=sample_code,
            mock_mode=True
        )

        self.assertEqual(res["overall_status"], "SIMULATED")
        self.assertTrue(res["tx_id"].startswith("tx_"))
        self.assertEqual(len(res["trace"]), 4)
        self.assertEqual(
            [s["agent"] for s in res["trace"]],
            ["Codex (Planner)", "Ollama (Preprocess)", "Antigravity (Executor)", "Local Verifier"],
        )

        stage3 = res["trace"][2]["execution"]
        self.assertEqual(stage3["status"], "SIMULATED")
        self.assertIsNone(stage3["exit_code"])
        self.assertEqual(res["trace"][3]["verification_status"], "SIMULATED")
        self.assertIsNone(res["trace"][3]["exit_code"])

        self.assertIn("src/math_ops.py", res["briefing"])
        self.assertIn("모의 실행", res["briefing"])
        for claim in ("완결", "완료", "VERIFIED_GREEN"):
            self.assertNotIn(claim, res["briefing"])

    def test_live_run_without_executor_is_not_configured(self):
        """Without an executor the live path reports NOT_CONFIGURED, not a fake SUCCESS."""
        orch = TrinityOrchestrator(
            db_path=os.path.join(self.temp_dir.name, "live.db"),
            vault_dir=self.vault_dir,
            ollama_base_url="http://127.0.0.1:59999"  # unreachable
        )
        try:
            res = orch.execute_cross_relay(
                task_title="Test Fallback",
                task_instruction="Verify failover",
                source_code="x = 10",
                mock_mode=False
            )
            self.assertEqual(res["overall_status"], "NOT_CONFIGURED")
            self.assertEqual(res["organism_state"], OrganismState.TWO_ALIVE.value)
            self.assertEqual(res["trace"][1]["status"], "FALLBACK_CLOUD")
            self.assertEqual(res["trace"][2]["execution"]["status"], "NOT_CONFIGURED")
            self.assertEqual(res["trace"][3]["verification_status"], "UNVERIFIED")
        finally:
            orch.close()

    def test_no_result_anywhere_reports_success(self):
        """Guard against a hard-coded SUCCESS reappearing in any stage."""
        for mock in (True, False):
            with self.subTest(mock_mode=mock):
                res = self.orchestrator.execute_cross_relay(
                    task_title="Scan", task_instruction="x", mock_mode=mock
                )
                self.assertNotIn('"SUCCESS"', json.dumps(res["trace"]))
                self.assertNotEqual(res["overall_status"], "SUCCESS")

    def test_liveness_starts_unknown_and_is_never_promoted_by_ollama(self):
        self.assertEqual(self.orchestrator.state, OrganismState.UNKNOWN)
        res = self.orchestrator.execute_cross_relay(
            task_title="Liveness", task_instruction="x", source_code="y = 1", mock_mode=True
        )
        self.assertNotEqual(res["organism_state"], OrganismState.THREE_ALIVE.value)

    def test_lock_release_after_execution(self):
        """The pipeline's own lock is released after execution."""
        target = "src/critical_service.py"
        self.orchestrator.execute_cross_relay(
            task_title="Lock Verification",
            task_instruction="Check lock lifecycle",
            target_file=target,
            mock_mode=True
        )
        res_lock = self.orchestrator.adapter.execute_tool("trinity_acquire_lock", {
            "resource_path": target,
            "holder_id": "test_checker"
        })
        self.assertEqual(res_lock["status"], "ACQUIRED")

    def test_lock_conflict_halts_and_preserves_foreign_lock(self):
        """Fail-closed: a held lock stops the patch and is not released by the pipeline."""
        target = "src/shared.py"
        self.orchestrator.adapter.execute_tool("trinity_acquire_lock", {
            "resource_path": target, "holder_id": "other_agent"
        })
        res = self.orchestrator.execute_cross_relay(
            task_title="Conflict", task_instruction="x", target_file=target, mock_mode=True
        )
        self.assertEqual(res["overall_status"], "HALTED_LOCK_CONFLICT")
        self.assertEqual(len(res["trace"]), 3)
        self.assertNotIn("execution", res["trace"][2])

        holder = self.orchestrator.adapter.db.execute_read(
            "SELECT holder_id FROM resource_locks WHERE resource_path = ?", (target,)
        )
        self.assertEqual(holder[0][0], "other_agent")

    def test_skeleton_metrics_are_reported_and_raw_source_not_leaked(self):
        ok = self.orchestrator.execute_cross_relay(
            task_title="Skeleton", task_instruction="x",
            source_code="def f(a):\n    return a * 2\n", mock_mode=True
        )
        self.assertEqual(ok["trace"][1]["skeleton_status"], "OK")
        self.assertGreater(ok["trace"][1]["skeleton_chars"], 0)

        bad_source = "def broken(:\n    SECRET_RAW_BODY_MARKER\n"
        bad = self.orchestrator.execute_cross_relay(
            task_title="Broken", task_instruction="x", source_code=bad_source, mock_mode=True
        )
        self.assertEqual(bad["trace"][1]["skeleton_status"], "PARSE_FAILED")
        self.assertNotIn("SECRET_RAW_BODY_MARKER", json.dumps(bad, ensure_ascii=False))


class _FakeExecutor:
    def __init__(self, receipt):
        self.receipt = receipt
        self.prompts = []

    def run(self, prompt, cwd=None):
        self.prompts.append(prompt)
        return dict(self.receipt)


class TestExecutorWiring(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, receipt):
        fake = _FakeExecutor(receipt)
        orch = TrinityOrchestrator(
            db_path=os.path.join(self.temp_dir.name, "exec.db"),
            vault_dir=os.path.join(self.temp_dir.name, "vault"),
            executor=fake,
        )
        try:
            res = orch.execute_cross_relay(
                task_title="Budget task", task_instruction="Summarize", mock_mode=False
            )
            stored = orch.adapter.vault.retrieve(res["trace"][2]["execution"]["receipt_ref"])
        finally:
            orch.close()
        return res, fake, stored

    def test_executor_success_without_verification_is_unverified(self):
        res, fake, stored = self._run({"status": "SUCCESS", "exit_code": 0, "response_sha256": "a" * 64})
        self.assertEqual(res["trace"][2]["execution"]["status"], "SUCCESS")
        self.assertEqual(res["overall_status"], "UNVERIFIED")
        self.assertEqual(len(fake.prompts), 1)
        self.assertIn("Summarize", fake.prompts[0])
        self.assertEqual(json.loads(stored)["status"], "SUCCESS")

    def test_executor_failure_is_failed(self):
        res, _, _ = self._run({"status": "FAILED", "exit_code": 0, "error": "empty response despite exit code 0"})
        self.assertEqual(res["overall_status"], "FAILED")


class TestVerificationWiring(unittest.TestCase):
    """Stage 4 with a real local CommandVerifier; the executor is faked to spend no quota."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, receipt, verifier_argv):
        from central_hub.command_verifier import CommandVerifier
        import sys
        fake = _FakeExecutor(receipt)
        orch = TrinityOrchestrator(
            db_path=os.path.join(self.temp_dir.name, "verify.db"),
            vault_dir=os.path.join(self.temp_dir.name, "vault"),
            executor=fake,
            verifier=CommandVerifier([sys.executable, *verifier_argv]),
        )
        try:
            res = orch.execute_cross_relay(task_title="Verify", task_instruction="x", mock_mode=False)
            v = res["trace"][3]["verification"]
            stored = orch.adapter.vault.retrieve(v["receipt_ref"]) if v.get("receipt_ref") else None
        finally:
            orch.close()
        return res, v, stored

    def test_executor_success_plus_passing_check_is_success(self):
        res, v, stored = self._run({"status": "SUCCESS", "exit_code": 0}, ["-c", "print('tests ok')"])
        self.assertEqual(res["overall_status"], "SUCCESS")
        self.assertEqual(v["status"], "VERIFIED")
        self.assertEqual(len(v["evidence"]), 1)
        self.assertEqual(json.loads(stored)["exit_code"], 0)
        self.assertIn("실행과 검증 증거 확인", res["briefing"])

    def test_failing_check_makes_the_run_failed(self):
        res, v, _ = self._run({"status": "SUCCESS", "exit_code": 0}, ["-c", "import sys; sys.exit(1)"])
        self.assertEqual(v["status"], "FAILED")
        self.assertEqual(res["overall_status"], "FAILED")

    def test_check_is_skipped_when_execution_failed(self):
        res, v, stored = self._run({"status": "FAILED", "exit_code": 1}, ["-c", "print('never')"])
        self.assertEqual(v["status"], "SKIPPED")
        self.assertIsNone(stored)
        self.assertEqual(res["overall_status"], "FAILED")


class TestFailureTriageRouting(TestVerificationWiring):
    """Failed execution or verification is triaged into a next_action."""

    def test_success_has_no_triage(self):
        res, v, _ = self._run({"status": "SUCCESS", "exit_code": 0}, ["-c", "print('ok')"])
        self.assertEqual(res["next_action"], "NONE")
        self.assertNotIn("triage", res["trace"][2]["execution"])
        self.assertNotIn("triage", v)

    def test_executor_infra_failure_halts_without_code_edits(self):
        res, _, _ = self._run(
            {"status": "FAILED", "exit_code": 1, "stderr_tail": "ConnectionRefusedError: Connection refused"},
            ["-c", "print('never')"],
        )
        triage = res["trace"][2]["execution"]["triage"]
        self.assertEqual(triage["category"], "INFRA_ENVIRONMENT")
        self.assertFalse(triage["code_edit_permitted"])
        self.assertEqual(res["next_action"], "HALT_AND_RELEASE_RESOURCE")

    def test_executor_timeout_is_triaged_as_environment(self):
        res, _, _ = self._run({"status": "TIMEOUT", "exit_code": None, "error": "did not finish"},
                              ["-c", "print('never')"])
        self.assertEqual(res["trace"][2]["execution"]["triage"]["category"], "INFRA_ENVIRONMENT")
        self.assertEqual(res["next_action"], "HALT_AND_RELEASE_RESOURCE")

    def test_failing_test_run_routes_to_repair(self):
        failing = "import sys; print('E   AssertionError: assert 0 == 1'); sys.exit(1)"
        res, v, _ = self._run({"status": "SUCCESS", "exit_code": 0}, ["-c", failing])
        self.assertEqual(v["triage"]["category"], "CODE_DEFECT")
        self.assertTrue(v["triage"]["code_edit_permitted"])
        self.assertEqual(res["next_action"], "ROUTE_TO_REPAIR_AGENT")
        self.assertEqual(res["overall_status"], "FAILED")

    def test_unclassified_failure_goes_to_manual_inspection(self):
        weird = "import sys; print('something odd happened'); sys.exit(2)"
        res, v, _ = self._run({"status": "SUCCESS", "exit_code": 0}, ["-c", weird])
        self.assertEqual(v["triage"]["category"], "UNKNOWN")
        self.assertFalse(v["triage"]["code_edit_permitted"])
        self.assertEqual(res["next_action"], "MANUAL_INSPECTION")


class TestDeriveOverallStatus(unittest.TestCase):
    """SUCCESS is reachable only with executor exit 0 and verification evidence."""

    def test_success_requires_exit_zero_and_evidence(self):
        exec_ok = {"status": "SUCCESS", "exit_code": 0}
        verified = {"status": "VERIFIED", "evidence": ["pytest exit 0 sha256:abc"]}
        self.assertEqual(derive_overall_status(exec_ok, verified), "SUCCESS")

    def test_verified_without_evidence_is_unverified(self):
        exec_ok = {"status": "SUCCESS", "exit_code": 0}
        self.assertEqual(
            derive_overall_status(exec_ok, {"status": "VERIFIED", "evidence": []}), "UNVERIFIED"
        )

    def test_nonzero_exit_is_failed(self):
        self.assertEqual(
            derive_overall_status({"status": "SUCCESS", "exit_code": 1},
                                  {"status": "VERIFIED", "evidence": ["x"]}),
            "FAILED",
        )

    def test_failed_or_timed_out_verification_is_failed(self):
        exec_ok = {"status": "SUCCESS", "exit_code": 0}
        for status in ("FAILED", "TIMEOUT"):
            with self.subTest(status=status):
                self.assertEqual(
                    derive_overall_status(exec_ok, {"status": status, "evidence": []}), "FAILED"
                )

    def test_simulated_and_not_configured_never_succeed(self):
        verified = {"status": "VERIFIED", "evidence": ["x"]}
        self.assertEqual(
            derive_overall_status({"status": "SIMULATED", "exit_code": 0}, verified), "SIMULATED"
        )
        self.assertEqual(
            derive_overall_status({"status": "NOT_CONFIGURED", "exit_code": None}, verified),
            "NOT_CONFIGURED",
        )


if __name__ == "__main__":
    unittest.main()
