"""
tests/test_antigravity_executor.py
Receipt contract for the Antigravity executor.

Default tests use a fake runner and spend no quota. Set TRINITY_LIVE_AGY=1 to run the
single live smoke test against the real `agy` CLI.
"""

import json
import os
import subprocess
import unittest

from central_hub.antigravity_executor import AntigravityExecutor


def fake_runner(returncode=0, stdout="", stderr="", raises=None):
    calls = []

    def run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    run.calls = calls
    return run


def agy_json(status="SUCCESS", response="RECEIPT_OK\n"):
    return json.dumps({
        "conversation_id": "conv-1", "status": status, "response": response,
        "duration_seconds": 1.0, "num_turns": 1,
        "usage": {"input_tokens": 19341, "output_tokens": 915, "total_tokens": 20256},
    })


def executor(runner, **kwargs):
    return AntigravityExecutor(runner=runner, which=lambda _: "C:/agy.exe", **kwargs)


class TestReceiptSemantics(unittest.TestCase):
    def test_success_receipt_carries_digest_and_usage(self):
        r = executor(fake_runner(stdout=agy_json())).run("Reply with exactly: RECEIPT_OK")
        self.assertEqual(r["status"], "SUCCESS")
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(len(r["response_sha256"]), 64)
        self.assertEqual(r["usage"]["input_tokens"], 19341)
        self.assertEqual(r["conversation_id"], "conv-1")

    def test_exit_zero_with_empty_response_is_failed(self):
        r = executor(fake_runner(stdout=agy_json(response="   "))).run("p")
        self.assertEqual(r["status"], "FAILED")
        self.assertIn("empty response", r["error"])

    def test_exit_zero_with_non_json_stdout_is_failed(self):
        r = executor(fake_runner(stdout="")).run("p")
        self.assertEqual(r["status"], "FAILED")

    def test_agy_reported_failure_is_failed(self):
        r = executor(fake_runner(stdout=agy_json(status="ERROR"))).run("p")
        self.assertEqual(r["status"], "FAILED")

    def test_nonzero_exit_is_failed(self):
        r = executor(fake_runner(returncode=2, stderr="boom")).run("p")
        self.assertEqual(r["status"], "FAILED")
        self.assertEqual(r["exit_code"], 2)

    def test_timeout_is_reported(self):
        r = executor(fake_runner(raises=subprocess.TimeoutExpired("agy", 1))).run("p")
        self.assertEqual(r["status"], "TIMEOUT")

    def test_missing_binary_is_not_configured(self):
        runner = fake_runner(stdout=agy_json())
        r = AntigravityExecutor(runner=runner, which=lambda _: None).run("p")
        self.assertEqual(r["status"], "NOT_CONFIGURED")
        self.assertEqual(runner.calls, [], "must not spawn when the binary is absent")


class TestSafetyDefaults(unittest.TestCase):
    def test_default_argv_is_sandboxed_plan_mode_json(self):
        runner = fake_runner(stdout=agy_json())
        executor(runner).run("p")
        argv = runner.calls[0]["argv"]
        self.assertIn("--sandbox", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(runner.calls[0]["stdin"], subprocess.DEVNULL)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            AntigravityExecutor(mode="yolo")


@unittest.skipUnless(os.environ.get("TRINITY_LIVE_AGY") == "1", "live agy smoke test (spends quota)")
class TestLiveAgy(unittest.TestCase):
    def test_live_receipt(self):
        r = AntigravityExecutor(print_timeout_seconds=120).run("Reply with exactly: RECEIPT_OK")
        self.assertEqual(r["status"], "SUCCESS", r)
        self.assertIn("RECEIPT_OK", r["response"])


if __name__ == "__main__":
    unittest.main()
