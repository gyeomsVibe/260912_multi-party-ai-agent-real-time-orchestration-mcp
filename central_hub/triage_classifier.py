"""
central_hub/triage_classifier.py
3-Tier Triage Engine - Trinity-ACE Protocol

Distinguishes environmental/infrastructure failures (port conflict, DB lock, network
timeout) from specification conflicts and actual source code defects, so that models do
not refactor working code when the environment is at fault.

docs/16 correction C-3 applied. The original engine inverted its own verdicts:
* first-match-wins with INFRA scanned first meant one incidental word ("timed out")
  anywhere in a pytest log outranked an explicit AssertionError and BLOCKED the repair;
* the fallback was CODE_DEFECT with code_edit_permitted=True, so an unrecognised infra
  failure was routed straight to "go edit the code" - the exact loop this module exists
  to prevent.

The engine now scores every category, weights the tail of the log (where the real
failure lives), lets a test-runner assertion override incidental infra vocabulary, and
fails safe to MANUAL_INSPECTION.
"""

import re
from enum import Enum
from typing import Dict, Any, List, Tuple


class ErrorCategory(str, Enum):
    INFRA_ENVIRONMENT = "INFRA_ENVIRONMENT"
    SPEC_CONFLICT = "SPEC_CONFLICT"
    CODE_DEFECT = "CODE_DEFECT"
    UNKNOWN = "UNKNOWN"


class TriagePrescription(str, Enum):
    HALT_AND_RELEASE_RESOURCE = "HALT_AND_RELEASE_RESOURCE"  # Do not modify code
    SPECIFICATION_RECONCILIATION = "SPECIFICATION_RECONCILIATION"  # Reconcile API/schema
    ROUTE_TO_REPAIR_AGENT = "ROUTE_TO_REPAIR_AGENT"  # Safe to modify source code
    MANUAL_INSPECTION = "MANUAL_INSPECTION"  # Ambiguous; a human decides


#: Signals that the test runner reached and evaluated the code under test. If one of
#: these is present the environment demonstrably worked, so incidental infra vocabulary
#: in the same log must not veto a code-defect verdict.
#: Only unambiguous runner output belongs here. A loose `\d+ failed` also matched
#: "Retry 3 failed: Connection refused" and re-inverted the verdict (docs/16 T-1).
_CODE_OVERRIDE_PATTERNS = [
    r"(?i)\bAssertionError\b",
    r"(?i)FAILED \((?:failures|errors)=\d+\)",
    r"(?i)^\s*E\s+assert\b",
    r"(?im)^=+ .*\b\d+ failed\b.* =+$",  # pytest summary banner only
]

#: Weak evidence: words that legitimately appear inside application-level failures and
#: therefore score lower than a hard infrastructure signature.
_WEAK_INFRA_PATTERNS = {"timed out", "timeout expired", "RequestTimeout"}

#: Number of trailing lines treated as the failure site and double-weighted.
_TAIL_LINES = 20
_TAIL_WEIGHT = 2
_STRONG_WEIGHT = 3
_WEAK_WEIGHT = 1


