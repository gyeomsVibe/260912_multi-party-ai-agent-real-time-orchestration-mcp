"""
tests/test_command_verifier.py
Verification receipt contract. Uses the current Python interpreter as the command.
"""

import sys
import unittest

from central_hub.command_verifier import CommandVerifier

PY = sys.executable


class TestCommandVerifier(unittest.TestCase):
    def test_exit_zero_is_verified_with_evidence(self):
        r = CommandVerifier([PY, "-c", "print('ok')"]).run()
        self.assertEqual(r["status"], "VERIFIED")
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(len(r["evidence"]), 1)
        self.assertIn(r["stdout_sha256"], r["evidence"][0])
        self.assertIn("ok", r["stdout_tail"])

    def test_nonzero_exit_is_failed_without_evidence(self):
        r = CommandVerifier([PY, "-c", "import sys; print('boom'); sys.exit(3)"]).run()
        self.assertEqual(r["status"], "FAILED")
        self.assertEqual(r["exit_code"], 3)
        self.assertEqual(r["evidence"], [])

    def test_timeout_is_reported(self):
        r = CommandVerifier([PY, "-c", "import time; time.sleep(5)"], timeout_seconds=0.5).run()
        self.assertEqual(r["status"], "TIMEOUT")
        self.assertEqual(r["evidence"], [])

    def test_missing_executable_is_not_configured(self):
        r = CommandVerifier(["definitely-not-a-real-binary-xyz"]).run()
        self.assertEqual(r["status"], "NOT_CONFIGURED")

    def test_shell_strings_and_empty_argv_are_rejected(self):
        for bad in ("pytest -q", [], [PY, 3]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CommandVerifier(bad)


if __name__ == "__main__":
    unittest.main()
