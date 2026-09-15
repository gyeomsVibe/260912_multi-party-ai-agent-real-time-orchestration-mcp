# C3P W01 PLAN_LOCKED — 결과 의미론과 위장 성공 제거

## 판정

**상태: `PLAN_LOCKED — READY_FOR_BUILD`**

W01의 범위와 결과 상태 계약은 잠겼다. 사용자가 기존 Antigravity 소유 변경의 역할별 재배분을 명시 승인했으므로 W01 구현 소유권은 Claude Code로 이전됐고, 기존 내용과 아래 스냅숏을 보존하는 조건으로 `BUILDING` 전환이 가능하다.

```yaml
work_id: W01
title: 결과 의미론과 위장 성공 제거
state: PLAN_LOCKED
owner: codex
source_plan:
  - docs/23_[C3P_통합수행규칙]_단일계획_프로젝트대화창_및_순차스케줄.md
  - docs/22_[C3P_3자절충합의]_실전하네스_다음구현_상세계획서.md
base_revision:
  commit: 43ca02763b3e5f7b976d9d00a765941ca2bd20f5
  branch: main
  working_tree: DIRTY_PRESERVE
  snapshot:
    central_hub/trinity_orchestrator.py: B6A3BAB6370F7F73DF4EF5876B34E7FF8EC740989DDE7A5EDF5A2BE399AB1395
    tests/test_live_orchestration.py: F690088776DDCBEA78045B2A43CC08F50824FDDEDB77A91B815A05801561856B
    tests/test_orchestrator.py: 344AA7805E20B74EDA81955F7A08A7A9FD27AB461E08010FED4B1847BCD89628
goal: 실제 executor 실행 및 검증 증거가 없는 cross-relay 결과에서 SUCCESS와 VERIFIED_GREEN을 완전히 제거하고, 실행 모드별 결과를 fail-closed 의미론으로 반환한다.
scope:
  files:
    - central_hub/trinity_orchestrator.py
    - tests/test_live_orchestration.py
    - tests/test_orchestrator.py
  actions:
    - execute_cross_relay의 최종 상태 도출 규칙을 모드별로 교정한다.
    - Stage 3의 고정 SUCCESS 결과를 제거한다.
    - Stage 4의 고정 VERIFIED_GREEN 및 가짜 exit_code=0을 제거한다.
    - 증거가 없는 완료·패치 적용·실측 검증 표현을 briefing에서 제거한다.
    - 기존 성공 기대 테스트를 실패 우선 테스트로 전환한다.
    - 기존 lock-conflict 및 W00 회귀 동작을 보존한다.
ownership_gate:
  status: RELEASED_BY_USER_REASSIGNMENT
  implementation_owner: claude_code
  verification_owner: antigravity
  rule: Claude Code는 BUILD 시작 직전에 위 SHA-256과 일치하는지 확인한다. 불일치하면 BUILDING으로 전환하지 않고 NEEDS_REVIEW를 반환한다.
  preservation: 현재 lock guard, ExecutionMode, liveness 초안, WorktreePool 초안을 삭제하거나 원복하지 않고 W01 의미론에 필요한 최소 줄만 수정한다.
state_contract:
  shared_rules:
    - 외부 실행 증거와 독립 검증 증거가 모두 없으면 overall_status는 SUCCESS가 될 수 없다.
    - Ollama 응답, 카드 발행, 락 획득은 executor 성공 또는 E2E 검증 증거가 아니다.
    - Stage 4 verifier가 실제 호출되지 않으면 VERIFIED_GREEN과 exit_code=0을 생성하지 않는다.
    - W01에서는 가상의 evidence receipt나 executor 결과를 새로 만들어 성공을 허용하지 않는다.
  MOCK:
    stage_3_execution: SIMULATED
    stage_4_verification: UNVERIFIED
    overall_status: SIMULATED
    briefing: 모의 실행이며 실제 패치·검증 완료가 아님을 명시
  HYBRID:
    stage_3_execution: SIMULATED
    stage_4_verification: UNVERIFIED
    overall_status: UNVERIFIED
    note: 현재 실제 executor와 verifier가 없으므로 PARTIAL로 승격할 실제 실행 증거가 없다. 향후 일부 실제 증거가 생긴 경우에만 PARTIAL을 고려한다.
    briefing: 실제 단계와 모의·미검증 단계를 구분하고 완결 표현 금지
  STRICT_LIVE:
    without_executor_adapter:
      stage_3_execution: NOT_CONFIGURED
      stage_4_verification: UNVERIFIED
      overall_status: NOT_CONFIGURED
    missing_required_evidence:
      overall_status: UNVERIFIED
    briefing: 설정 누락 또는 증거 누락을 명시하고 성공 표현 금지
  legacy_dispatch:
    mock_mode_true_without_execution_mode: MOCK
    mock_mode_false_without_execution_mode: HYBRID
    rule: 기존 호출 호환성은 유지하되 실제 수행 수준보다 높은 상태를 반환하지 않음
  preserved_terminal_states:
    - HALTED_LOCK_CONFLICT
    - FAILED_WORKTREE
  success_eligibility: W01에서는 SUCCESS를 반환할 수 있는 실제 executor·verifier 경로가 없다. 해당 자격은 W03 및 W06의 증거 계약이 구현된 이후 별도 작업에서만 추가한다.
failure_first_tests:
  red_phase:
    - MOCK 호출이 overall_status == SIMULATED이며 SUCCESS가 아님을 요구한다. 현재 구현에서는 SUCCESS이므로 먼저 실패해야 한다.
    - HYBRID 및 기본 mock_mode=False 호출이 overall_status == UNVERIFIED이며 Stage 3의 고정 SUCCESS를 포함하지 않음을 요구한다.
    - STRICT_LIVE에서 executor adapter가 없으면 NOT_CONFIGURED를 요구한다.
    - 모든 비중단 정상 반환에서 Stage 4 verification_status가 VERIFIED_GREEN이 아님을 요구한다.
    - briefing에 "완결", "패치 적용 완료", "실측 검증 완료"가 없음을 요구한다.
    - Ollama fallback이 TWO_ALIVE를 유지하더라도 overall_status를 SUCCESS로 승격하지 않음을 요구한다.
    - WorktreePool 기존 모의 테스트의 결과만 SIMULATED로 교정하되, acquire/release/discard 관측은 W02까지 현 상태를 그대로 고정한다.
  green_phase:
    - python -m unittest tests.test_live_orchestration -v
    - python -m unittest tests.test_orchestrator -v
    - python -m unittest discover -s tests -q
acceptance:
  - 관련 실패 우선 테스트가 수정 전 Red, 수정 후 Green임이 명령·출력·exit code로 남는다.
  - 실제 executor 증거가 없는 모든 테스트에서 overall_status == SUCCESS가 0건이다.
  - 'Stage 3의 fabricated {"status":"SUCCESS"} 반환이 제거된다.'
  - 실제 verifier 호출 없이 VERIFIED_GREEN 또는 검증 exit_code=0을 생성하는 경로가 0건이다.
  - MOCK은 SIMULATED, 현재 HYBRID는 UNVERIFIED, 미설정 STRICT_LIVE는 NOT_CONFIGURED를 반환한다.
  - HALTED_LOCK_CONFLICT와 FAILED_WORKTREE 결과는 회귀하지 않는다.
  - 전체 회귀 테스트가 exit code 0으로 통과한다.
  - Antigravity 소유 스냅숏의 W01 비관련 변경이 보존된다.
  - 허용 파일 외 변경이 0건이다.
non_goals:
  - 실제 WorktreePool API 계약 수정
  - acquire/release 시그니처 교정
  - export_changes, patch digest, artifact digest 구현
  - discard=True 수명주기 수정 또는 정리 정책 변경
  - 실제 executor 또는 evidence receipt 구현
  - timeout, 429, auth, 테스트 실패의 triage 연결
  - liveness 및 3-ALIVE 상태 모델 수정
  - 실제 Antigravity verifier 연결
  - 자동 merge, rollback, discard 또는 배포
  - central_hub/triage_classifier.py 수정
  - harness/ollama_worker.py 수정
  - docs/21 또는 W02 이후 범위 수정
prohibitions:
  - 현재 Antigravity 소유 변경을 원복·재작성·포맷팅하지 않는다.
  - 소유권 인계 전 BUILDING으로 전환하지 않는다.
  - 테스트 통과 수를 실제 E2E 성공 증거로 표현하지 않는다.
  - UNKNOWN, SIMULATED, UNVERIFIED, NOT_CONFIGURED를 SUCCESS로 정규화하지 않는다.
  - W02 문제를 함께 고친다는 이유로 WorktreePool 코드를 건드리지 않는다.
  - 기존 discard=True 동작을 승인된 설계로 표현하지 않는다.
  - 증거 없이 VERIFIED 또는 CLOSED를 선언하지 않는다.
evidence:
  planning_inspection:
    - AGENTS.md 완독
    - docs/23 완독
    - docs/22 완독
    - docs/24 완독
    - 관련 소스·테스트·HEAD 버전 비교
    - git status 및 대상 파일 SHA-256 재확인
  tests:
    - NOT_RUN_PLANNING_READ_ONLY
  artifacts:
    - docs/25_[C3P_W01_PLAN_LOCKED]_결과의미론과_위장성공제거.md
  observed_limitations:
    - git status 중 사용자 전역 ignore 파일 접근 거부 경고
    - .pytest_cache 디렉터리 접근 거부 경고
    - 두 경고는 대상 파일과 추적 상태 확인을 막지는 않음
risks:
  - 기준 스냅숏이 달라지면 재배분 승인과 무관하게 소유권 충돌 가능성이 있으므로 NEEDS_REVIEW가 필요하다.
  - 기존 테스트가 위장 성공을 계약으로 고정하고 있어 함께 교정하지 않으면 회귀 테스트가 실패한다.
  - W01 이후에도 WorktreePool 자동 discard 위험과 잘못된 실제 API 계약은 W02까지 남는다.
  - liveness의 가짜 3-ALIVE 가능성은 W05까지 남으며 W01 성공 판단 근거로 사용할 수 없다.
next_owner: claude_code
next_state: BUILDING
next_state_precondition: 대상 SHA-256 일치 확인과 허용 파일 외 작업 금지 수락
```

