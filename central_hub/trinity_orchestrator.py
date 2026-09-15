"""
central_hub/trinity_orchestrator.py
End-to-End Cross-Relay Pipeline Orchestrator - Trinity-ACE Protocol

Coordinates the 4-Stage Operating Lifecycle:
1. Codex (Planner): Task card emission with idempotency.
2. Ollama (Preprocess): AST skeleton extraction at zero token cost.
3. Antigravity (Executor): Bounded task execution with an evidence receipt.
4. Local Verifier: A configured local check (e.g. pytest) producing verification evidence.

Result semantics (P0 "no disguised success"):
* SUCCESS comes only from `derive_overall_status`, and requires an executor receipt with
  exit code 0 plus verification evidence. Mock runs are SIMULATED, live runs without an
  executor are NOT_CONFIGURED, and results without verification are UNVERIFIED.
* A lock that is not ACQUIRED halts the pipeline (fail-closed). A lock this pipeline does
  not own is never released.
* Liveness starts UNKNOWN. A healthy local Ollama never promotes the organism to 3-ALIVE.

Edit mode (`edit_mode=True`):
* Requires a WorktreePool and an executor in `accept-edits` mode; otherwise NOT_CONFIGURED.
* The executor edits only inside a leased worktree slot. Its changes are exported as a
  patch and stored as an artifact; producing no changes is FAILED, not success.
* Verification runs inside the same slot. The slot is reset and returned only after the
  patch is preserved; otherwise it stays held for manual recovery.
* The patch is never applied to the main repository automatically.

Failure routing:
* When execution or verification ends FAILED or TIMEOUT, the failure text is triaged and
  the prescription becomes `next_action`: environment faults halt without code edits,
  code defects route to repair, and unclassified failures go to manual inspection.
"""

import sys
import os
import time
import json
import uuid
from enum import Enum
from typing import Dict, Any, Optional, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from central_hub.hub_daemon import CentralHubDaemon
from central_hub.mcp_server_adapter import MCPServerAdapter
from central_hub.triage_classifier import TriageClassifier, ErrorCategory
from central_hub.stdio_wrapper import HeadlessAgentRunner
from harness.ollama_worker import OllamaWorker


class OrganismState(str, Enum):
    UNKNOWN = "UNKNOWN"      # No liveness evidence collected for the cloud tools
    THREE_ALIVE = "3-ALIVE"  # All tools + Ollama active (requires per-tool evidence)
    TWO_ALIVE = "2-ALIVE"    # One tool degraded; dynamic failover active
    ONE_ALIVE = "1-ALIVE"    # Emergency isolated defense


FAILURE_STATUSES = ("FAILED", "TIMEOUT")


def derive_overall_status(exec_res: Dict[str, Any], verification: Dict[str, Any]) -> str:
    """
    Derive the pipeline verdict from evidence. SUCCESS requires a real executor result
    (status SUCCESS, exit code 0) AND a VERIFIED verification that carries evidence.
    """
    exec_status = exec_res.get("status")
    if exec_status == "NOT_CONFIGURED":
        return "NOT_CONFIGURED"
    if exec_status == "SIMULATED" or verification.get("status") == "SIMULATED":
        return "SIMULATED"
    if exec_status != "SUCCESS" or exec_res.get("exit_code") != 0:
        return "FAILED"
    if verification.get("status") in FAILURE_STATUSES:
        # A failed check is a failure, not merely missing evidence.
        return "FAILED"
    if verification.get("status") == "VERIFIED" and verification.get("evidence"):
        return "SUCCESS"
    return "UNVERIFIED"


