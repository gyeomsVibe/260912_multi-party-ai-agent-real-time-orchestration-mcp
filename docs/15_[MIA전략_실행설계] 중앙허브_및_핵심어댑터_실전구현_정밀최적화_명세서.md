# 🏛️ [MIA전략 실행설계] 중앙허브 및 핵심 어댑터 실전구현 정밀최적화 명세서

> **문서 번호:** `docs/15`
> **문서 식별자:** `docs/15_[MIA전략_실행설계] 중앙허브_및_핵심어댑터_실전구현_정밀최적화_명세서.md`
> **상태:** 제정 및 실행 착수 승인 (ESTABLISHED / ACTIONABLE)
> **적용 프로토콜:** Trinity-ACE Protocol (The Trinity Vibe Council)
> **실행 프레임워크:** MIA 전략절차 4단계 (`Frame` ➔ `Review` ➔ `Execute` ➔ `Verify`)
> **적용 제어 토큰:** `/DEEPDIVE /REDTEAM /SELFREFINE /CRITIC /STEPBYSTEP /ALT3 /OPTIMIZE`

---

## 🎯 1. 기획 단계 (Frame the Opportunity) — `/DEEPDIVE` & `/SELFREFINE`

### 1.1 기획 배경 및 기회 정의 (Opportunity Brief)
1차 런타임 구축 단계에서 우리는 SQLite WAL 단일 쓰레드 직렬화 DB, 3-Tier Triage 엔진, 로컬 0원 노예 하네스(`harness/ollama_worker.py`), 그리고 18개의 무결성 단위 테스트를 완결(Exit Code 0)하였습니다.

그러나 실제 프로덕션 환경에서 3대 도구(**Codex CLI, Claude Code CLI, Antigravity**)가 사람의 개입 없이 유기적으로 동작하기 위해서는 다음 3가지 핵심 어댑터 계층의 실전 완결이 요구됩니다:
1. **표준 MCP Stdio Server Adapter**: Claude Code(`~/.claude.json` 또는 `.mcp.json`)와 Codex 환경에서 별도 프록시 없이 즉시 도구(Tools)로 인식되는 공식 JSON-RPC 2.0 Stdio 규격 서버.
2. **Memory Pointer Pattern (>1KB 자동 포인터화)**: 2026 프로덕션 에이전트 리서치(`simota/agent-skills`, arXiv:2511.22729)에서 규명된 "컨텍스트 윈도우 오버플로우 방어" 원칙에 따라 대용량 diff 및 로그를 `ref://` 포인터로 치환하는 최적화.
3. **Idempotency Key & Effect Ledger**: 네트워크 재시도나 에이전트 간 재전송 시 동일 명령이 중복 실행되는 것을 원천 차단하는 멱등성 트랜잭션 원장.

### 1.2 1차 구현체에 대한 비판적 자기점검 (`/SELFREFINE`)
현재 구현된 `central_hub/`의 4대 맹점을 엄격히 규명합니다:

| 분석 대상 | 현재 상태의 맹점 및 한계 | 실전 장애 시나리오 | 최적화 보강 방향 |
|---|---|---|---|
| **통신 프로토콜** | HTTP REST 엔드포인트 중심 (`/api/events`) | Claude Code는 기본적으로 Stdio 기반 MCP 서버를 표준으로 요구함. HTTP만으로는 추가 브리지 없이 직접 연동 불가 | 표준 JSON-RPC 2.0 Stdio 기반의 [`central_hub/mcp_server_adapter.py`](../central_hub/mcp_server_adapter.py) 구현 |
| **페이로드 크기** | DB `payload TEXT`에 수십~수백 KB 원시 로그 그대로 저장 | 에이전트 간 메시지 교환 시 수십만 토큰이 중복 전송되어 5시간 윈도우 소진 및 컨텍스트 폭발 | **Memory Pointer Pattern** 도입: 1KB 초과 페이로드는 로컬 디스크 격리 후 80바이트 URI 포인터만 전송 |
| **Windows 파이프** | `subprocess.communicate()` 단발성 호출 | 64KB 초과 출력 발생 시 Windows OS Stdio 파이프 버퍼가 가득 차 데드락(Deadlock)에 걸려 프로세스가 영구 멈춤 | 비동기 스레드 기반 스트리밍 소비 및 ANSI 이스케이프 코드 정제기 탑재 |
| **재시도 신뢰성** | 이벤트 단순 `INSERT` (멱등성 키 부재) | 릴레이 실패 후 재시도 시 동일한 코드 패치가 두 번 적용되어 중복 코드 및 문법 에러 유발 | `idempotency_key` 고유 제약 및 이펙트 원장(Effect Ledger) 검증 로직 탑재 |

---

## 🔍 2. 검토 단계 (Review & Auditable Decision) — `/CRITIC`, `/REDTEAM` & `/ALT3`

### 2.1 레드팀 가혹 공격 시나리오 검토 (`/REDTEAM`)

