# C3P W02 PLAN_LOCKED — 실제 WorktreePool 계약 브리지

## MIA 판단

**Decision Gate: Go (계약 브리지와 수명주기 증명으로 한정).** W01은 `docs/27`에서 `CLOSED`됐고 `docs/23`의 다음 순차 작업은 W02다. 현재 오케스트레이터의 더미 풀 호출은 실제 `WorktreePool` 공개 계약과 호환되지 않으며 성공 산출물을 export하기 전에 폐기할 수 있다. W02는 이 계약·수명주기 공백만 닫는다. 실제 외부 executor, 자동 merge/discard, liveness 및 Ollama 모델 풀은 범위 밖이다.

## 관측 증거와 계약 대조

기준 revision은 `43ca02763b3e5f7b976d9d00a765941ca2bd20f5` (`main`)이다. W00/W01 및 Antigravity 소유 미커밋 변경이 있으므로 BUILD 기준선은 아래 working-tree snapshot까지 함께 고정한다.

| 항목 | 실제 `WorktreePool` | 현재 오케스트레이터/더미 | W02 요구 |
|---|---|---|---|
| acquire | `acquire(agent_id, ref=None) -> Dict` | `acquire(holder_id, purpose) -> object` | `agent_id="claude_immune"`, `ref=base_sha`로 호출 |
| acquire 결과 | `status`, `slot`, `path`, `commit`, `holder` | `slot.name`, `slot.path` 가정 | 필수 키·상태 검증, 객체 속성 가정 제거 |
| export | `export_changes(name, agent_id) -> Dict` | 호출 없음 | 변경 뒤 release 전에 호출하고 patch/changed-files digest 보존 |
| release | `release(name, agent_id, discard=False) -> Dict` | `release(slot.name, discard=True)` | 동일 `agent_id` 전달, 기본 `discard=False` |
| 정리 정책 | dirty + `discard=False`는 `DIRTY`로 보존 | `finally` 무조건 discard | export 증거 저장 전 discard 금지, 자동 merge/discard 금지 |

```yaml
base_revision: 43ca02763b3e5f7b976d9d00a765941ca2bd20f5
branch: main
working_tree_snapshot:
  central_hub/worktree_pool.py: 26816F86C6D3E123AD7FE0B868DA4CE140C4AD89185BEF1056441CAD34AC4FF9
  central_hub/trinity_orchestrator.py: 3B0C3F36BC32FE343CA84A2267008AE6066FE96079EE2763532D234B343A45CA
  tests/test_worktree_pool.py: 31FF0A838516953C8A5BCAEDAF754E685EF75D075BC16805DAC6E69973BBD0E3
  tests/test_live_orchestration.py: 83C449388FC377EF735E81609D0F28BC334616D43152385FBFE4ED66C9973CC2
  tests/test_orchestrator.py: CE7AC80E09933D45121193093610388BE8A47B6141693BF62BA6905E837A1FE9
```

BUILD 시작 전 Claude Code는 위 해시를 다시 계산한다. 하나라도 다르면 쓰지 않고 `NEEDS_REVIEW_OWNERSHIP_DRIFT`를 반환한다.

## 실행 설계

1. 실행 시작 시 확정된 `base_sha`로 `acquire(agent_id="claude_immune", ref=base_sha)`를 호출한다.
2. 반환값이 `Dict`이고 `status == "ACQUIRED"`일 때만 `slot`, `path`, `commit`, `holder`를 읽는다. 키 누락, holder/commit 불일치는 fail-closed다.
3. W02 테스트용 제한적 변경 콜백은 획득한 `path` 아래 fixture만 바꾼다. 실제 외부 executor가 아니며 W03을 선점하지 않는다.
4. 변경 직후 `export_changes(slot_name, agent_id)`를 호출한다. `status == "OK"`인 patch와 changed-files를 임시 산출물에 저장하고 SHA-256 digest를 계산한 뒤 release한다.
5. 자동 merge는 하지 않는다. 운영 dirty slot은 `release(slot_name, agent_id, discard=False)`가 `DIRTY`를 반환하게 두고 `QUARANTINED/NEEDS_REVIEW`로 보존한다. 테스트 정리는 export와 digest 검증 뒤 테스트 소유 임시 저장소에서만 명시적 `discard=True`를 쓸 수 있다.
6. 예외가 발생해도 export 전에 discard하지 않는다. 락 해제, export 결과, release 결과와 순서를 trace에 남긴다.

## 대상 파일·소유권

```yaml
write_owner: claude_code
allowed_files:
  - central_hub/trinity_orchestrator.py
  - tests/test_live_orchestration.py
conditional_file:
  - tests/test_worktree_pool.py
conditional_rule: 공개 계약 회귀 보강에 꼭 필요할 때만 수정하고 BUILT 카드에 사유 명시
read_only_contract:
  - central_hub/worktree_pool.py
forbidden_files:
  - central_hub/triage_classifier.py
  - harness/ollama_worker.py
  - tests/test_orchestrator.py
```

`central_hub/trinity_orchestrator.py`, `tests/test_live_orchestration.py`, `tests/test_orchestrator.py`의 현재 W01 snapshot은 보존 대상이다. BUILD 동안 Claude Code만 허용 파일을 쓴다. Codex와 Antigravity는 읽기만 하며, Antigravity는 VERIFY에서 구현을 교정하지 않는다.

## 필수 테스트와 증거

