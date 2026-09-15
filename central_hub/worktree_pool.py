"""
central_hub/worktree_pool.py
Static Recycled Worktree Pool - Trinity-ACE Protocol (docs/14 pitfall 3, docs/16 W-1)

docs/14 declared this pitfall solved, but no implementation existed. This module is the
implementation, with the blind spots of the original sketch corrected:

* No branch-name assumption. `git checkout --force origin/main` presumed a remote and a
  branch literally named `main` (a fresh `git init` defaults to `master`). Refs are
  resolved to a commit SHA and checked out DETACHED, which also avoids git's refusal to
  check out one branch in two worktrees at once.
* No silent data loss. Recycling runs `checkout --force` + `clean`, which destroys
  uncommitted agent work. `release()` refuses a dirty slot unless the caller exported
  the changes first or passes `discard=True` explicitly.
* No destructive command outside the pool. Every git mutation is guarded by a realpath
  containment check against the pool root, and the pool root may not overlap the repo.
* Windows file locks are an infrastructure outcome, not a crash. When an IDE or language
  server holds a handle, the slot is QUARANTINED with a triage verdict instead of being
  handed to the next agent half-cleaned.
* A leftover `index.lock` is reported, never deleted: it may belong to a live process.

Slot state is held in process memory; run one pool instance per repository (the hub).
"""

import os
import subprocess
import threading
import time
from enum import Enum
from typing import Dict, Any, List, Optional

from central_hub.triage_classifier import TriageClassifier


class SlotState(str, Enum):
    FREE = "FREE"
    BUSY = "BUSY"
    QUARANTINED = "QUARANTINED"


