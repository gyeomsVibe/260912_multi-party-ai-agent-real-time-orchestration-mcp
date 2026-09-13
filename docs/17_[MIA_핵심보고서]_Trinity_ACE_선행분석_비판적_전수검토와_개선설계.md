# Trinity-ACE Protocol 선행분석 비판적 전수검토와 개선 설계

## 결론

Trinity-ACE의 **로컬 조정 허브**(작업 카드, 단일 기록자, 임대형 인박스, 잠금, 격리 볼트)는 유의미한 기반을 갖췄다. 그러나 이를 “3대 도구가 실시간으로 자율 협업하는 프로덕션 시스템”으로 선언하기에는 증거가 부족하다. 현 구현의 E2E는 외부 에이전트 실행·패치·검증을 실제로 연결하지 않은 **모의 오케스트레이션**이며, MCP는 그러한 에이전트 웨이크업을 보장하는 스케줄러가 아니다.

권고 결정은 **Pivot (통합 전 실증 우선)** 이다. 중앙 허브를 유지하되, 다음 릴리스의 범위를 “세 도구를 흉내 내는 파이프라인”에서 “각 도구의 실제 실행 계약, 증거, 실패·재개를 갖춘 작업 오케스트레이터”로 좁혀야 한다. 자동 코드 변경·자동 롤백·캐시 유지 데몬은 실증 전에는 비활성으로 둔다.

## 범위와 증거 한계

요청된 Antigravity 대화(`015fe317-fa82-4019-a621-39b53187befd`)와 Claude Code 대화(`07193751-7a8c-4c2f-b9e5-506a342af131`)의 원문은 현재 노출된 앱/탭 및 저장소에서 확인되지 않았다. 따라서 각 대화의 문장·결론을 원문 단위로 인용하거나 특정 도구의 발언으로 귀속하지 않는다.

대신 저장소의 선행 산출물 `docs/04`, `docs/14`, `docs/16`, 현재 `central_hub/`, `harness/`, `tests/`를 1차 내부 증거로 전수 대조했고, 공개 MCP·Anthropic·Git·Claude Code 문서와 GitHub/Reddit 공개 사례를 교차 검토했다. 원문을 제공받으면 아래의 “선행 분석” 행을 발화 단위 주장·근거·반증 표로 재감사해야 한다.

## MIA Stage 1 — 검토 대상의 주장 지도

