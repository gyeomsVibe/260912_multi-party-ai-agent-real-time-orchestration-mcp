"""
central_hub/command_verifier.py
Local command verifier that turns a check (e.g. pytest) into verification evidence.

Verification spends no model tokens: it runs a command the orchestrator owner configured
(never a model-authored string, never through a shell) and records the exit code plus
digests of its output. Only exit code 0 yields VERIFIED.
"""

import hashlib
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

TAIL_CHARS = 1500


class CommandVerifier:
    """Runs one fixed argv and returns a verification receipt."""

    def __init__(
        self,
        argv: List[str],
        cwd: Optional[str] = None,
        timeout_seconds: float = 600.0,
        which: Callable[[str], Optional[str]] = shutil.which,
    ):
        if isinstance(argv, str) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings (no shell command strings)")
        self.argv = list(argv)
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self._which = which

    def run(self, cwd: Optional[str] = None) -> Dict[str, Any]:
        """Run the check. `cwd` overrides the configured directory (e.g. an edit worktree)."""
        started = time.time()
        workdir = cwd or self.cwd
        receipt: Dict[str, Any] = {
            "verifier": "command",
            "argv": self.argv,
            "cwd": workdir,
            "exit_code": None,
            "evidence": [],
        }

        if self._which(self.argv[0]) is None:
            return self._finish(receipt, started, "NOT_CONFIGURED", f"'{self.argv[0]}' was not found")

        try:
            proc = subprocess.run(
                self.argv,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return self._finish(receipt, started, "TIMEOUT", f"did not finish within {self.timeout_seconds}s")
        except OSError as exc:
            return self._finish(receipt, started, "FAILED", f"spawn failed: {exc}")

        stdout, stderr = proc.stdout or "", proc.stderr or ""
        stdout_sha = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
        stderr_sha = hashlib.sha256(stderr.encode("utf-8")).hexdigest()
        receipt.update({
            "exit_code": proc.returncode,
            "stdout_sha256": stdout_sha,
            "stderr_sha256": stderr_sha,
            "stdout_tail": stdout[-TAIL_CHARS:],
            "stderr_tail": stderr[-TAIL_CHARS:],
        })

        if proc.returncode != 0:
            return self._finish(receipt, started, "FAILED", f"exit code {proc.returncode}")

        receipt["evidence"] = [
            f"{' '.join(self.argv)} exit 0 stdout_sha256:{stdout_sha} stderr_sha256:{stderr_sha}"
        ]
        return self._finish(receipt, started, "VERIFIED", None)

    @staticmethod
    def _finish(receipt: Dict[str, Any], started: float, status: str, error: Optional[str]) -> Dict[str, Any]:
        receipt["status"] = status
        receipt["error"] = error
        receipt["duration_ms"] = round((time.time() - started) * 1000.0, 1)
        return receipt
