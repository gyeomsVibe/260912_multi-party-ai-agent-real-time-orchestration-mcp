"""
central_hub/trinity_orchestrator.py
End-to-End Cross-Relay Pipeline Orchestrator - Trinity-ACE Protocol

Coordinates the 4-Stage Operating Lifecycle:
1. Codex (Brain / Command): Task planning, idempotency card emission.
2. Ollama (0-Token Slave): AST skeleton extraction & fixture generation.
3. Claude Code (Immune / Muscle): Targeted surgical patch & triage intercept.
4. Antigravity (Sensory & Hands): E2E test verification & user-facing briefing.
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
    THREE_ALIVE = "3-ALIVE"  # All 3 tools + Ollama active
    TWO_ALIVE = "2-ALIVE"    # One tool degraded; dynamic failover active
    ONE_ALIVE = "1-ALIVE"    # Emergency isolated defense


class ExecutionMode(str, Enum):
    MOCK = "MOCK"
    HYBRID = "HYBRID"
    STRICT_LIVE = "STRICT_LIVE"


class TrinityOrchestrator:
    """The central coordinator executing cross-relay agentic workflows."""

    def __init__(
        self,
        db_path: str = ":memory:",
        vault_dir: Optional[str] = None,
        ollama_base_url: str = "http://localhost:11434",
        worktree_pool: Optional[Any] = None
    ):
        self.db_path = db_path
        self.adapter = MCPServerAdapter(db_path=db_path, vault_dir=vault_dir)
        self.ollama = OllamaWorker(base_url=ollama_base_url, timeout_seconds=2.0)
        self.claude_runner = HeadlessAgentRunner("claude_code")
        self.worktree_pool = worktree_pool
        self.state = OrganismState.THREE_ALIVE

    def refresh_organism_liveness(self) -> OrganismState:
        """Polls component liveness and updates OrganismState dynamically."""
        ollama_ok = self.ollama.check_health()
        if not ollama_ok:
            self.state = OrganismState.TWO_ALIVE
        else:
            self.state = OrganismState.THREE_ALIVE
        return self.state

    def execute_cross_relay(
        self,
        task_title: str,
        task_instruction: str,
        target_file: Optional[str] = None,
        source_code: Optional[str] = None,
        mock_mode: bool = False,
        execution_mode: Optional[ExecutionMode] = None
    ) -> Dict[str, Any]:
        """
        Executes the full 4-stage cross-relay pipeline with fault recovery and fail-closed lock defense.
        """
        if execution_mode is None:
            mode = ExecutionMode.MOCK if mock_mode else ExecutionMode.HYBRID
        else:
            mode = execution_mode
        is_mock = (mode == ExecutionMode.MOCK) or mock_mode

        start_time = time.time()
        tx_id = f"tx_{uuid.uuid4().hex[:12]}"
        trace: List[Dict[str, Any]] = []

        # -------------------------------------------------------------
        # STAGE 1: Codex Brain (Planning & Idempotency Card Issuance)
        # -------------------------------------------------------------
        stage1_start = time.time()
        card_text = f"1. Target: {target_file or 'General'}\n2. Action: {task_title}\n3. Constraint: Minimal scoped patch"

        card_args = {
            "sender": "codex_brain",
            "target": "claude_immune",
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
            "agent": "Codex (Brain)",
            "action": "ISSUE_TASK_CARD",
            "result": send_res,
            "duration_ms": (time.time() - stage1_start) * 1000.0
        })

        # -------------------------------------------------------------
        # STAGE 2: Ollama Slave (0-Token AST Skeleton & Preprocessing)
        # -------------------------------------------------------------
        stage2_start = time.time()
        ast_skeleton = None
        ollama_status = "SKIPPED"

        if source_code:
            ast_skeleton = self.ollama.extract_ast_skeleton(source_code)
            if mock_mode:
                ollama_res = self.ollama.generate("Analyze AST", mock_response="AST_ANALYZED_OK")
            else:
                ollama_res = self.ollama.generate(f"Verify skeleton for {task_title}")

            if ollama_res.get("status") == "FAILFAST_FALLBACK_CLOUD":
                self.state = OrganismState.TWO_ALIVE
                ollama_status = "FALLBACK_CLOUD"
            else:
                ollama_status = "LOCAL_OLLAMA_SUCCESS"

        trace.append({
            "stage": 2,
            "agent": "Ollama (Slave)",
            "action": "AST_PREPROCESS",
            "status": ollama_status,
            "skeleton_length": len(ast_skeleton) if ast_skeleton else 0,
            "duration_ms": (time.time() - stage2_start) * 1000.0
        })

        # -------------------------------------------------------------
        # STAGE 3: Claude Code Immune (Surgical Patch & Triage Defense)
        # -------------------------------------------------------------
        stage3_start = time.time()
        # Read inbox
        inbox_res = self.adapter.execute_tool("trinity_read_inbox", {"agent_id": "claude_immune"})

        # Acquire lock
        lock_path = target_file or f"global_lock_{tx_id}"
        lock_res = self.adapter.execute_tool("trinity_acquire_lock", {
            "resource_path": lock_path,
            "holder_id": "claude_immune"
        })

        if lock_res.get("status") != "ACQUIRED":
            # FAIL-CLOSED INTERCEPT: Immediately halt to avoid corrupting shared files
            trace.append({
                "stage": 3,
                "agent": "Claude Code (Immune)",
                "action": "SURGICAL_PATCH",
                "lock_status": lock_res.get("status"),
                "error": f"LOCK_CONFLICT: Resource '{lock_path}' cannot be acquired (Status: {lock_res.get('status')}).",
                "duration_ms": (time.time() - stage3_start) * 1000.0
            })
            return {
                "tx_id": tx_id,
                "overall_status": "HALTED_LOCK_CONFLICT",
                "organism_state": self.state.value,
                "total_duration_ms": (time.time() - start_time) * 1000.0,
                "briefing": f"[Trinity-ACE 중단] 락 경합으로 인한 Fail-Closed 방어: 자원 '{lock_path}' 점유 실패 ({lock_res.get('status')})",
                "trace": trace
            }

        # Worktree isolation slot (if pool configured)
        slot = None
        worktree_desc = "sandbox"
        if self.worktree_pool:
            try:
                slot = self.worktree_pool.acquire(holder_id="claude_immune", purpose=f"cross_relay_{tx_id}")
                worktree_desc = f"worktree {slot.name}"
            except Exception as e:
                # Worktree pool acquisition failed - triage
                triage = TriageClassifier.classify(str(e))
                self.adapter.execute_tool("trinity_release_lock", {
                    "resource_path": lock_path,
                    "holder_id": "claude_immune"
                })
                trace.append({
                    "stage": 3,
                    "agent": "Claude Code (Immune)",
                    "action": "WORKTREE_ACQUIRE",
                    "error": str(e),
                    "triage": triage,
                    "duration_ms": (time.time() - stage3_start) * 1000.0
                })
                return {
                    "tx_id": tx_id,
                    "overall_status": "FAILED_WORKTREE",
                    "organism_state": self.state.value,
                    "total_duration_ms": (time.time() - start_time) * 1000.0,
                    "briefing": f"[Trinity-ACE 중단] Worktree 격리 실패: {e}",
                    "trace": trace
                }

        # W01 fail-closed semantics: no real executor exists yet, so Stage 3 never reports SUCCESS.
        try:
            if mode == ExecutionMode.STRICT_LIVE:
                exec_res = {
                    "status": "NOT_CONFIGURED",
                    "exit_code": None,
                    "output": f"No executor adapter configured; patch not executed in {worktree_desc}."
                }
            elif is_mock:
                runner_res = self.claude_runner.execute_task(
                    f"Apply patch for {task_title}",
                    mock_response=f"PATCH_OK: Modified {target_file or 'system'} cleanly in {worktree_desc}."
                )
                # Mock runner output is not executor evidence; never surface its SUCCESS/exit_code=0.
                exec_res = dict(runner_res, status="SIMULATED", exit_code=None)
            else:
                exec_res = {
                    "status": "SIMULATED",
                    "exit_code": None,
                    "output": f"No executor invoked; patch not applied in {worktree_desc}."
                }
        finally:
            # Always release lock
            self.adapter.execute_tool("trinity_release_lock", {
                "resource_path": lock_path,
                "holder_id": "claude_immune"
            })
            if slot and self.worktree_pool:
                self.worktree_pool.release(slot.name, discard=True)

        trace.append({
            "stage": 3,
            "agent": "Claude Code (Immune)",
            "action": "SURGICAL_PATCH",
            "lock_status": lock_res.get("status"),
            "execution": exec_res,
            "duration_ms": (time.time() - stage3_start) * 1000.0
        })

        # -------------------------------------------------------------
        # STAGE 4: Antigravity Sensory (Verification & User Briefing)
        # -------------------------------------------------------------
        stage4_start = time.time()
        # No verifier is invoked in W01: never fabricate VERIFIED_GREEN or exit_code=0.
        verification_status = "UNVERIFIED"
        exit_code = None

        if mode == ExecutionMode.STRICT_LIVE:
            overall_status = "NOT_CONFIGURED"
            header = "[Trinity-ACE 설정 누락]"
            stage3_note = "executor adapter 미설정으로 패치 미실행"
        elif mode == ExecutionMode.MOCK:
            overall_status = "SIMULATED"
            header = "[Trinity-ACE 모의 실행]"
            stage3_note = "모의 실행이며 실제 패치 미적용"
        else:
            overall_status = "UNVERIFIED"
            header = "[Trinity-ACE 미검증]"
            stage3_note = "실제 executor 미호출, 모의 단계로 실제 패치 미적용"

        total_duration_ms = (time.time() - start_time) * 1000.0
        briefing = (
            f"{header} {task_title}\n"
            f"- 사령탑(Codex): 3줄 카드({tx_id}) 발행\n"
            f"- 면역계(Claude Code): {stage3_note} (Lock: {lock_path})\n"
            f"- 감각기(Antigravity): 검증 미수행, 증거 없음 (결과: {overall_status}, 상태: {self.state.value}, {total_duration_ms:.1f}ms)"
        )

        trace.append({
            "stage": 4,
            "agent": "Antigravity (Sensory)",
            "action": "E2E_VERIFICATION",
            "verification_status": verification_status,
            "exit_code": exit_code,
            "duration_ms": (time.time() - stage4_start) * 1000.0
        })

        return {
            "tx_id": tx_id,
            "overall_status": overall_status,
            "organism_state": self.state.value,
            "total_duration_ms": total_duration_ms,
            "briefing": briefing,
            "trace": trace
        }

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