| 선행 분석에서 반복된 주장 | 현재 판정 | 근거 |
|---|---|---|
| STDIO 클라이언트는 외부 이벤트만으로 자율 웨이크업하지 못하므로 래퍼가 필요하다 | **방향은 타당, 표현은 과장** | MCP STDIO는 클라이언트가 서버를 자식 프로세스로 띄우고 stdin/stdout으로 요청·응답을 교환하는 규격이다. 이는 장기 실행 CLI 세션을 깨우는 스케줄러 계약이 아니다. 별도 worker/queue가 필요하다. [MCP transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) |
| SQLite 다중 쓰기를 Single Writer로 직렬화하면 Windows 락 문제를 없앤다 | **조건부 타당** | 하나의 허브 프로세스 내부 쓰기에는 효과적이다. 하지만 작업자·도구가 DB 파일을 직접 열면 보장되지 않으며, 다중 허브·재시작·영속성은 별도 설계 대상이다. |
| 고정 재사용 worktree가 동적 생성/삭제보다 Windows에 안전하다 | **조건부 타당** | 파일 편집 격리는 worktree가 제공하지만, 공용 포트·DB·시크릿·Git 병합 충돌은 분리하지 않는다. Claude Code도 병렬 세션의 파일 충돌 방지 수단으로 worktree를 권장한다. [Claude Code worktrees](https://code.claude.com/docs/en/worktrees) |
| 270초 로컬 keep-alive가 Claude 캐시를 24시간 유지한다 | **반증됨** | 캐시는 동일 접두사를 포함한 실제 API 요청의 read/write에서만 갱신된다. 로컬 SQLite INSERT는 원격 캐시와 무관하다. 캐시 히트도 비용이 들며 TTL은 요청 시작 시점부터 계산된다. [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) |
| 정규식 3원 분류기로 안전하게 자동 수리할 수 있다 | **부분 타당** | 코드에는 보수적 fallback과 점수제가 추가되어 단순 오분류 위험을 낮췄다. 다만 실제 환경의 오류 분포·정밀도·재현성 지표가 없어 자동 편집 허가의 근거는 아직 없다. |
| MCP 정규화만으로 도구 간 호환성과 보안이 확보된다 | **반증됨** | 프로토콜 정규화는 필요조건일 뿐이다. 로컬 HTTP는 Origin 검증·localhost bind·인증을 요구하며, 프록시가 stdio 자식 프로세스를 띄우는 구조는 입력 검증·최소 권한·감사가 없으면 권한 상승 경로가 된다. [MCP transport security](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports), [MCP security practices](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/tutorials/security/security_best_practices.mdx) |

## MIA Stage 2 — /CRITIC·/REDTEAM 전수 결과

### 1. “E2E 완료”와 실제 실행 경계가 다르다 — P0

`TrinityOrchestrator.execute_cross_relay()`는 카드 발행과 Ollama 호출 외에는 실제 외부 도구를 실행·검증하지 않는다. 비모의 모드의 Claude 단계는 `{"status": "SUCCESS", "output": "Patch verified in sandbox."}`를 직접 만들고, Antigravity 단계는 항상 `VERIFIED_GREEN`, exit code `0`을 반환한다. `TriageClassifier`는 import만 되고 오케스트레이터의 실패 분기에 연결되지 않는다.

이는 통과한 88개 테스트와 모순되지 않는다. 테스트는 이 모의 계약을 검증한다. 따라서 현 상태의 올바른 명칭은 **“로컬 허브 및 모의 교차 릴레이 시험대”**이며, 세 도구의 런타임 결합 증거는 아니다.

### 2. 락 성공 여부를 확인하지 않고 패치 흐름을 계속한다 — P0

Stage 3은 `trinity_acquire_lock` 결과가 `LOCKED` 또는 `ERROR`여도 `APPLIED` 흐름으로 진행한다. 이후 release 실패도 무시한다. 단품 잠금 구현은 개선됐어도, 호출자가 락을 실패-폐쇄(fail closed)로 소비하지 않으면 상호배제는 엔드투엔드에서 무효화된다.

### 3. 상태머신은 관측 상태가 아니라 라벨이다 — P0

`3-ALIVE/2-ALIVE/1-ALIVE`는 Codex·Claude·Antigravity의 헬스체크, 인증 상태, 잔여 쿼터, 작업 재개 가능성을 반영하지 않는다. 실제로는 Ollama 한 번의 fail-fast만 `TWO_ALIVE`로 바꾸며, 복구 전이·지속 실패 예산·재시작 복구·작업 취소가 없다. “현재 살아 있다”와 “다음 작업을 안전하게 수행할 수 있다”를 분리한 상태 모델이 필요하다.

### 4. 작업 전달의 at-least-once 성질과 외부 부작용을 끝까지 묶지 못한다 — P1

인박스 lease와 card-level idempotency는 좋은 출발점이다. 그러나 외부 CLI의 파일 변경·테스트 실행·커밋은 ledger의 트랜잭션 밖에 있다. worker가 패치 후 ACK 전에 죽으면 재전달된 작업이 중복 실행될 수 있고, 재시작 시 실행 중 상태도 없다. 작업 키는 사용자 의도와 대상 revision을 포함한 안정 키여야 하며, 실행 전/후 evidence digest로 완료를 확인해야 한다.

### 5. worktree 풀은 안전하게 구현됐지만 도구 경계에 연결되지 않았다 — P1

`worktree_pool.py`는 dirty slot 거부·quarantine 등 유용한 방어를 제공하지만 MCP tool과 orchestrator 작업 lifecycle에 아직 결합되지 않았다. 또한 worktree는 파일 충돌만 분리한다. 공개 현장 사례도 공용 포트·공용 DB·공유 설정은 별도 충돌원이라고 지적한다. [Reddit 사례](https://www.reddit.com/r/ClaudeAI/comments/1swlxqb/running_parallel_claude_code_agents_on_the_same/)

### 6. 캐시·쿼터 전략의 경제성이 검증되지 않았다 — P1

캐시 refresh는 “1-token probe”가 아니라 캐시되는 전체 접두사 요청이다. Anthropic 문서는 5분 cache write 1.25×, 1시간 write 2×, 일반 cache hit 0.1× 입력 단가를 명시한다. 따라서 큐 깊이, 다음 사용 예상 시각, 접두사 크기, API 과금 환경을 모르면 270초 주기의 손익을 주장할 수 없다. 구독형 CLI의 세션/쿼터 동작도 API 캐시 과금과 동치라고 가정해서는 안 된다. [Anthropic pricing and TTL](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

### 7. 프로토콜의 시간축을 고정값으로 다루면 호환성이 다시 깨진다 — P1

현재 adapter의 2025년 버전 협상과 batch 거부는 진전이다. 다만 MCP 2026-07-28 계열은 transport/lifecycle를 더 바꾸며, 2025-06-18 Streamable HTTP는 POST body에 단일 JSON-RPC 메시지를 요구한다. 버전별 conformance 테스트와 명시적 지원 범위 없이는 “최신 호환”을 선언할 수 없다. [MCP 2025-06-18 transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports), [공식 conformance 논의](https://github.com/modelcontextprotocol/conformance/issues/378)

### 8. 보안 경계는 토큰 보유 여부보다 capability 축소가 먼저다 — P1

artifact path traversal 방어와 bearer 인증은 개선됐다. 하지만 실행 래퍼는 모델이 만든 입력을 하위 CLI/도구 권한으로 변환하는 고위험 경계다. 허브는 허용된 명령·허용된 worktree·허용된 파일 범위·최대 실행시간·최대 출력·네트워크 권한을 job manifest로 고정하고, 외부 입력은 untrusted로 태깅해야 한다. MCP도 server가 받은 입력을 검증하고 최소 권한으로 설계할 책임을 명시한다. [MCP trust model](https://github.com/modelcontextprotocol/modelcontextprotocol/security)

## /ALT3 — 선택지와 결정

| 대안 | 내용 | 장점 | 치명적 한계 | 판정 |
|---|---|---|---|---|
| A. 현 구조 확대 | 현재 Python orchestrator에 도구 호출을 계속 추가 | 빠름 | 모의 성공이 실제 성공으로 보이는 구조를 유지 | No-Go |
| B. 단일 실제 worker + 증거 원장 | 한 작업자만 실제 CLI를 실행하고, job manifest·lease·evidence·재개를 먼저 완성 | 실패 원인과 부작용을 추적 가능, 가장 작은 실증 | 처리량이 낮음 | **Go** |
| C. 다중 worker/분산 큐 | Redis/Celery 등으로 수평 확장 | 처리량·복구성 | 아직 안정되지 않은 실행 계약과 위험을 증폭 | Research More |

## MIA Stage 3 — 권고 설계와 실행 카드

### 목표 아키텍처

`Planner → Durable Job Ledger → Dispatcher(동시성 1) → Tool-specific Worker → Verifier → Evidence Store → Human Gate`

MCP는 worker가 쓰는 도구 인터페이스로 한정한다. 허브의 일은 에이전트를 “깨우는” 것이 아니라, 등록된 worker가 pull하거나 안전한 hook으로 등록하는 **명시적 job lifecycle**을 관리하는 것이다. Claude Code는 이미 worktree와 hook/agent-team이라는 별도 병렬화·수명주기 기능을 제공하므로, 이를 우회하는 임의 subprocess 프로토콜보다 실제 지원 계약을 먼저 검증해야 한다. [Claude Code hooks](https://code.claude.com/docs/en/hooks-guide), [Claude Code parallel agents](https://code.claude.com/docs/en/agents)

### 순차 실행 계획

1. **P0 — 진실한 실행 결과**: `mock_mode=False`에서 고정 성공을 제거한다. Claude/Antigravity adapter가 없거나 증거를 반환하지 않으면 `NOT_CONFIGURED` 또는 `UNVERIFIED`로 종료한다. lock 비획득 시 worker를 기동하지 않는다.
2. **P0 — Job manifest와 증거 스키마**: `job_id`, base commit SHA, 허용 worktree, 파일 범위, 명령 allowlist, timeout, retry budget, executor identity, stdout/stderr digest, test command/exit code, artifact digest를 원자적으로 기록한다.
3. **P0 — 단일 실제 worker PoC**: Claude 또는 Codex 중 하나만 선택해 worktree에서 읽기 전용 분석 → 제한된 patch → 테스트 → diff/evidence 반환을 수행한다. 자동 merge·자동 rollback은 제외한다.
4. **P1 — 실패/재개 정책**: lease 만료, process crash, timeout, 429/quota, human-question을 별도 상태로 모델링한다. UNKNOWN triage는 항상 사람 검토이며 재시도는 실행 예산을 차감한다.
5. **P1 — 격리 강제**: worktree pool을 dispatcher에 연결하고 slot별 포트·temp·DB namespace를 부여한다. 공용 interface 변경은 단일 owner와 human gate로 직렬화한다.
6. **P1 — 프로토콜/보안 검증**: 지원 MCP revision을 고정하고 해당 revision별 conformance/negative test를 CI에 넣는다. localhost HTTP는 Origin allowlist, 인증, request-size/timeout/rate limit을 적용한다.
7. **P2 — 다도구 확장**: 두 번째 도구(Claude/Codex)와 Antigravity 검증기를 추가하되, 각각 독립 adapter contract test와 실제 evidence 예제가 통과한 경우에만 dispatch 대상으로 등록한다.

## MIA Stage 4 — 검증 기준

| 검증 게이트 | 합격 기준 |
|---|---|
| 실제 실행성 | 비모의 작업이 실제 executor version, base SHA, diff digest, 테스트 exit code를 남긴다. 누락 시 성공 금지. |
| 상호배제 | 동일 file scope job 8개 경쟁에서 worker 기동은 정확히 1개이며, lock 실패 작업은 command 실행 기록이 0건이다. |
| 재개 안전성 | patch 후 crash / ACK 전 crash을 주입해도 같은 base SHA에 중복 side effect가 없고, 상태가 `NEEDS_REVIEW` 또는 idempotent completion으로 수렴한다. |
| 캐시 경제성 | 30일 표본에서 cache-read/write, 접두사 토큰, task success, 지연, quota를 수집해 probe 없는 기준선보다 CPST가 낮음을 보인다. |
| 보안 | 경로 탈출, prompt-injected command, 허용 범위 밖 파일, localhost 외부 Origin, oversized payload가 모두 fail closed이며 비밀값은 로그에 없다. |
| 도구 결합 | 각 도구의 실제 adapter contract test와 1개 이상의 사람이 재현 가능한 E2E 영수증이 있어야 “3-Alive” 표기를 허용한다. |

## 현재 검증 사실

* `python -m unittest discover -s tests -v`를 실행해 **88 tests, OK**를 확인했다.
* `python -m pytest tests -q`는 현재 `C:\Python314\python.exe`에 pytest가 설치되지 않아 실행되지 않았다. 의존성 설치는 요청 범위가 아니므로 수행하지 않았다.
* 위 테스트 성공은 단품 방어와 모의 릴레이에 대한 증거다. 실제 Codex·Claude Code·Antigravity의 상호 호출·코드 변경·E2E UI 검증 증거는 아니다.

## 최종 권고

다음 구현은 P0의 “단일 실제 worker + fail-closed evidence”까지만 승인하는 것이 적절하다. 이 게이트가 통과하기 전에는 3-Alive, 무결점, 24시간 상시가동, 98.5% 절감 같은 성능·가용성·비용 주장을 문서와 UI에서 제거하거나 **가설**로 낮춰야 한다. 공개 Reddit 사례는 worktree가 병렬 편집 충돌 완화에는 유효하되 공유 자원과 병합 책임을 없애지 않는다는 현장 신호를 준다. [Reddit worktree 사례](https://www.reddit.com/r/ClaudeCode/comments/1rr7vgo/i_reverseengineered_claude_code_to_build_a_better/)
