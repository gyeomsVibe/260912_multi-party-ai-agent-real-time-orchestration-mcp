# Antigravity·Claude Code 지정 세션 원문 색인과 정리

## 보관 원칙

이 문서는 지정 세션의 **무가공 원문을 재서술한 문서가 아니다.** 원문은 각 도구가 관리하는 로컬 세션 로그에 유지하고, 이 저장소에는 변조 탐지용 해시, 출처, 범위, 대화 흐름 및 안전하게 정리한 내용 색인만 보관한다. 이 방식은 대화 로그에 포함될 수 있는 도구 출력·로컬 경로·환경 정보·비밀값을 Git 작업공간에 무분별하게 복제하지 않으면서, 동일 원문을 다시 확인할 수 있게 한다.

## 원문 무결성 레지스트리

| 도구 | 지정 세션 ID | 원문 로그 | 기간 | 규모 | SHA-256 |
|---|---|---|---|---:|---|
| Antigravity | `015fe317-fa82-4019-a621-39b53187befd` | `C:\\Users\\Kimyoongyeom\\.gemini\\antigravity\\brain\\015fe317-fa82-4019-a621-39b53187befd\\.system_generated\\logs\\transcript_full.jsonl` | 2026-09-11 16:39:47 ~ 2026-09-13 04:33:10 | 1,210 lines / 3,296,071 bytes | `C7B735B127F13D45A18704A54826D312D27222E0AD252056D517146AD25E31D5` |
| Claude Code | `07193751-7a8c-4c2f-b9e5-506a342af131` | `C:\\Users\\Kimyoongyeom\\.claude\\projects\\D--D-Workspace-NB--agentic-ai-workspace-260912-multi-party-ai-agent-real-time-orchestration-mcp\\07193751-7a8c-4c2f-b9e5-506a342af131.jsonl` | 2026-09-12 05:54:19 ~ 2026-09-13 04:08:27 | 544 lines / 1,624,354 bytes | `4C71755473445F25A1528B4BDB82B5D44D78B082EB9B520BC2B84B8B84AEF4CE` |

재검증은 각 원문 파일에 SHA-256을 다시 계산해 위 값과 비교한다. 값이 다르면 원문이 변경됐거나 다른 파일을 가리키는 것이므로, 기존 분석 인용을 무효로 하고 새 버전을 별도 등록한다.

## Antigravity 원문 정리

### 구조

* 레코드: `USER_INPUT` 19, `PLANNER_RESPONSE` 607, `GENERIC` 557, `CHECKPOINT` 6, `SYSTEM_MESSAGE` 21.
* 대화는 C3P 협의체의 전면 개편 요구에서 출발해, 0~4단계 거버넌스·도구 역할·비용/캐시 전략·Ollama 하네스·E2E 교차 릴레이 설계로 확장된다.
* 원문에 실제로 등장하는 주요 산출물 제목은 다음과 같다.

  - `MIA 전략 심층기획`: 사전조사와 웹 딥리서치에 근거한 7대 구현 난제 극복 계획
  - `1단계 정본 명세서`: Codex 사령관의 모델/비용/KV 캐시 최적화
  - `2단계 정본 명세서`: Claude Code 역할과 5시간 윈도우 운용
  - `3단계 정본 명세서`: Antigravity·Ollama 연동
  - `4단계 정본 명세서`: E2E 실시간 교차 릴레이 및 MIA 파이프라인

### 보관된 설계 주장 색인

| 주제 | 원문에서 전개된 방향 | 후속 검증 위치 |
|---|---|---|
| 협의체 구조 | Codex·Claude Code·Antigravity·Ollama의 역할 분리와 단계적 릴레이 | `docs/17` — 실제 executor/evidence 경계 검증 |
| 비용/컨텍스트 | 플랜별 쿼터, 프롬프트/KV 캐시, 로컬 전처리로 CPST를 낮추려는 전략 | `docs/17` — cache refresh·비용 가설 재검토 |
| 내구성 | checkpoint/replay, idempotency, retry/timeout/circuit breaker | `central_hub/`, `docs/17` — 실제 lifecycle 검증 |
| 구현 근거 | Claude Code Router, Loop Engineering, oh-my-claudecode 등 공개 구현체·커뮤니티 자료 탐색 | `docs/17` — 1차 출처와 현재 규격 대조 |

## Claude Code 원문 정리

### 구조

* 레코드: `user` 98, `assistant` 160, 그리고 세션/첨부/파일 이력/큐 등 보조 레코드 286.
* 대화는 기존 설계·구현의 적대적 재검토에서 시작해, 재현된 결함의 교정·회귀 테스트 추가·Windows worktree pool 구현과 검증으로 진행된다.
* 원문에 실제로 나타나는 완료/상태 표시는 다음과 같다.

  - 초기 교정 후 `25 passed → 64 passed`
  - Step 1 재검증 후 69건 통과
  - Step 2 Worktree Pool 구현 후 88건 통과
  - MIA Stage 1~3 완료, Stage 4는 승인 대기

### 보관된 구현 주장 색인

| 주제 | 원문에서 수행/주장한 내용 | 현재 저장소 대조 |
|---|---|---|
| 실패 경로 | lock, vault traversal, triage, in-memory DB, keep-alive, protocol, spawn concurrency 문제를 재현·교정 | `tests/test_failure_paths.py` 및 88개 표준 라이브러리 테스트로 단품 검증됨 |
| worktree | 고정·재사용 풀, dirty 상태 거부, quarantine 및 Windows 파일 점유 대응 | `central_hub/worktree_pool.py`, `tests/test_worktree_pool.py`에 구현됨 |
| 문서 정정 | “완결” 주장과 미해결 후속의 구분, A-3 배칭/429 차단기 등 잔여 과제 제시 | `docs/16`, `docs/17`에 잔여 위험으로 기록됨 |
| E2E | 통합 릴레이 실행 명세를 제안 | `docs/17`이 실제 external executor와 모의 성공을 분리해 재판정함 |

## 두 원문의 연결 관계

Antigravity 원문은 **전략·역할·비용·릴레이 구상**을 넓게 만들었고, Claude Code 원문은 그 구상 중 일부를 **코드 결함 재현·테스트·국소 구현**으로 내렸다. 둘은 상호 보완적이지만 동일한 수준의 증거는 아니다. 특히 “모의 릴레이 테스트 통과”는 “세 외부 도구가 실제로 연결되어 동작함”의 증거가 아니므로, 이 차이는 [MIA 핵심 보고서](17_[MIA_핵심보고서]_Trinity_ACE_선행분석_비판적_전수검토와_개선설계.md)의 Pivot 결정으로 관리한다.

## 원문 인용·분석의 다음 절차

1. 핵심 주장마다 세션 ID와 원문 레코드 순번을 붙여 `docs/17`에 인용한다.
2. 원문 주장과 현재 코드/테스트/공식 규격을 1:1 대조한다.
3. 판정은 `확인됨`, `부분 확인`, `반증`, `증거 부족` 중 하나로만 기록한다.
4. 원문 전체를 저장소에 복제해야 할 필요가 생기면, 먼저 비밀값·개인정보·도구 출력의 민감 부분을 검토·마스킹한 별도 비공개 아카이브를 사용한다.