class WorktreePool:
    """A fixed set of git worktrees that are reset and reused instead of removed."""

    GIT_TIMEOUT_SECONDS = 120.0

    def __init__(
        self,
        repo_dir: str,
        pool_root: str,
        slot_names: List[str],
        base_ref: str = "HEAD",
        clean_ignored: bool = True,
        git_executable: str = "git",
    ):
        if not slot_names:
            raise ValueError("slot_names must not be empty")
        if len(set(slot_names)) != len(slot_names):
            raise ValueError("slot_names must be unique")
        for name in slot_names:
            if not name or os.sep in name or "/" in name or name in (".", ".."):
                raise ValueError(f"Invalid slot name: {name!r}")

        self.repo_dir = os.path.realpath(repo_dir)
        self.pool_root = os.path.realpath(pool_root)
        if self._is_within(self.pool_root, self.repo_dir) and self.pool_root == self.repo_dir:
            raise ValueError("pool_root must not be the repository itself")
        if self._is_within(self.repo_dir, self.pool_root):
            raise ValueError("pool_root must not contain the repository")

        self.base_ref = base_ref
        # Ignored files (build output, caches, *.db) leak state between tasks if kept.
        self.clean_ignored = clean_ignored
        self.git_executable = git_executable
        self._lock = threading.Lock()
        self._slots: Dict[str, Dict[str, Any]] = {
            name: {"state": SlotState.FREE, "holder": None, "ref": None, "since": None,
                   "reason": None}
            for name in slot_names
        }

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _is_within(child: str, parent: str) -> bool:
        try:
            return os.path.commonpath([child, parent]) == parent
        except ValueError:  # different drives on Windows
            return False

    def slot_path(self, name: str) -> str:
        if name not in self._slots:
            raise KeyError(f"Unknown slot: {name}")
        path = os.path.realpath(os.path.join(self.pool_root, name))
        # Defence in depth: never run a destructive command outside the pool.
        if not self._is_within(path, self.pool_root) or path == self.pool_root:
            raise ValueError(f"Slot path escapes pool root: {path}")
        if self._is_within(path, self.repo_dir) and not self._is_within(self.pool_root, self.repo_dir):
            raise ValueError(f"Slot path overlaps repository: {path}")
        return path

    def _git(self, args: List[str], cwd: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Git for Windows asks "Should I try again? (y/n)" when a file is locked; answer no.
        env["GIT_ASK_YESNO"] = "false"
        return subprocess.run(
            [self.git_executable, *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.GIT_TIMEOUT_SECONDS,
        )

    def _resolve_commit(self, ref: str) -> Optional[str]:
        res = self._git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], self.repo_dir)
        return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None

    def _index_lock_path(self, path: str) -> Optional[str]:
        res = self._git(["rev-parse", "--git-path", "index.lock"], path)
        if res.returncode != 0:
            return None
        lock = res.stdout.strip()
        return lock if os.path.isabs(lock) else os.path.join(path, lock)

    def _quarantine(self, name: str, reason: str, log: str = "") -> Dict[str, Any]:
        slot = self._slots[name]
        slot.update(state=SlotState.QUARANTINED, holder=None, reason=reason, since=time.time())
        verdict = TriageClassifier.classify(log or reason, exit_code=1)
        return {
            "status": "QUARANTINED",
            "slot": name,
            "reason": reason,
            "triage": {k: verdict[k] for k in ("category", "prescription", "code_edit_permitted")},
            "detail": log.strip()[-600:],
        }

    # ----- lifecycle -------------------------------------------------------

    def initialize(self) -> Dict[str, Any]:
        """Create any missing slots as detached worktrees. Existing slots are reused."""
        commit = self._resolve_commit(self.base_ref)
        if commit is None:
            return {"status": "ERROR",
                    "error": f"base_ref {self.base_ref!r} does not resolve to a commit "
                             "(a repository with no commits cannot host worktrees)"}

        os.makedirs(self.pool_root, exist_ok=True)
        self._git(["worktree", "prune"], self.repo_dir)

        created, reused, failed = [], [], {}
        for name in self._slots:
            path = self.slot_path(name)
            if os.path.isdir(os.path.join(path)) and self._git(
                ["rev-parse", "--is-inside-work-tree"], path
            ).stdout.strip() == "true":
                reused.append(name)
                continue
            res = self._git(["worktree", "add", "--detach", path, commit], self.repo_dir)
            if res.returncode == 0:
                created.append(name)
            else:
                failed[name] = res.stderr.strip()
                with self._lock:
                    self._quarantine(name, "worktree add failed", res.stderr)

        return {"status": "OK" if not failed else "PARTIAL", "commit": commit,
                "created": created, "reused": reused, "failed": failed}

    def _recycle(self, name: str, commit: str) -> Dict[str, Any]:
        """Reset a slot to `commit`. Caller must hold self._lock."""
        path = self.slot_path(name)

        lock = self._index_lock_path(path)
        if lock and os.path.exists(lock):
            return self._quarantine(
                name, f"index.lock present at {lock}; another git process may be running",
                f"fatal: Unable to create '{lock}': File exists.",
            )

        checkout = self._git(["checkout", "--detach", "--force", commit], path)
        if checkout.returncode != 0:
            return self._quarantine(name, "checkout failed", checkout.stderr)

        clean_args = ["clean", "-ffd" + ("x" if self.clean_ignored else "")]
        clean = self._git(clean_args, path)
        if clean.returncode != 0 or "warning: failed to remove" in clean.stderr.lower():
            return self._quarantine(name, "clean failed (file likely held open)", clean.stderr)

        leftover = self._git(["status", "--porcelain", "--ignored" if self.clean_ignored else "-uall"], path)
        if leftover.stdout.strip():
            return self._quarantine(
                name, "slot not pristine after recycle",
                "PermissionError: residual files could not be removed\n" + leftover.stdout,
            )
        return {"status": "OK"}

    def acquire(self, agent_id: str, ref: Optional[str] = None) -> Dict[str, Any]:
        """Hand a pristine slot checked out at `ref` (default base_ref) to `agent_id`."""
        commit = self._resolve_commit(ref or self.base_ref)
        if commit is None:
            return {"status": "ERROR", "error": f"ref {ref or self.base_ref!r} is not a commit"}

        with self._lock:
            quarantined_now = []
            for name, slot in self._slots.items():
                if slot["state"] is not SlotState.FREE:
                    continue
                result = self._recycle(name, commit)
                if result["status"] != "OK":
                    quarantined_now.append(result)
                    continue
                slot.update(state=SlotState.BUSY, holder=agent_id, ref=commit,
                            since=time.time(), reason=None)
                return {"status": "ACQUIRED", "slot": name, "path": self.slot_path(name),
                        "commit": commit, "holder": agent_id,
                        "quarantined_during_acquire": quarantined_now}

        return {"status": "NO_FREE_SLOT", "quarantined_during_acquire": quarantined_now}

    def export_changes(self, name: str, agent_id: str) -> Dict[str, Any]:
        """Capture the slot's work (tracked edits and new files) as a binary patch."""
        with self._lock:
            slot = self._slots[name]
            if slot["holder"] != agent_id:
                return {"status": "FORBIDDEN", "error": f"slot held by {slot['holder']}"}
            path = self.slot_path(name)
            # Intent-to-add makes untracked files visible to `git diff` without staging content.
            self._git(["add", "--intent-to-add", "--all"], path)
            diff = self._git(["diff", "--binary", slot["ref"]], path)
            files = self._git(["status", "--porcelain"], path)
            return {
                "status": "OK" if diff.returncode == 0 else "ERROR",
                "slot": name,
                "base_commit": slot["ref"],
                "patch": diff.stdout,
                "changed_files": [line[3:] for line in files.stdout.splitlines() if line.strip()],
                "error": diff.stderr.strip() or None,
            }

    def release(self, name: str, agent_id: str, discard: bool = False) -> Dict[str, Any]:
        """Return a slot to the pool. A dirty slot is refused unless `discard=True`."""
        with self._lock:
            slot = self._slots[name]
            if slot["holder"] != agent_id:
                return {"status": "FORBIDDEN", "error": f"slot held by {slot['holder']}"}

            dirty = self._git(["status", "--porcelain"], self.slot_path(name)).stdout
            if dirty.strip() and not discard:
                return {
                    "status": "DIRTY",
                    "slot": name,
                    "changed_files": [line[3:] for line in dirty.splitlines() if line.strip()],
                    "instruction": "Call export_changes() first, or release(discard=True) "
                                   "to destroy this work.",
                }

            result = self._recycle(name, slot["ref"])
            if result["status"] != "OK":
                return result
            slot.update(state=SlotState.FREE, holder=None, since=time.time(), reason=None)
            return {"status": "RELEASED", "slot": name, "discarded": bool(dirty.strip())}

    def reinstate(self, name: str) -> Dict[str, Any]:
        """Retry a quarantined slot (e.g. after the IDE released its file handles)."""
        with self._lock:
            slot = self._slots[name]
            if slot["state"] is not SlotState.QUARANTINED:
                return {"status": "NOT_QUARANTINED", "slot": name}
            commit = slot["ref"] or self._resolve_commit(self.base_ref)
            result = self._recycle(name, commit)
            if result["status"] != "OK":
                return result
            slot.update(state=SlotState.FREE, holder=None, reason=None, since=time.time())
            return {"status": "REINSTATED", "slot": name}

    def status(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"slot": n, "state": s["state"].value, "holder": s["holder"],
                 "commit": s["ref"], "since": s["since"], "reason": s["reason"]}
                for n, s in self._slots.items()
            ]
