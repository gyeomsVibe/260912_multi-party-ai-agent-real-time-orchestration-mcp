"""
tests/test_stdio_wrapper.py
Unit tests for HeadlessAgentRunner and stdio command execution.
"""

import unittest
import sys
from central_hub.stdio_wrapper import HeadlessAgentRunner


class TestHeadlessAgentRunner(unittest.TestCase):
    def test_mock_response_execution(self):
        runner = HeadlessAgentRunner("test_claude")
        res = runner.execute_task("Run linter", mock_response="LINT_OK: 0 issues found")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["mode"], "MOCK")
        self.assertEqual(res["output"], "LINT_OK: 0 issues found")

    def test_live_python_subprocess_success(self):
        runner = HeadlessAgentRunner("python_echo", executable=sys.executable, default_args=["-c", "import sys; print(sys.stdin.read().strip())"])
        res = runner.execute_task("ECHO_MESSAGE_PING")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["stdout"], "ECHO_MESSAGE_PING")

    def test_subprocess_timeout(self):
        # Run a sleep command that exceeds 0.5s timeout
        code = "import time; time.sleep(2.0)"
        runner = HeadlessAgentRunner("sleeper", executable=sys.executable, default_args=["-c", code])
        res = runner.execute_task("dummy", timeout_seconds=0.5)
        self.assertEqual(res["status"], "TIMEOUT")
        self.assertEqual(res["exit_code"], -1)


if __name__ == "__main__":
    unittest.main()