Claude Code는 실패 테스트를 먼저 만들고 최소 수정 후 다음을 모두 exit `0`으로 제출한다.

1. 실제 임시 Git 저장소와 실제 `WorktreePool`을 생성·initialize한다.
2. 오케스트레이터 브리지로 `acquire(agent_id, ref)` → fixture 변경 → `export_changes(name, agent_id)` → patch/changed-files digest 저장 → `release(name, agent_id, discard=False)` 순서를 호출 로그로 증명한다.
3. export patch가 변경 내용과 신규 파일을 포함하고 `git apply --check`를 통과함을 확인한다.
4. export 이전 `discard=True` 감시 테스트가 교정 전 실패하고 교정 후 호출 0건임을 확인한다.
5. release/export에 `agent_id`가 빠지거나 다르면 성공하지 못함을 확인한다.
6. `slot.name` 객체 접근 없이 실제 `Dict`로 통합 테스트가 통과함을 확인한다.
7. `NO_FREE_SLOT`, `ERROR`, `QUARANTINED`, 필수 키 누락이 `SUCCESS`로 승격되지 않음을 확인한다.
8. W01 의미론 테스트와 전체 회귀를 재실행한다.

```text
python -m unittest tests.test_worktree_pool -v
python -m unittest tests.test_live_orchestration -v
python -m unittest discover -s tests -q
```

증거에는 명령, exit code, 테스트 수, 변경 파일 SHA-256, patch digest, changed-files, lifecycle 호출 순서를 포함한다.

## 완료기준

- 실제 `WorktreePool` 공개 계약으로 통합 테스트가 통과한다.
- `acquire → 변경 → export → digest 저장 → release` 순서가 관측되고 역전되지 않는다.
- export 이전 discard, 자동 merge, 운영 저장소 자동 discard가 각각 0건이다.
- `slot.name` 같은 object 가정과 `agent_id` 누락 호출이 각각 0건이다.
- 실패/불완전 반환은 fail-closed 상태이며 `SUCCESS`가 아니다.
- W01의 `SIMULATED/UNVERIFIED/NOT_CONFIGURED` 의미론과 전체 회귀가 유지된다.

## 금지사항과 실패기준

다음 중 하나라도 발생하면 `BUILT`가 아니라 `BLOCKED` 또는 `NEEDS_REVIEW`다.

- patch·changed-files·digest 저장 전 `discard=True`
- 사용자 승인 없는 자동 merge, apply, discard
- 실제 `WorktreePool` 대신 Dummy만으로 완료 주장
- acquire 결과에 `slot.name`/`slot.path` 객체 속성 가정
- acquire/release/export에서 `agent_id` 누락 또는 holder 혼용
- 기준 SHA/working-tree hash drift를 무시한 덮어쓰기
- W03 executor, W04 triage 확대, W05 liveness를 함께 구현
- 테스트 실패나 미실행 검사를 통과로 표기

## 잔여 위험과 후속 제안

- `export_changes()`가 반환한 patch의 영속 artifact store 계약은 W03 실행 영수증과 함께 추가 설계가 필요하다.
- 운영 환경에서 discard/reinstate할 주체와 시점은 사용자 승인 경계로 남는다.
- 프로세스 재시작 복구, 다중 프로세스 안전성, Windows open-handle 환경차는 W02 완료기준이 아니다.
- **`Ollama 모델 풀과 백오프`는 기존 W02가 아니다.** W02에 섞지 않으며 W02 `CLOSED` 뒤 별도 작업 ID 후보로만 제안한다.

## Claude Code BUILD → Antigravity VERIFY 전달 카드

```yaml
work_id: W02
title: 실제 WorktreePool 계약 브리지
state: PLAN_LOCKED
owner: codex
source_plan:
  - docs/23_[C3P_통합수행규칙]_단일계획_프로젝트대화창_및_순차스케줄.md
  - docs/22_[C3P_3자절충합의]_실전하네스_다음구현_상세계획서.md
  - docs/28_[C3P_W02_PLAN_LOCKED]_실제_WorktreePool_계약_브리지.md
base_revision: 43ca02763b3e5f7b976d9d00a765941ca2bd20f5 + working-tree SHA-256 snapshot
goal: 실제 WorktreePool Dict 계약과 export-before-release 수명주기를 연결하고 임시 Git 저장소에서 증명한다.
scope:
  files: [central_hub/trinity_orchestrator.py, tests/test_live_orchestration.py, tests/test_worktree_pool.py(조건부)]
  actions: [실제 Dict 계약 호출, fail-closed 검증, export digest, 임시 Git lifecycle 테스트]
non_goals: [WorktreePool 공개 계약 변경, 실제 executor, 자동 merge/discard/apply, Ollama 모델 풀과 백오프, triage/liveness 확대]
acceptance:
  - acquire→변경→export→digest 저장→release 순서 증명
  - export 이전 discard 0건
  - object.name 가정 0건
  - agent_id 누락 0건
  - 실패 상태의 SUCCESS 승격 0건
  - 관련/전체 테스트 exit 0
evidence:
  tests: [명령, 테스트 수, exit code]
  artifacts: [변경 파일 해시, patch digest, changed_files, lifecycle 호출 로그]
risks: [artifact 영속화는 W03 연계, 운영 discard/reinstate는 사용자 승인 필요]
next_owner: claude_code
next_state_on_success: BUILT
handoff_after_built: antigravity는 동일 임시 Git lifecycle을 독립 재현하고 구현 코드를 수정하지 않는다.
```
