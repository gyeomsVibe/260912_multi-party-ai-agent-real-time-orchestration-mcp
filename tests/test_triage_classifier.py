"""
tests/test_triage_classifier.py
Unit tests for 3-Tier TriageClassifier engine.
"""

import unittest
from central_hub.triage_classifier import (
    TriageClassifier,
    ErrorCategory,
    TriagePrescription
)


class TestTriageClassifier(unittest.TestCase):
    def test_infra_sqlite_locked(self):
        log = "sqlite3.OperationalError: database is locked"
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.INFRA_ENVIRONMENT.value)
        self.assertEqual(res["prescription"], TriagePrescription.HALT_AND_RELEASE_RESOURCE.value)
        self.assertFalse(res["code_edit_permitted"])

    def test_infra_windows_sharing_violation(self):
        log = "PermissionError: [WinError 32] The process cannot access the file because it is being used by another process"
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.INFRA_ENVIRONMENT.value)
        self.assertEqual(res["prescription"], TriagePrescription.HALT_AND_RELEASE_RESOURCE.value)
        self.assertFalse(res["code_edit_permitted"])

    def test_infra_port_conflict(self):
        log = "OSError: [Errno 98] Address already in use: ('127.0.0.1', 8080)"
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.INFRA_ENVIRONMENT.value)
        self.assertEqual(res["prescription"], TriagePrescription.HALT_AND_RELEASE_RESOURCE.value)
        self.assertFalse(res["code_edit_permitted"])

    def test_spec_conflict_missing_argument(self):
        log = "TypeError: dispatch_task() missing 1 required positional argument: 'target_id'"
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.SPEC_CONFLICT.value)
        self.assertEqual(res["prescription"], TriagePrescription.SPECIFICATION_RECONCILIATION.value)
        self.assertTrue(res["code_edit_permitted"])

    def test_code_defect_assertion_error(self):
        log = "AssertionError: Expected status 200 but got 500"
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.CODE_DEFECT.value)
        self.assertEqual(res["prescription"], TriagePrescription.ROUTE_TO_REPAIR_AGENT.value)
        self.assertTrue(res["code_edit_permitted"])

    def test_code_defect_zero_division(self):
        log = "ZeroDivisionError: division by zero"
        res = TriageClassifier.classify(log, exit_code=1)
        self.assertEqual(res["category"], ErrorCategory.CODE_DEFECT.value)
        self.assertEqual(res["prescription"], TriagePrescription.ROUTE_TO_REPAIR_AGENT.value)
        self.assertTrue(res["code_edit_permitted"])

    def test_clean_exit_code_zero(self):
        res = TriageClassifier.classify("", exit_code=0)
        self.assertEqual(res["category"], ErrorCategory.UNKNOWN.value)
        self.assertFalse(res["code_edit_permitted"])


if __name__ == "__main__":
    unittest.main()
