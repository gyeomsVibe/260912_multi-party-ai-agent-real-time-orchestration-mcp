"""
tests/test_edit_mode.py
Edit mode: the executor edits only inside a leased worktree, the patch is preserved,
verification runs in that worktree, and the main repository is never modified.

Uses a throwaway git repository and a fake executor that really edits files, so no
model quota is spent.
"""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from central_hub.command_verifier import CommandVerifier
from central_hub.trinity_orchestrator import TrinityOrchestrator
from central_hub.worktree_pool import WorktreePool

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}

# Passes only when app.py reads exactly VALUE = 2 in the working directory.
CHECK_VALUE_2 = "import sys; sys.exit(0 if open('app.py').read().strip() == 'VALUE = 2' else 1)"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True,
                          text=True, check=True).stdout.strip()


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class EditingExecutor:
    """Fake accept-edits executor that applies `action(cwd)` and reports success."""

    def __init__(self, action, mode="accept-edits"):
        self.action = action
        self.mode = mode
        self.cwds = []

    def run(self, prompt, cwd=None):
        self.cwds.append(cwd)
        self.action(cwd)
        response = "edited"
        return {"status": "SUCCESS", "exit_code": 0, "response": response,
                "response_sha256": hashlib.sha256(response.encode()).hexdigest()}


def write_value(value):
    def action(cwd):
        with open(os.path.join(cwd, "app.py"), "w", encoding="utf-8") as f:
            f.write(f"VALUE = {value}\n")
    return action


class TestEditMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        with open(os.path.join(self.repo, "app.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.pool = WorktreePool(self.repo, os.path.join(self.tmp, "pool"), ["slot_a"])
        self.assertEqual(self.pool.initialize()["status"], "OK")
        self.orchs = []

    def tearDown(self):
        for orch in self.orchs:
            orch.close()
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        if sys.version_info >= (3, 12):
            shutil.rmtree(self.tmp, onexc=_force_remove)
        else:
            shutil.rmtree(self.tmp, onerror=_force_remove)

    def _orch(self, executor, pool="default", check=CHECK_VALUE_2):
        orch = TrinityOrchestrator(
            db_path=os.path.join(self.tmp, f"o{len(self.orchs)}.db"),
            vault_dir=os.path.join(self.tmp, "vault"),
            executor=executor,
            verifier=CommandVerifier([sys.executable, "-c", check]),
            worktree_pool=self.pool if pool == "default" else pool,
        )
        self.orchs.append(orch)
        return orch

    def _run(self, orch):
        return orch.execute_cross_relay(task_title="Edit", task_instruction="Set VALUE to 2",
                                        mock_mode=False, edit_mode=True)

    def _slot_state(self):
        return {s["slot"]: s["state"] for s in self.pool.status()}["slot_a"]

    def test_edit_verified_in_worktree_with_patch_preserved(self):
        executor = EditingExecutor(write_value(2))
        orch = self._orch(executor)
        res = self._run(orch)

        self.assertEqual(res["overall_status"], "SUCCESS")
        self.assertNotEqual(os.path.realpath(executor.cwds[0]), os.path.realpath(self.repo))
        patch = orch.adapter.vault.retrieve(res["patch_ref"])
        self.assertIn("+VALUE = 2", patch)
        self.assertEqual(res["trace"][2]["execution"]["changed_files"], ["app.py"])
        self.assertEqual(res["worktree"]["release_status"], "RELEASED")
        self.assertEqual(self._slot_state(), "FREE")

        # The main repository is never touched.
        with open(os.path.join(self.repo, "app.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "VALUE = 1\n")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_edit_that_changes_nothing_is_failed(self):
        orch = self._orch(EditingExecutor(lambda cwd: None))
        res = self._run(orch)
        ex = res["trace"][2]["execution"]
        self.assertEqual(ex["status"], "FAILED")
        self.assertIn("no changes", ex["error"])
        self.assertEqual(res["overall_status"], "FAILED")
        self.assertEqual(res["next_action"], "MANUAL_INSPECTION")
        self.assertNotIn("patch_ref", res)
        self.assertEqual(self._slot_state(), "FREE")

    def test_failed_check_still_preserves_patch(self):
        orch = self._orch(EditingExecutor(write_value(3)))
        res = self._run(orch)
        self.assertEqual(res["trace"][3]["verification_status"], "FAILED")
        self.assertEqual(res["overall_status"], "FAILED")
        self.assertIn("+VALUE = 3", orch.adapter.vault.retrieve(res["patch_ref"]))
        self.assertEqual(self._slot_state(), "FREE")

    def test_edit_mode_without_pool_is_not_configured(self):
        executor = EditingExecutor(write_value(2))
        res = self._run(self._orch(executor, pool=None))
        self.assertEqual(res["overall_status"], "NOT_CONFIGURED")
        self.assertEqual(executor.cwds, [], "executor must not run without a worktree")

    def test_plan_mode_executor_is_refused_in_edit_mode(self):
        executor = EditingExecutor(write_value(2), mode="plan")
        res = self._run(self._orch(executor))
        self.assertEqual(res["overall_status"], "NOT_CONFIGURED")
        self.assertEqual(executor.cwds, [])
        self.assertEqual(self._slot_state(), "FREE")

    def test_no_free_slot_fails_without_running_executor(self):
        self.assertEqual(self.pool.acquire("other_agent")["status"], "ACQUIRED")
        executor = EditingExecutor(write_value(2))
        res = self._run(self._orch(executor))
        self.assertEqual(res["overall_status"], "FAILED")
        self.assertIn("NO_FREE_SLOT", res["trace"][2]["execution"]["error"])
        self.assertEqual(executor.cwds, [])
        self.assertEqual({s["slot"]: s["holder"] for s in self.pool.status()}["slot_a"], "other_agent")


if __name__ == "__main__":
    unittest.main()
