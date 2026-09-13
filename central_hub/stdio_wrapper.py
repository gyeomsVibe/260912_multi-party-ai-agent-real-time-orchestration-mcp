"""
central_hub/stdio_wrapper.py
Headless Stdio Subprocess Wrapper - Trinity-ACE Protocol

Resolves Pitfall 1:
Claude Code and Codex CLI lack autonomous background SSE listeners.
This module actively spawns headless subprocesses and controls stdin/stdout streams.
"""

import subprocess
import threading
import time
import json
from typing import Dict, Any, Optional, List


class HeadlessAgentRunner:
    """
    Manages active invocation of CLI-based AI agents via headless Stdio.

    docs/16 correction A-3: every spawn starts a fresh CLI session, and a fresh session
    pays the premium cache-WRITE rate rather than the cheap cache-read rate. Unbounded
    spawning therefore burns subscription quota fastest exactly when the event bus is
    busiest. A semaphore caps concurrent spawns; callers should also batch bus events
    rather than spawning once per card.
    """

    DEFAULT_MAX_CONCURRENCY = 1

    def __init__(
        self,
        agent_name: str,
        executable: str = "python",
        default_args: Optional[List[str]] = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ):
        self.agent_name = agent_name
        self.executable = executable
        self.default_args = default_args or []
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.max_concurrency = max_concurrency
        self._slots = threading.Semaphore(max_concurrency)

    def _run_subprocess(self, prompt: str, timeout_seconds: float) -> Dict[str, Any]:
        """Spawn the CLI once and collect its result. Overridden in tests."""
        cmd = [self.executable] + self.default_args
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            # Drain the pipes after killing, otherwise the Windows handles leak and the
            # child can block forever on a full stdout buffer.
            try:
                process.communicate(timeout=5)
            except Exception:
                pass
            return {
                "status": "TIMEOUT",
                "exit_code": -1,
                "error": f"Task timed out after {timeout_seconds} seconds",
            }
        return {
            "status": "SUCCESS" if process.returncode == 0 else "ERROR",
            "exit_code": process.returncode,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        }

    def execute_task(
        self,
        prompt: str,
        timeout_seconds: float = 60.0,
        mock_response: Optional[str] = None,
        acquire_timeout: float = 300.0,
    ) -> Dict[str, Any]:
        """
        Execute a task against the agent CLI.
        If mock_response is supplied, simulates immediate green execution for testing.
        """
        if mock_response is not None:
            return {
                "agent_name": self.agent_name,
                "status": "SUCCESS",
                "exit_code": 0,
                "output": mock_response,
                "duration_ms": 15.0,
                "mode": "MOCK"
            }

        if not self._slots.acquire(timeout=acquire_timeout):
            return {
                "agent_name": self.agent_name,
                "status": "THROTTLED",
                "exit_code": -3,
                "error": f"No spawn slot within {acquire_timeout}s (cap={self.max_concurrency})",
                "mode": "LIVE",
            }

        start_time = time.time()
        try:
            result = self._run_subprocess(prompt, timeout_seconds)
        except Exception as e:
            result = {"status": "EXCEPTION", "exit_code": -2, "error": str(e)}
        finally:
            self._slots.release()

        result.update({
            "agent_name": self.agent_name,
            "duration_ms": (time.time() - start_time) * 1000.0,
            "mode": "LIVE",
        })
        return result


if __name__ == "__main__":
    runner = HeadlessAgentRunner("mock_claude")
    res = runner.execute_task("Write unit test", mock_response="PASS: 8 tests passed in 0.4s")
    print("Execution result:", json.dumps(res, indent=2))