## 역할별 소유권 재배분

사용자의 명시적 승인에 따라 기존 변경의 내용은 보존하고 역할 소유권만 다음과 같이 재배분한다.

| 경로·책임 | 역할 소유자 | 적용 시점과 제한 |
|---|---|---|
| 정본 계획·상태 계약·종결 | Codex | `docs/23`, 이 카드와 후속 상태 판정만 관리 |
| `central_hub/trinity_orchestrator.py` | Claude Code | W01 BUILD 대상, 승인된 결과 의미론만 최소 수정 |
| `tests/test_live_orchestration.py` | Claude Code | W01 구현·실패 우선·회귀 테스트 대상 |
| `tests/test_orchestrator.py` | Claude Code | W01 구현·실패 우선·회귀 테스트 대상 |
| Antigravity 독립 VERIFY·E2E 증거 | Antigravity | `BUILT` 이후 읽기 전용 검증, 검증 중 구현 코드 직접 수정 금지 |
| `central_hub/triage_classifier.py` | Claude Code | 미래 triage BUILD 시에만 구현 소유, W01 수정 금지 |
| `harness/ollama_worker.py` | Claude Code | 해당 미래 BUILD 시에만 구현 소유, W01 수정 금지 |
| `docs/21_[MIA전략_협의기획] 3대도구_진행상황_동기화_및_실전하네스_차기구현_상세계획서.md` | 역사적 제안 자료 | 현재 내용을 보존하며 W01에서 수정하지 않음 |

