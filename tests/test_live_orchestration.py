"""
tests/test_live_orchestration.py
Verification suite for TrinityOrchestrator Fail-Closed Lock, Dynamic Liveness,
and Worktree Pool Lifecycle Integration (docs/21).
"""

import unittest
import os
import tempfile
from central_hub.trinity_orchestrator import (
    TrinityOrchestrator,
    OrganismState,
    ExecutionMode
)


class DummyWorktreeSlot:
    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path


class DummyWorktreePool:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.acquired_slots = []
        self.released_slots = []

    def acquire(self, holder_id: str, purpose: str):
        if self.should_fail:
            raise RuntimeError("Windows NTFS locked: index.lock exists in worktree")
        slot = DummyWorktreeSlot("slot_alpha", "/mock/worktree/slot_alpha")
        self.acquired_slots.append(slot.name)
        return slot

    def release(self, slot_name: str, discard: bool = False):
        self.released_slots.append((slot_name, discard))


class TestLiveOrchestration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "live_orch.db")
        self.vault_dir = os.path.join(self.temp_dir.name, "vault")
        self.orchestrator = TrinityOrchestrator(
            db_path=self.db_path,
            vault_dir=self.vault_dir
        )

    def tearDown(self):
        self.orchestrator.close()
        self.temp_dir.cleanup()

    def test_fail_closed_lock_conflict(self):
        """
        FAIL-CLOSED GUARANTEE:
        When a resource is locked by another agent, the pipeline must immediately abort
        at Stage 3 without attempting any patch modification.
        """
        target = "src/payment_gateway.py"

        # Pre-lock resource under a competitor holder
        pre_lock = self.orchestrator.adapter.execute_tool("trinity_acquire_lock", {
            "resource_path": target,
            "holder_id": "external_agent_beta"
        })
        self.assertEqual(pre_lock["status"], "ACQUIRED")

        # Now run cross-relay targeting the same locked file
        res = self.orchestrator.execute_cross_relay(
            task_title="Hotfix Payment Timeout",
            task_instruction="Add circuit breaker",
            target_file=target,
            mock_mode=True
        )

        # Must halt immediately with HALTED_LOCK_CONFLICT
        self.assertEqual(res["overall_status"], "HALTED_LOCK_CONFLICT")
        self.assertIn("자원 'src/payment_gateway.py' 점유 실패", res["briefing"])
        self.assertEqual(len(res["trace"]), 3)
        self.assertEqual(res["trace"][2]["action"], "SURGICAL_PATCH")
        self.assertEqual(res["trace"][2]["lock_status"], "LOCKED")
        self.assertIn("LOCK_CONFLICT", res["trace"][2]["error"])

    def test_dynamic_liveness_refresh(self):
        """Verify dynamic detection of Ollama service degradation."""
        # By default against local mock/offline port, check liveness
        orch = TrinityOrchestrator(
            db_path=os.path.join(self.temp_dir.name, "live_degrade.db"),
            vault_dir=self.vault_dir,
            ollama_base_url="http://127.0.0.1:59998"
        )
        try:
            state = orch.refresh_organism_liveness()
            self.assertEqual(state, OrganismState.TWO_ALIVE)
        finally:
            orch.close()

    def test_execution_modes(self):
        """Verify ExecutionMode enum and dispatch."""
        res_mock = self.orchestrator.execute_cross_relay(
            task_title="Test Mock Mode",
            task_instruction="Check enum",
            execution_mode=ExecutionMode.MOCK
        )
        self.assertEqual(res_mock["overall_status"], "SUCCESS")

        res_hybrid = self.orchestrator.execute_cross_relay(
            task_title="Test Hybrid Mode",
            task_instruction="Check enum",
            execution_mode=ExecutionMode.HYBRID
        )
        self.assertEqual(res_hybrid["overall_status"], "SUCCESS")

    def test_worktree_pool_clean_lifecycle(self):
        """Verify worktree slot acquisition and discard on success."""
        pool = DummyWorktreePool(should_fail=False)
        orch = TrinityOrchestrator(
            db_path=os.path.join(self.temp_dir.name, "wt_clean.db"),
            vault_dir=self.vault_dir,
            worktree_pool=pool
        )
        try:
            res = orch.execute_cross_relay(
                task_title="Worktree Isolation Task",
                task_instruction="Patch in slot",
                mock_mode=True
            )
            self.assertEqual(res["overall_status"], "SUCCESS")
            self.assertEqual(pool.acquired_slots, ["slot_alpha"])
            self.assertEqual(pool.released_slots, [("slot_alpha", True)])
            self.assertIn("worktree slot_alpha", res["trace"][2]["execution"]["output"])
        finally:
            orch.close()

    def test_worktree_pool_failure_triage(self):
        """Verify worktree slot acquisition failure halts with triage."""
        pool = DummyWorktreePool(should_fail=True)
        orch = TrinityOrchestrator(
            db_path=os.path.join(self.temp_dir.name, "wt_fail.db"),
            vault_dir=self.vault_dir,
            worktree_pool=pool
        )
        try:
            res = orch.execute_cross_relay(
                task_title="Failing Worktree Task",
                task_instruction="Patch in failing slot",
                mock_mode=True
            )
            self.assertEqual(res["overall_status"], "FAILED_WORKTREE")
            self.assertIn("Worktree 격리 실패", res["briefing"])
            self.assertEqual(res["trace"][2]["action"], "WORKTREE_ACQUIRE")
            self.assertEqual(res["trace"][2]["triage"]["category"], "INFRA_ENVIRONMENT")
        finally:
            orch.close()


if __name__ == "__main__":
    unittest.main()
