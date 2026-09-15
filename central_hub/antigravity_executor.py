"""
central_hub/antigravity_executor.py
Antigravity (`agy` CLI) executor that returns an evidence receipt - Trinity-ACE Protocol

Antigravity is the budget worker: it spends Google quota instead of the orchestrator's.
Every call returns a receipt; the receipt, not the process exit code, decides success.

Measured on this machine (agy 1.2.3, Windows, plain subprocess, 2026-09-15):
* `--print` returns stdout normally (the empty-stdout-under-subprocess issue reported by
  public bridges was not reproduced), but the empty-output guard stays: exit 0 with no
  response is FAILED.
* `--output-format json` returns {conversation_id, status, response, duration_seconds,
  num_turns, usage}. A one-line prompt still consumed ~19k input tokens with 0 cache
  reads, so each spawn carries a fixed overhead - batch small tasks into one call.

Safety defaults: `--sandbox` on, `--mode plan` (no edits) unless the caller opts into
`accept-edits`, and `--dangerously-skip-permissions` is never passed.
"""

import hashlib
import json
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

ALLOWED_MODES = ("plan", "accept-edits")
MAX_RESPONSE_CHARS = 4000  # keep receipts small; the digest covers the full response


class AntigravityExecutor:
    """Runs one bounded prompt through `agy --print` and returns a receipt."""

    def __init__(
        self,
        agy_binary: str = "agy",
        mode: str = "plan",
        sandbox: bool = True,
        print_timeout_seconds: int = 300,
        runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        which: Callable[[str], Optional[str]] = shutil.which,
    ):
        if mode not in ALLOWED_MODES:
            raise ValueError(f"mode must be one of {ALLOWED_MODES}, got {mode!r}")
        self.agy_binary = agy_binary
        self.mode = mode
        self.sandbox = sandbox
        self.print_timeout_seconds = print_timeout_seconds
        self._runner = runner or subprocess.run
        self._which = which

    def build_argv(self, prompt: str) -> List[str]:
        argv = [self.agy_binary]
        if self.sandbox:
            argv.append("--sandbox")
        argv += [
            "--mode", self.mode,
            "--print-timeout", f"{self.print_timeout_seconds}s",
            "--output-format", "json",
            "--print", prompt,
        ]
        return argv

    def run(self, prompt: str, cwd: Optional[str] = None) -> Dict[str, Any]:
        started = time.time()
        receipt: Dict[str, Any] = {
            "executor": "antigravity",
            "mode": self.mode,
            "sandbox": self.sandbox,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "exit_code": None,
        }

        if self._which(self.agy_binary) is None:
            return self._finish(receipt, started, "NOT_CONFIGURED",
                                f"'{self.agy_binary}' was not found on PATH")

        try:
            proc = self._runner(
                self.build_argv(prompt),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.print_timeout_seconds + 30,
            )
        except subprocess.TimeoutExpired:
            return self._finish(receipt, started, "TIMEOUT",
                                f"agy did not finish within {self.print_timeout_seconds + 30}s")
        except OSError as exc:
            return self._finish(receipt, started, "FAILED", f"spawn failed: {exc}")

        receipt["exit_code"] = proc.returncode
        receipt["stderr_tail"] = (proc.stderr or "")[-500:]
        if proc.returncode != 0:
            return self._finish(receipt, started, "FAILED", f"agy exited {proc.returncode}")

        try:
            payload = json.loads(proc.stdout or "")
        except json.JSONDecodeError:
            return self._finish(receipt, started, "FAILED", "stdout was not the expected JSON result")
        if not isinstance(payload, dict):
            return self._finish(receipt, started, "FAILED", "JSON result was not an object")

        response = payload.get("response") or ""
        receipt.update({
            "agy_status": payload.get("status"),
            "conversation_id": payload.get("conversation_id"),
            "usage": payload.get("usage") or {},
            "num_turns": payload.get("num_turns"),
            "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
            "response_chars": len(response),
            "response": response[:MAX_RESPONSE_CHARS],
        })

        if payload.get("status") != "SUCCESS":
            return self._finish(receipt, started, "FAILED", f"agy reported status {payload.get('status')!r}")
        if not response.strip():
            # Exit 0 with nothing to show is a disguised failure, not a success.
            return self._finish(receipt, started, "FAILED", "empty response despite exit code 0")
        return self._finish(receipt, started, "SUCCESS", None)

    @staticmethod
    def _finish(receipt: Dict[str, Any], started: float, status: str, error: Optional[str]) -> Dict[str, Any]:
        receipt["status"] = status
        receipt["error"] = error
        receipt["duration_ms"] = round((time.time() - started) * 1000.0, 1)
        return receipt