def triage_receipt(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a failed executor or verifier receipt from the text it produced."""
    parts = [receipt.get(k) for k in ("error", "response", "stdout_tail", "stderr_tail")]
    log = "\n".join(p for p in parts if isinstance(p, str) and p.strip())
    if receipt.get("status") == "TIMEOUT" and "timed out" not in log.lower():
        log = f"{log}\nProcess timed out".strip()
    verdict = TriageClassifier.classify(log, exit_code=receipt.get("exit_code") or 1)
    return {k: verdict[k] for k in ("category", "prescription", "code_edit_permitted", "reason")}


_BRIEFING_BY_STATUS = {
    "SIMULATED": "모의 실행 — 실제 실행·검증은 수행되지 않았습니다",
    "NOT_CONFIGURED": "실행기 미연결 — 실제 실행이 수행되지 않았습니다",
    "UNVERIFIED": "검증 증거 없음 — 결과를 확인할 수 없습니다",
    "FAILED": "실패",
    "SUCCESS": "실행과 검증 증거 확인",
}


class TrinityOrchestrator:
    """The central coordinator executing cross-relay agentic workflows."""

    EXECUTOR_HOLDER_ID = "executor"

    def __init__(
        self,
        db_path: str = ":memory:",
        vault_dir: Optional[str] = None,
        ollama_base_url: str = "http://localhost:11434",
        executor: Optional[Any] = None,
        verifier: Optional[Any] = None,
        worktree_pool: Optional[Any] = None
    ):
        self.db_path = db_path
        self.adapter = MCPServerAdapter(db_path=db_path, vault_dir=vault_dir)
        self.ollama = OllamaWorker(base_url=ollama_base_url, timeout_seconds=2.0)
        self.mock_runner = HeadlessAgentRunner("mock_executor")
        # Real executor (e.g. AntigravityExecutor). Without one, live runs are NOT_CONFIGURED.
        self.executor = executor
        # Local check (e.g. CommandVerifier running pytest). Without one, results stay UNVERIFIED.
        self.verifier = verifier
        # Real WorktreePool. Edit mode is refused without one.
        self.worktree_pool = worktree_pool
        self.state = OrganismState.UNKNOWN

    def _store_receipt(self, receipt: Dict[str, Any]) -> Dict[str, Any]:
        return self.adapter.execute_tool("trinity_store_artifact", {
            "content": json.dumps(receipt, ensure_ascii=False),
            "artifact_type": "json",
        })

    @staticmethod
    def _summarize_receipt(receipt: Dict[str, Any], stored: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": receipt.get("status"),
            "exit_code": receipt.get("exit_code"),
            "error": receipt.get("error"),
            "response_sha256": receipt.get("response_sha256"),
            "usage": receipt.get("usage"),
            "receipt_ref": stored.get("ref_uri"),
            "receipt_sha256": stored.get("sha256"),
        }

    def _execute_in_worktree(self, prompt: str, base_ref: Optional[str]):
        """Run the executor inside a leased worktree slot. Returns (exec_res, slot or None)."""
        if self.worktree_pool is None:
            return {"status": "NOT_CONFIGURED", "exit_code": None,
                    "error": "edit mode requires a worktree pool"}, None
        if getattr(self.executor, "mode", None) != "accept-edits":
            return {"status": "NOT_CONFIGURED", "exit_code": None,
                    "error": "edit mode requires an executor in accept-edits mode"}, None

        lease = self.worktree_pool.acquire(self.EXECUTOR_HOLDER_ID, ref=base_ref)
        if lease.get("status") != "ACQUIRED":
            detail = "; ".join(q.get("detail", "") for q in lease.get("quarantined_during_acquire") or [])
            failure = {"status": "FAILED", "exit_code": None,
                       "error": f"worktree {lease.get('status')}: {lease.get('error') or detail or 'no slot'}"}
            failure["triage"] = triage_receipt(failure)
            return failure, None

        receipt = self.executor.run(prompt, cwd=lease["path"])
        exec_res = self._summarize_receipt(receipt, self._store_receipt(receipt))
        exec_res["worktree_slot"] = lease["slot"]
        exec_res["base_commit"] = lease.get("commit")

        if receipt.get("status") == "SUCCESS":
            export = self.worktree_pool.export_changes(lease["slot"], self.EXECUTOR_HOLDER_ID)
            if export.get("status") != "OK":
                exec_res.update(status="FAILED", error=f"patch export failed: {export.get('error')}")
            elif not export.get("changed_files"):
                # An edit task that changed nothing has produced no artifact.
                exec_res.update(status="FAILED", error="no changes produced in edit mode")
            else:
                stored_patch = self.adapter.execute_tool("trinity_store_artifact", {
                    "content": export["patch"], "artifact_type": "patch",
                })
                exec_res.update(changed_files=export["changed_files"],
                                patch_ref=stored_patch.get("ref_uri"),
                                patch_sha256=stored_patch.get("sha256"))

        if exec_res.get("status") in FAILURE_STATUSES:
            exec_res["triage"] = triage_receipt(dict(receipt, status=exec_res["status"], error=exec_res.get("error")))
        return exec_res, lease

    def _release_worktree(self, lease: Optional[Dict[str, Any]], exec_res: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Reset and return the slot only when its work is preserved (or there was none)."""
        if not lease:
            return None
        released = self.worktree_pool.release(
            lease["slot"], self.EXECUTOR_HOLDER_ID, discard=bool(exec_res.get("patch_ref"))
        )
        return {"slot": lease["slot"], "base_commit": lease.get("commit"),
                "release_status": released.get("status")}

    def execute_cross_relay(
        self,
        task_title: str,
        task_instruction: str,
        target_file: Optional[str] = None,
        source_code: Optional[str] = None,
        mock_mode: bool = False,
        edit_mode: bool = False,
        base_ref: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes the 4-stage cross-relay pipeline and reports an evidence-derived verdict.
        """
        start_time = time.time()
        tx_id = f"tx_{uuid.uuid4().hex[:12]}"
        trace: List[Dict[str, Any]] = []

        # -------------------------------------------------------------
        # STAGE 1: Codex Planner (Task Card Issuance)
        # -------------------------------------------------------------
        stage1_start = time.time()
        card_text = f"1. Target: {target_file or 'General'}\n2. Action: {task_title}\n3. Constraint: Minimal scoped patch"

        card_args = {
            "sender": "codex_planner",
            "target": self.EXECUTOR_HOLDER_ID,
            "task_name": task_title,
            "card_text": card_text,
            "payload": {
                "instruction": task_instruction,
                "target_file": target_file
            },
            "idempotency_key": tx_id
        }
        send_res = self.adapter.execute_tool("trinity_send_card", card_args)
        trace.append({
            "stage": 1,
            "agent": "Codex (Planner)",
            "action": "ISSUE_TASK_CARD",
            "result": send_res,
            "duration_ms": (time.time() - stage1_start) * 1000.0
        })

        # -------------------------------------------------------------
        # STAGE 2: Ollama Preprocess (0-Token AST Skeleton)
        # -------------------------------------------------------------
        stage2_start = time.time()
        skeleton_res: Dict[str, Any] = {}
        ollama_status = "SKIPPED"

        if source_code:
            # extract_ast_skeleton returns a status dict; never forward raw source on failure.
            skeleton_res = self.ollama.extract_ast_skeleton(source_code)
            if mock_mode:
                self.ollama.generate("Analyze AST", mock_response="AST_ANALYZED_OK")
                ollama_status = "SIMULATED"
            else:
                ollama_res = self.ollama.generate(f"Verify skeleton for {task_title}")
                if ollama_res.get("status") == "FAILFAST_FALLBACK_CLOUD":
                    self.state = OrganismState.TWO_ALIVE
                    ollama_status = "FALLBACK_CLOUD"
                else:
                    # Local evidence only; the cloud tools remain unverified.
                    ollama_status = "LOCAL_OLLAMA_SUCCESS"

        trace.append({
            "stage": 2,
            "agent": "Ollama (Preprocess)",
            "action": "AST_PREPROCESS",
            "status": ollama_status,
            "skeleton_status": skeleton_res.get("status", "SKIPPED"),
            "skeleton_chars": skeleton_res.get("skeleton_chars", 0),
            "duration_ms": (time.time() - stage2_start) * 1000.0
        })

        # -------------------------------------------------------------
        # STAGE 3: Antigravity Executor (Bounded Execution & Receipt)
        # -------------------------------------------------------------
        stage3_start = time.time()
        self.adapter.execute_tool("trinity_read_inbox", {"agent_id": self.EXECUTOR_HOLDER_ID})

        lock_path = target_file or f"global_lock_{tx_id}"
        lock_res = self.adapter.execute_tool("trinity_acquire_lock", {
            "resource_path": lock_path,
            "holder_id": self.EXECUTOR_HOLDER_ID
        })

        if lock_res.get("status") != "ACQUIRED":
            # Fail-closed: never execute, and never release a lock held by someone else.
            trace.append({
                "stage": 3,
                "agent": "Antigravity (Executor)",
                "action": "EXECUTE_TASK",
                "lock_status": lock_res.get("status"),
                "error": f"LOCK_CONFLICT: '{lock_path}' held by {lock_res.get('current_holder', 'unknown')}",
                "duration_ms": (time.time() - stage3_start) * 1000.0
            })
            return {
                "tx_id": tx_id,
                "overall_status": "HALTED_LOCK_CONFLICT",
                "next_action": "WAIT_FOR_LOCK",
                "organism_state": self.state.value,
                "total_duration_ms": (time.time() - start_time) * 1000.0,
                "briefing": f"[Trinity-ACE 중단] {task_title}: 자원 '{lock_path}' 잠금 실패로 실행하지 않았습니다",
                "trace": trace
            }

        lease: Optional[Dict[str, Any]] = None
        prompt = (f"{card_text}\n\nInstruction: {task_instruction}\n"
                  "Return a concise result of at most 10 lines.")
        try:
            if mock_mode:
                runner_res = self.mock_runner.execute_task(
                    f"Execute {task_title}",
                    mock_response=f"PATCH_OK: Modified {target_file or 'system'} cleanly."
                )
                # A mock runner's SUCCESS/exit_code 0 is not executor evidence.
                exec_res = dict(runner_res, status="SIMULATED", exit_code=None)
            elif self.executor is not None and edit_mode:
                exec_res, lease = self._execute_in_worktree(prompt, base_ref)
            elif self.executor is not None:
                receipt = self.executor.run(prompt)
                exec_res = self._summarize_receipt(receipt, self._store_receipt(receipt))
                if receipt.get("status") in FAILURE_STATUSES:
                    exec_res["triage"] = triage_receipt(receipt)
            else:
                exec_res = {
                    "status": "NOT_CONFIGURED",
                    "exit_code": None,
                    "output": "No executor adapter configured; task was not executed."
                }
        finally:
            self.adapter.execute_tool("trinity_release_lock", {
                "resource_path": lock_path,
                "holder_id": self.EXECUTOR_HOLDER_ID
            })

        trace.append({
            "stage": 3,
            "agent": "Antigravity (Executor)",
            "action": "EXECUTE_TASK",
            "lock_status": lock_res.get("status"),
            "execution": exec_res,
            "duration_ms": (time.time() - stage3_start) * 1000.0
        })

        # -------------------------------------------------------------
        # STAGE 4: Local Verifier (Evidence)
        # -------------------------------------------------------------
        stage4_start = time.time()
        try:
            if mock_mode:
                verification = {"status": "SIMULATED", "exit_code": None, "evidence": []}
            elif self.verifier is None:
                verification = {"status": "UNVERIFIED", "exit_code": None, "evidence": []}
            elif exec_res.get("status") != "SUCCESS":
                # Verifying an execution that did not succeed would only produce misleading evidence.
                verification = {"status": "SKIPPED", "exit_code": None, "evidence": [],
                                "error": f"execution status was {exec_res.get('status')}"}
            else:
                # In edit mode the check must see the edited worktree, not the main repository.
                check = self.verifier.run(cwd=lease["path"]) if lease else self.verifier.run()
                stored_check = self._store_receipt(check)
                verification = {
                    "status": check.get("status"),
                    "exit_code": check.get("exit_code"),
                    "evidence": check.get("evidence") or [],
                    "error": check.get("error"),
                    "receipt_ref": stored_check.get("ref_uri"),
                    "receipt_sha256": stored_check.get("sha256"),
                }
                if check.get("status") in FAILURE_STATUSES:
                    verification["triage"] = triage_receipt(check)
            trace.append({
                "stage": 4,
                "agent": "Local Verifier",
                "action": "VERIFY",
                "verification_status": verification["status"],
                "exit_code": verification["exit_code"],
                "verification": verification,
                "duration_ms": (time.time() - stage4_start) * 1000.0
            })
        finally:
            worktree = self._release_worktree(lease, exec_res)

        overall_status = derive_overall_status(exec_res, verification)
        failure_triage = exec_res.get("triage") or verification.get("triage")
        next_action = failure_triage["prescription"] if failure_triage else "NONE"

        total_duration_ms = (time.time() - start_time) * 1000.0
        briefing = (
            f"[Trinity-ACE {overall_status}] {task_title}\n"
            f"- {_BRIEFING_BY_STATUS[overall_status]}\n"
            f"- 다음 조치: {next_action}\n"
            f"- 작업 카드: {tx_id} / 잠금 대상: {lock_path}\n"
            + (f"- 패치(메인 저장소 미적용): {exec_res['patch_ref']}\n" if exec_res.get("patch_ref") else "")
            + f"- 상태: {self.state.value}, {total_duration_ms:.1f}ms"
        )

        result = {
            "tx_id": tx_id,
            "overall_status": overall_status,
            "next_action": next_action,
            "organism_state": self.state.value,
            "total_duration_ms": total_duration_ms,
            "briefing": briefing,
            "trace": trace
        }
        if exec_res.get("patch_ref"):
            result["patch_ref"] = exec_res["patch_ref"]
        if worktree:
            result["worktree"] = worktree
        return result

    def close(self):
        """Clean shutdown of adapter and DB connections."""
        self.adapter.close()


if __name__ == "__main__":
    orchestrator = TrinityOrchestrator()
    try:
        sample_code = "def process_data(items: list):\n    # TODO\n    return [x * 2 for x in items]\n"
        res = orchestrator.execute_cross_relay(
            task_title="Refactor Data Processor",
            task_instruction="Optimize list comprehension for zero copy memory",
            target_file="src/processor.py",
            source_code=sample_code,
            mock_mode=True
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        print("\n" + res["briefing"])
    finally:
        orchestrator.close()