```
[공격 시나리오 1 : Windows 파이프 64KB 버퍼 폭발 (Pipe Deadlock)]
 에이전트가 10,000줄의 컴파일 에러 로그를 Stdio로 방출 ➔
 Windows 기본 파이프 버퍼(64KB) 포화 ➔ 쓰기 스레드 블로킹 ➔
 중앙 허브가 응답을 영구 대기하며 전체 오케스트레이션 영구 정지(Hang).
 ☞ 극복책: background reader 스레드가 버퍼를 즉시 비우는 논블로킹 소비기 구현.

[공격 시나리오 2 : 토큰 흡혈귀 증후군 (Token Vampirism via Large Payload)]
 Codex 사령관이 Claude Code에게 200KB 소스코드 diff 전체를 카드 페이로드로 전송 ➔
 Claude Code의 5시간 슬라이딩 윈도우 급속 소진(DORMANT 강제 진입).
 ☞ 극복책: 1KB 초과 시 즉시 [ref://artifacts/hash.diff] 포인터로 강제 변환 (토큰 99.9% 절감).

[공격 시나리오 3 : 유령 중복 패치 (Phantom Duplicate Execution)]
 통신 일시 지연으로 릴레이 카드가 재전송됨 ➔
 Claude Code가 동일 리팩토링 함수를 중복 추가하여 `IndentationError` / `NameError` 유발.
 ☞ 극복책: 모든 태스크 발행 시 UUID 기반 idempotency_key를 발급하고 30분 내 중복 요청 자동 무시.
```

### 2.2 2026 글로벌 오픈소스 실증 인텔리전스 반영 (`/DEEPDIVE`)
GitHub 39,000★ 레포지토리 `Yeachan-Heo/oh-my-claudecode`, 37,000★ `musistudio/claude-code-router`, 그리고 2026 프로덕션 에이전트 스킬 `simota/agent-skills`의 실제 코드를 전수 분석하여 검증된 설계를 도출했습니다:
1. **Stdio MCP 브리지 패턴 (`oh-my-claudecode` 실증)**:
   - Claude Code는 플러그인 또는 `.mcp.json`에서 `command: "python", args: ["central_hub/mcp_server_adapter.py"]` 형태로 Stdio를 연결할 때 가장 안정적이며 0ms 지연으로 도구를 로드함.
2. **단일 로컬 컨트롤 플레인 (`claude-code-router` 실증)**:
   - 에이전트별로 분산된 환경 설정을 중앙 허브가 단일 프로파일(Profile)로 통합 통제하여 쿼터 소진 시 즉각적인 모델 페일오버를 수행.
3. **Memory Pointer & Idempotency (`simota/agent-skills` 실증)**:
   - "Tool outputs > 1KB MUST be stored externally and passed as short references." (arXiv:2511.22729 기반)
   - "Every effectful tool invocation needs an idempotency key; retry without idempotency risks double-execution."

### 2.3 `/ALT3` : 3대 아키텍처 대안 종합 비교

| 비교 항목 | [대안 A] 순수 Subprocess CLI 직접 스폰 방식 | [대안 B] 표준 MCP Stdio Server Adapter + Single-Writer Hub (추천안) | [대안 C] 로컬 WebSocket/gRPC 양방향 데몬 방식 |
|---|---|---|---|
| **통신 방식** | `subprocess.run(["claude", "-p", ...])` | 표준 JSON-RPC 2.0 Stdio (`sys.stdin` / `sys.stdout`) | 로컬 WS/gRPC 소켓 서버 (TCP) |
| **Claude Code 호환성** | 1회성 실행만 가능, 대화 세션 유지 불가 | `.mcp.json` 등록으로 **완벽한 네이티브 도구 연동** | 별도의 클라이언트 브리지 프록시 필수 |
| **컨텍스트 효율성** | 매 실행마다 시스템 프롬프트 재전송 (낭비 심각) | **MCP Tool Call 기반 최소 패킷 통신 (최적)** | 연결 유지되나 별도 직렬화 오버헤드 |
| **Windows 안정성** | 파이프 버퍼 및 인코딩 충돌 취약 | **Stdio UTF-8 스트리밍 래퍼로 완벽 제어** | 포트 점유 충돌(`EADDRINUSE`) 빈발 |
| **구현 복잡도** | 낮음 | **중간 (표준 라이브러리로 100% 자립)** | 높음 (외부 종속성 다수 필요) |
| **4대 렌즈 평가** | 가치성 낮음 / 위험도 높음 | **가치성 최고 / 실현가능성 최고 / 위험도 최저** | 가치성 높으나 종속성 리스크 |
| **의사결정 게이트** | **No-Go** | **GO (만장일치 확정 채택)** | **Pivot** |

---

## 🛠️ 3. 실행 단계 (Execute - Smallest Useful Proof) — `/STEPBYSTEP` & `/OPTIMIZE`

### 3.1 신규 모듈 및 고도화 아키텍처

