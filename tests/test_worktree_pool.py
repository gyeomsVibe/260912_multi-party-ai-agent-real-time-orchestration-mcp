"""
tests/test_worktree_pool.py
Tests for the Static Recycled Worktree Pool (docs/16 W-1).

Every test builds a throwaway git repository in a temp directory; the project's own
repository is never touched.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from central_hub.worktree_pool import WorktreePool

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True,
                          text=True, check=True).stdout.strip()


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class PoolTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")  # default branch deliberately left as git chooses
        with open(os.path.join(self.repo, "app.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 1\n")
        with open(os.path.join(self.repo, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("*.db\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.pool_root = os.path.join(self.tmp, "pool")
        self.pool = WorktreePool(self.repo, self.pool_root, ["worker_code", "antigravity"])
        self.assertEqual(self.pool.initialize()["status"], "OK")

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, onexc=_force_remove) if sys.version_info >= (3, 12) \
            else shutil.rmtree(self.tmp, onerror=_force_remove)


class TestInitialization(PoolTestBase):
    def test_slots_are_detached_worktrees_without_branch_assumption(self):
        listing = git(self.repo, "worktree", "list")
        self.assertIn("worker_code", listing)
        self.assertIn("(detached HEAD)", listing)

    def test_initialize_is_idempotent(self):
        res = self.pool.initialize()
        self.assertEqual(res["status"], "OK")
        self.assertEqual(sorted(res["reused"]), ["antigravity", "worker_code"])
        self.assertEqual(res["created"], [])

    def test_repository_without_commits_is_reported_not_crashed(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        git(empty, "init", "-q")
        pool = WorktreePool(empty, os.path.join(self.tmp, "p2"), ["s"])
        res = pool.initialize()
        self.assertEqual(res["status"], "ERROR")
        self.assertIn("no commits", res["error"])


class TestSafetyGuards(unittest.TestCase):
    def test_pool_root_equal_to_repo_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                WorktreePool(d, d, ["s"])

    def test_pool_root_containing_repo_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                WorktreePool(os.path.join(d, "repo"), d, ["s"])

    def test_traversal_slot_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ["..", "../escape", "a/b", ""]:
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    WorktreePool(os.path.join(d, "r"), os.path.join(d, "p"), [bad])


class TestAcquireRelease(PoolTestBase):
    def test_distinct_slots_and_exhaustion(self):
        a = self.pool.acquire("worker")
        b = self.pool.acquire("antigravity")
        self.assertEqual(a["status"], "ACQUIRED")
        self.assertEqual(b["status"], "ACQUIRED")
        self.assertNotEqual(a["slot"], b["slot"])
        self.assertEqual(self.pool.acquire("codex")["status"], "NO_FREE_SLOT")

    def test_dirty_release_is_refused_to_protect_work(self):
        a = self.pool.acquire("worker")
        with open(os.path.join(a["path"], "app.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 2\n")
        res = self.pool.release(a["slot"], "worker")
        self.assertEqual(res["status"], "DIRTY")
        self.assertIn("app.py", res["changed_files"])
        # Work must still be there
        with open(os.path.join(a["path"], "app.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "VALUE = 2\n")

    def test_export_then_discard_recycles_to_pristine(self):
        a = self.pool.acquire("worker")
        path = a["path"]
        with open(os.path.join(path, "app.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 2\n")
        with open(os.path.join(path, "new_module.py"), "w", encoding="utf-8") as f:
            f.write("NEW = True\n")
        with open(os.path.join(path, "cache.db"), "w", encoding="utf-8") as f:
            f.write("ignored state\n")

        export = self.pool.export_changes(a["slot"], "worker")
        self.assertEqual(export["status"], "OK")
        self.assertIn("VALUE = 2", export["patch"])
        self.assertIn("new_module.py", export["patch"])

        self.assertEqual(self.pool.release(a["slot"], "worker", discard=True)["status"], "RELEASED")

        b = self.pool.acquire("codex")
        self.assertEqual(b["slot"], a["slot"])
        with open(os.path.join(b["path"], "app.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "VALUE = 1\n")
        self.assertFalse(os.path.exists(os.path.join(b["path"], "new_module.py")))
        self.assertFalse(os.path.exists(os.path.join(b["path"], "cache.db")),
                         "ignored files must not leak between tasks")

    def test_exported_patch_applies_to_main_repo(self):
        a = self.pool.acquire("worker")
        with open(os.path.join(a["path"], "app.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 42\n")
        patch = self.pool.export_changes(a["slot"], "worker")["patch"]
        patch_file = os.path.join(self.tmp, "w.patch")
        with open(patch_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(patch)
        git(self.repo, "apply", "--check", patch_file)

    def test_wrong_agent_cannot_release_or_export(self):
        a = self.pool.acquire("worker")
        self.assertEqual(self.pool.release(a["slot"], "intruder")["status"], "FORBIDDEN")
        self.assertEqual(self.pool.export_changes(a["slot"], "intruder")["status"], "FORBIDDEN")

    def test_acquire_at_newer_commit(self):
        with open(os.path.join(self.repo, "app.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 3\n")
        git(self.repo, "commit", "-q", "-am", "bump")
        new_commit = git(self.repo, "rev-parse", "HEAD")
        a = self.pool.acquire("worker", ref=new_commit)
        self.assertEqual(a["commit"], new_commit)
        with open(os.path.join(a["path"], "app.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "VALUE = 3\n")

    def test_unknown_ref_is_an_error(self):
        self.assertEqual(self.pool.acquire("worker", ref="no-such-ref")["status"], "ERROR")


class TestQuarantine(PoolTestBase):
    def test_stale_index_lock_quarantines_without_deleting_it(self):
        path = self.pool.slot_path("worker_code")
        lock = self.pool._index_lock_path(path)
        open(lock, "w").close()

        a = self.pool.acquire("worker")
        self.assertEqual(a["slot"], "antigravity", "locked slot must be skipped")
        states = {s["slot"]: s["state"] for s in self.pool.status()}
        self.assertEqual(states["worker_code"], "QUARANTINED")
        self.assertTrue(os.path.exists(lock), "index.lock may belong to a live process")

        os.remove(lock)
        self.assertEqual(self.pool.reinstate("worker_code")["status"], "REINSTATED")

    @unittest.skipUnless(os.name == "nt", "NTFS open-handle semantics are Windows-specific")
    def test_file_held_open_quarantines_slot_on_windows(self):
        a = self.pool.acquire("worker")
        held_path = os.path.join(a["path"], "held_by_ide.txt")
        handle = open(held_path, "w", encoding="utf-8")
        handle.write("an editor keeps this open")
        handle.flush()
        try:
            res = self.pool.release(a["slot"], "worker", discard=True)
            self.assertEqual(res["status"], "QUARANTINED")
            self.assertEqual(res["triage"]["category"], "INFRA_ENVIRONMENT")
            self.assertFalse(res["triage"]["code_edit_permitted"])
        finally:
            handle.close()
        self.assertEqual(self.pool.reinstate(a["slot"])["status"], "REINSTATED")


if __name__ == "__main__":
    unittest.main()