## `/CRITIC` 결론

위장 성공은 네 지점에서 발생한다.

1. Stage 3 비모의 경로가 실제 executor를 호출하지 않고 고정 `SUCCESS`를 만든다.
2. Stage 4가 실제 Antigravity 검증 없이 고정 `VERIFIED_GREEN`과 `exit_code=0`을 만든다.
3. 최종 반환이 실행 모드와 증거 유무에 관계없이 `SUCCESS`다.
4. briefing이 실제 수행되지 않은 패치와 검증을 “완료”와 “완결”로 표현한다.

W01은 이 네 의미론만 교정한다. 새로운 실제 executor, verifier 또는 증거 영수증을 가장해 성공 경로를 만들지 않는다.

## `/REDTEAM` 결론

* Ollama 성공, 카드 발행 또는 락 획득을 executor 성공 증거로 승격하는 우회를 금지한다.
* `MOCK` 또는 현재 `HYBRID` 결과가 내부 모의 출력 문자열 때문에 `SUCCESS`가 되는 우회를 금지한다.
* 미설정 `STRICT_LIVE`가 sandbox 문구나 기본 exit code 때문에 성공으로 보이는 우회를 금지한다.
* 테스트 통과 개수만으로 실제 E2E 성공을 선언하는 우회를 금지한다.
* 이번 사용자 재배분 승인 범위를 넘어 다른 파일이나 미래 작업의 즉시 구현 권한까지 이전된 것으로 간주하지 않는다.
* W01을 명분으로 WorktreePool, discard, liveness 또는 triage를 함께 수정하는 범위 확장을 금지한다.

## 수행 순서

1. 재배분된 소유권과 대상 SHA-256을 확인한다.
2. 기존 성공 기대를 위 상태 계약의 실패 우선 테스트로 바꾸고 Red 증거를 남긴다.
3. `central_hub/trinity_orchestrator.py`에서 결과 의미론만 최소 수정한다.
4. 관련 테스트와 전체 회귀 테스트를 실행해 Green 및 exit code 0을 확인한다.
5. Claude Code가 `BUILT` 인계 카드를 반환하고 쓰기를 중단한다.
6. Antigravity가 구현 변경 없이 독립 검증한 뒤에만 `VERIFIED` 후보가 된다.