class TriageClassifier:
    """Classifies runtime and test failures into actionable categories."""

    INFRA_PATTERNS: List[Tuple[str, str]] = [
        (r"(?i)address already in use|EADDRINUSE", "Port conflict / EADDRINUSE"),
        (r"(?i)Win32 Error 32|Sharing Violation|file is locked|WinError 32", "Windows NTFS file lock"),
        (r"(?i)OperationalError: database is locked", "SQLite database lock contention"),
        (r"(?i)connection refused|ECONNREFUSED", "Target service / daemon not running"),
        (r"(?i)no space left on device|ENOSPC", "Disk full"),
        (r"(?i)out of memory|CUDA out of memory|MemoryError", "Out of memory / OOM"),
        (r"(?i)broken pipe|EPIPE", "Broken communication pipe"),
        (r"(?i)DLL load failed", "OS environment / missing system library"),
        (r"(?i)ModuleNotFoundError: No module named", "Missing dependency in environment"),
        (r"(?i)Access is denied|PermissionError", "Filesystem permission denied"),
        # Observed on Git for Windows 2.55 when an editor holds a handle: git reports
        # "Invalid argument", not "Permission denied" (docs/16 W-1 measurement).
        (r"(?i)failed to remove .+: (?:Invalid argument|Permission denied|Device or resource busy)",
         "Git could not remove a file held open (Windows handle lock)"),
        # Weak signals: real but frequently incidental inside application failures.
        (r"(?i)timeout expired|timed out|RequestTimeout", "Process or network timeout"),
    ]

    SPEC_PATTERNS: List[Tuple[str, str]] = [
        (r"(?i)TypeError: .* missing \d+ required positional argument", "Missing required argument"),
        (r"(?i)TypeError: .* unexpected keyword argument", "Unexpected keyword argument"),
        (r"(?i)ValidationError", "JSON/Pydantic schema validation error"),
        (r"(?i)KeyError: .*(?:missing )?required field", "Missing required field"),
        (r"(?i)schema mismatch|invalid parameter", "Parameter or contract mismatch"),
        (r"(?i)AttributeError: '.*' object has no attribute", "Interface attribute mismatch"),
    ]

    CODE_PATTERNS: List[Tuple[str, str]] = [
        (r"(?i)\bAssertionError\b", "Test assertion failure / functional logic defect"),
        (r"(?i)SyntaxError: invalid syntax", "Source code syntax error"),
        (r"(?i)IndentationError", "Indentation defect"),
        (r"(?i)ZeroDivisionError", "Division by zero defect"),
        (r"(?i)IndexError: list index out of range", "Index out of bounds defect"),
        (r"(?i)ValueError: math domain error", "Domain mathematical error"),
        (r"(?im)FAILED \((?:failures|errors)=\d+\)|^=+ .*\b\d+ failed\b.* =+$",
         "Unit/integration test failure"),
        (r"(?i)UnboundLocalError|NameError: name '.*' is not defined", "Undefined symbol defect"),
    ]

    _PRESCRIPTIONS = {
        ErrorCategory.INFRA_ENVIRONMENT: (
            TriagePrescription.HALT_AND_RELEASE_RESOURCE,
            False,
            "DO NOT MODIFY SOURCE CODE. Release the system resource, wait for the lock, "
            "or verify the environment daemon.",
        ),
        ErrorCategory.SPEC_CONFLICT: (
            TriagePrescription.SPECIFICATION_RECONCILIATION,
            True,
            "Reconcile the API contract, argument list, or schema definition.",
        ),
        ErrorCategory.CODE_DEFECT: (
            TriagePrescription.ROUTE_TO_REPAIR_AGENT,
            True,
            "Route to the repair agent with a minimal RTK traceback vector.",
        ),
        ErrorCategory.UNKNOWN: (
            TriagePrescription.MANUAL_INSPECTION,
            False,
            "Unclassified failure. Do not edit source code; escalate for human inspection.",
        ),
    }

    @staticmethod
    def _weight_for(pattern: str, reason: str) -> int:
        return _WEAK_WEIGHT if any(w in pattern for w in _WEAK_INFRA_PATTERNS) else _STRONG_WEIGHT

    @classmethod
    def _score(cls, patterns, head: str, tail: str) -> Tuple[int, str, str]:
        """Returns (score, best_reason, best_match) across head and tail sections."""
        total = 0
        best_reason = ""
        best_match = ""
        best_weight = -1
        for pattern, reason in patterns:
            for section, multiplier in ((tail, _TAIL_WEIGHT), (head, 1)):
                m = re.search(pattern, section)
                if not m:
                    continue
                weight = cls._weight_for(pattern, reason) * multiplier
                total += weight
                if weight > best_weight:
                    best_weight, best_reason, best_match = weight, reason, m.group(0)
                break  # count each pattern once, at its highest-weight location
        return total, best_reason, best_match

    @classmethod
    def _build(cls, category: ErrorCategory, reason: str, match: str, scores: Dict[str, int],
               note: str = "") -> Dict[str, Any]:
        prescription, edit_permitted, instruction = cls._PRESCRIPTIONS[category]
        return {
            "category": category.value,
            "prescription": prescription.value,
            "reason": reason,
            "matched_pattern": match or None,
            "code_edit_permitted": edit_permitted,
            "instruction": instruction,
            "scores": scores,
            "note": note,
        }

    @classmethod
    def classify(cls, error_log: str, exit_code: int = 1) -> Dict[str, Any]:
        """
        Classify an error log into INFRA_ENVIRONMENT, SPEC_CONFLICT, CODE_DEFECT, or
        UNKNOWN. An unrecognised failure fails safe: code edits are NOT permitted.

        `exit_code` is advisory only - a log is classified whenever one is supplied,
        because test runners and wrappers routinely report 0 alongside real failures.
        """
        zero_scores = {c.value: 0 for c in ErrorCategory if c is not ErrorCategory.UNKNOWN}

        if not error_log or not error_log.strip():
            return cls._build(ErrorCategory.UNKNOWN, "No error log supplied", "", zero_scores)

        lines = error_log.splitlines()
        tail = "\n".join(lines[-_TAIL_LINES:])
        head = "\n".join(lines[:-_TAIL_LINES]) if len(lines) > _TAIL_LINES else ""

        infra = cls._score(cls.INFRA_PATTERNS, head, tail)
        spec = cls._score(cls.SPEC_PATTERNS, head, tail)
        code = cls._score(cls.CODE_PATTERNS, head, tail)

        scores = {
            ErrorCategory.INFRA_ENVIRONMENT.value: infra[0],
            ErrorCategory.SPEC_CONFLICT.value: spec[0],
            ErrorCategory.CODE_DEFECT.value: code[0],
        }

        # Override: the runner reached the code, so the environment worked - unless a
        # STRONG infrastructure signature sits in the failure tail, in which case the
        # assertion is most likely a symptom of the broken environment.
        strong_infra_in_tail = any(
            re.search(p, tail)
            for p, _ in cls.INFRA_PATTERNS
            if not any(w in p for w in _WEAK_INFRA_PATTERNS)
        )
        if not strong_infra_in_tail and any(
            re.search(p, error_log, re.MULTILINE) for p in _CODE_OVERRIDE_PATTERNS
        ):
            reason = code[1] or "Test assertion failure / functional logic defect"
            return cls._build(
                ErrorCategory.CODE_DEFECT, reason, code[2], scores,
                note="Test-runner assertion present: incidental infrastructure vocabulary ignored.",
            )

        if strong_infra_in_tail and any(
            re.search(p, error_log, re.MULTILINE) for p in _CODE_OVERRIDE_PATTERNS
        ):
            return cls._build(
                ErrorCategory.INFRA_ENVIRONMENT, infra[1], infra[2], scores,
                note="Assertion present but a strong infrastructure signature is in the "
                     "failure tail; treating the assertion as an environment symptom.",
            )

        if max(scores.values()) == 0:
            return cls._build(
                ErrorCategory.UNKNOWN,
                "No known infrastructure, contract, or code-defect signature matched",
                "", scores,
                note="Fail-safe: unclassified failures must not authorise source edits.",
            )

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            return cls._build(
                ErrorCategory.UNKNOWN,
                f"Ambiguous signature: {ranked[0][0]} and {ranked[1][0]} scored equally",
                "", scores,
                note="Tie between categories; escalated rather than guessed.",
            )

        winner = ErrorCategory(ranked[0][0])
        detail = {
            ErrorCategory.INFRA_ENVIRONMENT: infra,
            ErrorCategory.SPEC_CONFLICT: spec,
            ErrorCategory.CODE_DEFECT: code,
        }[winner]
        return cls._build(winner, detail[1], detail[2], scores)


if __name__ == "__main__":
    for sample in [
        "sqlite3.OperationalError: database is locked",
        "E   AssertionError: assert 0 == 1\nE   payment gateway timed out",
        "RuntimeError: something weird happened",
    ]:
        r = TriageClassifier.classify(sample, exit_code=1)
        print(f"{r['category']:18} edit={r['code_edit_permitted']!s:5} scores={r['scores']}")