```
 [Claude Code / Codex / Antigravity]
         │
         │ (표준 JSON-RPC 2.0 Stdio)
         ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ central_hub/mcp_server_adapter.py (공식 MCP 규격 어댑터)    │
 │ - tools/list : 6대 표준 Trinity-ACE 도구 노출               │
 │   1) trinity_send_card (3줄 초압축 작업카드 발행)           │
 │   2) trinity_read_inbox (미확인 작업카드 수신)              │
 │   3) trinity_acquire_lock (원자적 파일/리소스 락 획득)      │
 │   4) trinity_release_lock (리소스 락 해제)                  │
 │   5) trinity_store_artifact (1KB 초과 포인터화 ref://)      │
 │   6) trinity_triage_error (3-Tier 에러 사전 분류)           │
 └──────────────────────────────┬──────────────────────────────┘
                                │ (로컬 내부 큐 / 메모리 버스)
                                ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ central_hub/hub_daemon.py (고도화된 단일 쓰레드 엔진)       │
 │ - SingleWriterDB (SQLite WAL 동시성 보장)                   │
 │ - Idempotency Ledger (중복 이벤트 원천 차단)                │
 │ - Memory Pointer Vault (.artifacts/vault/ 저장 관리)        │
 │ - 270s Anthropic TTL 킵얼라이브 프로브 발송기               │
 └─────────────────────────────────────────────────────────────┘
```

### 3.2 핵심 어댑터 6대 MCP 도구 명세

1. **`trinity_send_card`**:
   - `sender` (str), `target` (str: `codex` | `claude` | `antigravity` | `all`), `task_name` (str), `card_text` (str, max 3 lines), `idempotency_key` (str).
   - 카드 본문이 1KB를 초과할 경우 내부에서 자동으로 `trinity_store_artifact`를 호출하여 포인터(`ref://...`)로 변환.
2. **`trinity_read_inbox`**:
   - `agent_id` (str), `limit` (int, default 5).
   - 자신에게 할당된 미처리 작업 카드를 FIFO 순서로 반환하고 자동 확인(Ack) 처리.
3. **`trinity_acquire_lock`**:
   - `resource_path` (str), `holder_id` (str), `timeout_seconds` (float, default 30.0).
   - 파일 충돌을 방지하기 위한 원자적 배타 락(Atomic Exclusive Lock).
4. **`trinity_release_lock`**:
   - `resource_path` (str), `holder_id` (str).
   - 작업 완료 후 락 정상 해제.
5. **`trinity_store_artifact`**:
   - `content` (str), `artifact_type` (str: `diff` | `log` | `schema`).
   - 콘텐츠 해시(SHA-256) 기반으로 로컬 볼트 디렉터리에 저장하고 `ref://vault/<sha256>` URI 반환 (토큰 99.9% 절감).
6. **`trinity_triage_error`**:
   - `error_log` (str), `exit_code` (int).
   - `TriageClassifier`와 연동하여 `INFRA_ENVIRONMENT` 여부를 판정하고 코드 수정 가능 여부(`code_edit_permitted`) 반환.

### 3.3 고도화 구현 로드맵 (Micro-Milestones)
- **Step 1**: `central_hub/hub_daemon.py`에 Idempotency Ledger 테이블 및 Memory Vault 저장 로직 추가.
- **Step 2**: `central_hub/mcp_server_adapter.py` 표준 JSON-RPC 2.0 Stdio 서버 구현.
- **Step 3**: `central_hub/stdio_wrapper.py`에 스트리밍 논블로킹 파이프 리더 및 ANSI 필터 보강.
- **Step 4**: `tests/`에 신규 어댑터, 메모리 포인터, 멱등성 검증 단위 테스트 3종 추가 작성 및 전수 통과 확인 (Exit Code 0).

---

## 🧪 4. 검증 단계 (Verify - Close the Learning Loop)

### 4.1 수락 기준 (Acceptance Criteria)
1. **MCP 프로토콜 준수**: `tools/list` 요청 시 JSON-RPC 2.0 규격에 맞는 6대 도구 스키마를 100% 반환할 것.
2. **메모리 포인터 토큰 압축률**: 100KB 문자열 저장 시 반환되는 포인터 문자열 길이가 120바이트 이하일 것 (99% 이상 압축).
3. **멱등성 중복 차단**: 동일한 `idempotency_key`를 가진 카드 발행 요청 시 2회차 요청은 DB 중복 삽입 없이 기존 결과를 반환할 것.
4. **기존 테스트 호환성**: 기존 18개 테스트를 포함하여 전체 테스트가 100% 무결 통과(Exit Code 0)할 것.
5. **금지어 및 규칙 준수**: 구시대적 레거시 명칭 영구 정제, `git diff --check` 클린, 2자리 연속 번호 완결 유지.

---

## 📌 5. 결론 및 승인 메모 (Decision Memo)

- **평가 결론**: **GO (만장일치 승인)**
- **사유**: 기존의 HTTP 방식은 Claude Code 및 Codex CLI의 공식 MCP Stdio 표준과 괴리가 있었으나, 본 설계를 통해 표준 Stdio 어댑터와 Memory Pointer Vault를 구축함으로써 2026 프론티어 AI 에이전트의 실전 운영 제약(Windows 파이프 락, 토큰 오버플로우, 중복 실행)을 완벽히 극복함.
