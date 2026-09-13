# 🔴 [MIA 전략 적대적 재검토] 웹 딥리서치 기반 구현난제 실측 검증 및 잔존 결함 최적화 설계서

> **문서 번호:** `docs/16`
> **상태:** 제정 · 식별 결함 교정 완료 · W-1 구현 완료 (PATCHED / VERIFIED) — 단, A-3 후속(배칭·429 차단기)은 **미해결**
> **스킬 프레임워크:** MIA 전략절차 (`Frame ➔ Review ➔ Execute ➔ Verify`)
> **적용 모드:** `/CRITIC` `/REDTEAM` `/SELFREFINE` `/DEEPDIVE` `/ALT3` `/OPTIMIZE`
> **검토 대상:** `docs/03`~`docs/15` 전 문서 + `central_hub/` `harness/` `tests/` 실제 구현체
> **검증 방식:** 정적 독해가 아닌 **실제 파이썬 실행 재현(Executed Repro)** + 웹 딥리서치 대조
> **기준선:** `python -m pytest tests -q` ➔ **25 passed** (Exit Code 0). 즉 **전 테스트 통과 상태에서도 아래 결함이 전부 살아 있음.**

---

## 🧭 Stage 1. 기획 (Frame) — 이 문서가 존재해야 하는 이유

`docs/14`는 7대 구현난제를 "완벽히 해결했다"고 선언했고 `docs/15`는 이를 구현했다고 선언했습니다.
`/CRITIC` `/REDTEAM` 모드로 **선언과 코드를 1:1 대조**한 결과 다음 구조적 문제가 관찰되었습니다.

> **핵심 관찰: 테스트 25건은 "설계가 의도한 경로"만 검증하고, "설계가 방어하겠다고 선언한 실패 경로"는 단 한 건도 검증하지 않습니다.**
> 락 경합·동시성·오분류·신뢰 경계(trust boundary)를 재현하는 테스트가 0건이므로, 녹색 신호가 무결성의 증거가 되지 못합니다.

* **결정 과제**: 잔존 결함을 실측으로 확정하고 근거 기반 최소 교정 설계를 확정한다.
* **Non-goal**: 이 문서 단계에서 코드를 수정하지 않는다 (P2 승인 게이트 준수).
* **Success Signal**: 결함 1건마다 ① 재현 방법 ② 관측된 실제 출력 ③ 교정 설계 ④ 회귀 테스트 정의가 갖춰질 것.

---

## 🔍 Stage 2. 검토 (Review) — 실측 확정 결함 (CONFIRMED, 실행 재현 완료)

### 🔴 C-1. `resource_locks` 상호배제 완전 붕괴 — 3개 에이전트가 동일 파일 락을 **모두** 획득

가장 치명적입니다. `docs/14`가 "파일 쓰기 충돌은 Single-Writer Mutex로 방어"한다고 선언한 **바로 그 잠금장치가 잠기지 않습니다.**

* **원인**: `central_hub/mcp_server_adapter.py`의 `_handle_acquire_lock`이 `SELECT`(점유 확인) ➔ `INSERT ... ON CONFLICT DO UPDATE`(등록)라는 **Check-Then-Act(TOCTOU) 2단계**로 구성됨. 두 단계 사이에 실행권이 양보되면 모든 경합자가 "비어 있음"을 읽고 전원 `ACQUIRED`를 반환합니다. 게다가 `DO UPDATE`는 **타인의 락을 무조건 덮어쓰는 강제 탈취문**입니다.
* **관측된 실제 출력**:

```
lock race result: [('agent2','ACQUIRED'), ('agent1','ACQUIRED'), ('agent0','ACQUIRED')]
final holder:     [('agent0',)]
```

  → 3개 에이전트가 전부 "내가 `src/app.py`의 단독 소유자"라고 믿고 동시 편집에 진입합니다.

* **교정 설계**:
  1. 획득을 **단일 원자 구문**으로 축약: `INSERT INTO resource_locks ... ON CONFLICT(resource_path) DO NOTHING`.
  2. `execute_write` 결과에 `cursor.rowcount`를 실어 반환하고, `rowcount == 1`일 때만 `ACQUIRED`, `0`이면 `LOCKED`.
  3. 재진입(동일 holder 재획득)은 `DO UPDATE ... WHERE holder_id = excluded.holder_id`로 한정.
  4. **락 TTL(lease) 추가**: `acquired_at`을 기록만 하고 아무도 읽지 않아, 죽은 에이전트가 영구 데드락을 만듭니다. `acquired_at < now - lease_seconds` 조건부 탈취와 주기적 lease 갱신을 도입.
* **회귀 테스트**: 스레드 8개 동시 `acquire` ➔ `ACQUIRED` 카운트가 정확히 1이어야 함.

### 🔴 C-2. `MemoryVault` 경로 탈출(Path Traversal) — 볼트 밖 임의 파일 쓰기

LLM이 채우는 도구 인자가 파일 경로에 **무검증 연결**됩니다.

* **원인**: `store()`가 `filename = f"{sha}.{artifact_type}"`로 `artifact_type`을 그대로 이어붙여 `os.path.join`에 투입합니다.
* **관측된 실제 출력** (`artifact_type='txt/../../../ESCAPED.txt'`):

```
ref://vault/<sha>.txt/../../../ESCAPED.txt
escaped exists: True        ← 볼트 디렉터리 바깥에 실제 파일이 생성됨
```

* **위험 등급**: 높음. `trinity_store_artifact`는 협의체 에이전트가 호출하는 도구이며, 프롬프트 인젝션 한 줄로 임의 경로 쓰기가 성립합니다 (`docs/14` 6번 "보안" 항목의 정면 위반).
* **교정 설계**: `artifact_type`을 `^[A-Za-z0-9]{1,16}$` 화이트리스트로 강제하고, 최종 경로에 `os.path.realpath` 기반 **봉쇄 검사**(`commonpath(vault_dir, target) == vault_dir`)를 추가. `retrieve()`의 `ref_uri` 파싱(`(.+)`)에도 동일 검사를 적용.

### 🔴 C-3. 트리아지 분류기 역전 — 진짜 버그는 "고치지 말라", 미지의 오류는 "고쳐도 된다"

`docs/14` 5번 난제(헛바퀴 루프 차단)의 목적을 **정확히 반대로** 달성합니다.

* **원인 A (오탐)**: `INFRA_PATTERNS`가 최우선이고 `timed out`이 전체 로그 대상 부분일치입니다. 결제 테스트의 단정 실패 로그에 "timed out" 한 단어만 섞이면 인프라로 확정되어 수리가 금지됩니다.
* **원인 B (미탐)**: 어떤 패턴에도 걸리지 않으면 **fallback이 `CODE_DEFECT` + `code_edit_permitted=True`** 입니다. 정체불명 인프라 장애가 곧장 "코드를 고쳐라"로 라우팅됩니다 — 막겠다고 선언한 바로 그 시나리오입니다.
* **관측된 실제 출력**:

```
pytest timeout assert -> INFRA_ENVIRONMENT  edit=False  match='timed out'   ← 진짜 버그인데 수리 금지
unknown error         -> CODE_DEFECT        edit=True   match=None          ← 미지 오류인데 수리 허가
exit0 with log        -> UNKNOWN            edit=False                      ← 에러 로그 무시
```

* **교정 설계**:
  1. **첫 일치 승리(first-match-wins) 폐기 ➔ 가중 점수제**. 세 카테고리를 모두 스캔해 점수를 합산하고 최고점을 채택, 동점은 `MANUAL_INSPECTION`.
  2. **위치 가중**: 전체 로그가 아니라 **마지막 traceback 프레임 / 마지막 20줄**에 2배 가중.
  3. **CODE 우선권 규칙**: `AssertionError` · `FAILED (failures=n)`이 존재하면 인프라 단어가 섞여 있어도 `CODE_DEFECT`로 승격 (테스트 러너가 거기까지 도달했다는 것 자체가 환경 정상의 증거).
  4. **안전한 fallback 반전**: 미분류 ➔ `UNKNOWN` + `MANUAL_INSPECTION` + `code_edit_permitted=False`.
  5. `exit_code == 0`이라도 `error_log`가 있으면 분류를 수행.

### 🔴 C-4. `:memory:` 기본값에서 허브가 **즉시 붕괴** (기본 설정이 곧 고장)

`SingleWriterDB`와 `CentralHubDaemon`의 기본 `db_path`가 `":memory:"`입니다.

* **원인**: `_init_db`, `_writer_loop`, `execute_read`가 **각각 별도의 `sqlite3.connect()`** 를 엽니다. `:memory:`는 연결마다 완전히 독립된 DB이므로, 스키마를 만든 연결·쓰는 연결·읽는 연결이 서로 다른 빈 데이터베이스입니다.
* **관측된 실제 출력**:

```
write: {'status':'ERROR','error':'no such table: message_bus'}
read : sqlite3.OperationalError: no such table: message_bus
```

* **테스트가 못 잡은 이유**: `tests/test_hub_daemon.py`가 항상 `tempfile` **파일 경로**만 주입합니다. 기본 인자 경로는 커버리지 0%입니다.
* **교정 설계**: `:memory:`를 `file:trinity_bus?mode=memory&cache=shared`(`uri=True`) 공유 캐시로 치환하고 프로세스 수명 동안 유지 연결 1개를 고정. 또는 기본값을 파일 경로로 바꾸고 `:memory:` 입력 시 명시적 `ValueError`로 조기 실패시킵니다.

---

## 🩸 Stage 2-B. 설계 전제 자체의 오류 (웹 딥리서치 대조 결과)

### 🟠 A-1. "270초 Keep-Alive 프로브"는 프롬프트 캐시를 **전혀 갱신하지 않습니다** (개념적 무효)

`docs/14` 4번 난제의 핵심 해법이 코드 레벨에서 공회전합니다.

* **코드 실태**: `hub_daemon._keep_alive_worker`가 하는 일은 **로컬 SQLite에 `CACHE_KEEP_ALIVE_PROBE` 행 1건 INSERT**가 전부입니다. Anthropic/Google API로 나가는 요청이 없으므로 원격 캐시 TTL 타이머는 1초도 갱신되지 않습니다.
* **딥리서치 대조**: 캐시 TTL 갱신은 **동일 캐시 접두사를 그대로 재전송한 실제 API 요청**이 있을 때만 발생하며, 그 요청은 cache-read 단가(입력가의 약 10%)로 **과금**됩니다. `docs/14`가 쓴 "1토큰짜리 더미 프로브"라는 표현은 성립하지 않습니다 — 접두사 전체가 매번 재전송되어야 하고 비용은 접두사 크기에 비례합니다.
* **경제성 역전**: 공개 분석이 제시하는 손익분기는 **약 62.5분**입니다. 다음 실사용까지 그보다 오래 남았다면 프로브를 돌리는 쪽이 **더 비쌉니다**. 24시간 무조건 270초 주기(하루 약 320회 유료 요청)는 절감이 아니라 **상시 과금 장치**가 될 수 있습니다.
* **추가 치명점 (구독 요금제)**: Claude Code Pro·Codex Plus는 API 키 과금이 아니라 **세션 쿼터** 기반입니다. 쿼터를 소모하는 프로브를 24시간 자동 발사하는 것은 `docs/11`의 5시간 슬라이딩 윈도우 보존 목표와 **정면 충돌**합니다.
* **교정 설계 (`/ALT3`)**:

| 대안 | 내용 | 판정 |
|---|---|---|
| A. 현행 유지 | 270초 무조건 프로브 | ❌ 무효(로컬 INSERT일 뿐) + 실제 구현 시 쿼터 역효과 |
| B. **적응형 프로브** | 작업 큐에 대기 카드가 있고 "다음 사용 예상시각 < 손익분기"일 때만 프로브 | ✅ **권고안** |
| C. 1시간 TTL 전환 | 쓰기 단가 약 2배를 감수하고 장수명 캐시 사용 | △ API 키 직접 과금 환경에서만 검토 가치 |

  더불어 `keep_alive_interval`을 **설정값으로 외부화**하고 기본을 `DISABLED`로 두는 것이 P2 안전측입니다.

* **부수 피해**: 이 프로브 행은 `target='ALL'`이라 `trinity_read_inbox`가 **전부 업무 카드로 수신**합니다. 하루 약 320건의 노이즈 카드가 에이전트 인박스를 점거하고 `message_bus`는 무한 증식합니다(가지치기 로직 부재).

### 🟠 A-2. MCP 프로토콜 버전이 레거시(`2024-11-05`)로 고정 + 협상 미구현

* **딥리서치 대조**: 공개된 MCP 개정 순서는 `2024-11-05`(Legacy) ➔ `2025-03-26` ➔ `2025-06-18`(Stable) ➔ `2025-11-25` ➔ `2026-07-28`입니다. 어댑터는 최초 버전에 고정되어 있습니다.
* **결함**: `initialize` 핸들러가 클라이언트의 `params.protocolVersion`을 **읽지도 않고** 자기 상수를 통보합니다. 최신 클라이언트가 상위 버전을 요청해도 협상이 일어나지 않으며, structured output·resource links 등 최신 기능을 사용할 수 없습니다.
* **교정 설계**: `SUPPORTED = ["2025-11-25","2025-06-18","2025-03-26","2024-11-05"]` 목록을 두고, 클라이언트 요청 버전이 지원 목록에 있으면 그대로 반향(echo)하고 없으면 최신 지원 버전을 제시합니다. HTTP 전송 도입 시 `MCP-Protocol-Version` 헤더 처리를 추가하고, **JSON-RPC 배치는 2025-06-18에서 제거**되었으므로 배치 수신 시 명시적으로 거부합니다.

### 🟠 A-3. Headless Subprocess 웨이크업이 캐시·쿼터 전략을 자기모순으로 무효화

* **모순**: `docs/14` 1번(이벤트마다 `claude -p` 신규 기동)과 4번(프롬프트 캐시 보존)은 양립하지 않습니다. 매 기동은 **새 세션 = 새 캐시 쓰기**이며, 캐시 쓰기는 읽기보다 비싼 프리미엄 단가(5분 TTL 기준 약 1.25배)입니다. 이벤트가 잦을수록 비용이 선형 증가합니다.
* **코드 결함**: `stdio_wrapper.HeadlessAgentRunner`에 **동시 실행 상한이 없습니다**. 이벤트 폭주 시 N개의 CLI 프로세스가 동시 기동되어 쿼터를 한 번에 소진시킵니다 (`docs/07`의 서킷 브레이커와 미연결).
* **교정 설계**: ① 전역 세마포어(기본 1, 최대 2)로 동시 기동 상한 강제. ② 이벤트 **배칭 윈도우**(예: 5초)로 N건의 카드를 1회 기동에 합쳐 처리. ③ 429/쿼터 소진 신호를 감지해 `DORMANT`로 전이하는 서킷 브레이커를 래퍼에 실제 연결.
* **부속 버그**: `TimeoutExpired` 처리에서 `process.kill()` 후 `communicate()`를 호출하지 않아 Windows에서 파이프 핸들이 잔류합니다. `kill()` ➔ `communicate()` ➔ 결과 반환 순서로 교정합니다.

---

## ⚙️ Stage 3. 실행 설계 (Execute) — 중위험 잔존 결함 및 교정

| # | 위치 | 결함 | 교정 설계 |
|---|---|---|---|
| M-1 | `hub_daemon.execute_write` | `response_event.wait(timeout=10)` 만료 시 **빈 dict `{}`** 반환 ➔ 호출부 `res.get("status")`가 `None`이 되어 **조용한 데이터 유실**. HTTP는 그대로 200 응답 | 타임아웃 시 `{"status":"TIMEOUT"}` 명시 반환, `publish_event` 실패 시 HTTP 503 |
| M-2 | `hub_daemon.execute_read` | 호출마다 새 연결, `busy_timeout`·WAL 프라그마 미적용 ➔ Windows에서 쓰기 중 읽기가 `database is locked`로 실패 가능 (막겠다던 바로 그 오류) | 스레드 로컬 읽기 연결 재사용 + `PRAGMA busy_timeout=10000` 적용 |
| M-3 | `hub_daemon.start` | `HTTPServer`(단일 스레드) 사용 ➔ 3개 에이전트 동시 폴링이 직렬화, 느린 요청 1건이 버스 전체를 정지 | `ThreadingHTTPServer`로 교체 (`daemon_threads=True`) |
| M-4 | `hub_daemon` HTTP 엔드포인트 | **인증 전무**. `docs/14` 6번이 선언한 "Ephemeral Secret Bearer Token"이 코드에 없음. 로컬 임의 프로세스가 `POST /api/events`로 업무 카드를 위조 주입 가능 = **에이전트 프롬프트 인젝션 관문** | 기동 시 난수 토큰 생성 ➔ `Authorization: Bearer` 상수시간 비교 강제, 토큰은 접근 제한 파일로 전달 |
| M-5 | `_handle_read_inbox` | 읽는 즉시 ACK(at-most-once). 에이전트가 수신 후 죽으면 카드 **영구 소실**, 재전달 없음 | 가시성 타임아웃 도입: 읽기 시 `leased_until` 설정, 완료 시 명시적 `trinity_ack_card` 호출로 확정 |
| M-6 | `_handle_send_card` | 멱등성 원장이 **카드 INSERT 이후**에 기록되고 예약 단계가 없음 ➔ 동일 키 동시 호출 시 카드 2건 중복 삽입 | `INSERT OR IGNORE`로 키를 **선점 예약**한 뒤 성공한 호출만 카드 발행 |
| M-7 | `mcp_server_adapter.run_stdio` | 한국어 Windows 기본 인코딩(cp949)에서 `sys.stdin/stdout` 사용 ➔ 비ASCII JSON-RPC 손상, 개행 변환으로 프레이밍 손상 | 기동 시 `sys.stdin/stdout.reconfigure(encoding='utf-8', newline='\n')` 강제 |
| M-8 | `message_bus` / `inbox_acks` | 가지치기·인덱스 부재. `LEFT JOIN` 인박스 조회가 테이블 성장에 비례해 상시 열화 | 보존기간 기반 정리 작업 + `(target_id, message_id)` 인덱스 추가 |
| M-9 | `tests/` | 실패 경로 테스트 0건 (락 경합·경로 탈출·오분류·`:memory:`·동시성 전무) | C-1~C-4 재현 케이스를 **회귀 테스트로 먼저 작성(Red)** 한 뒤 교정(Green) |

---

## 🗺️ Stage 4. 검증 로드맵 (Verify) — 승인 후 실행 순서

```
[P0 즉시 차단]           [P1 설계 정정]              [P2 강건화]
• C-1 락 원자화           • A-1 프로브 적응형 전환      • M-1~M-3 동시성/오류전파
• C-2 경로 봉쇄           • A-2 MCP 버전 협상          • M-5~M-6 전달보장·멱등
• C-3 트리아지 반전       • A-3 동시기동 상한/배칭      • M-7~M-8 인코딩·인덱스
• C-4 :memory: 조기실패                                • M-4 Bearer 인증
        ↓                        ↓                          ↓
  회귀테스트 선작성(Red) ──► 최소 패치 ──► pytest Exit 0 ──► E2E 실측 영수증
```

**수용 기준 (Acceptance Criteria)**

1. 8스레드 동시 락 획득 시 `ACQUIRED` 정확히 1건.
2. `artifact_type`에 `../` 주입 시 볼트 밖 파일 생성 0건 + 명시적 예외.
3. 단정 실패 로그에 `timed out`이 섞여도 `CODE_DEFECT` 판정.
4. 미분류 오류의 `code_edit_permitted`가 `False`.
5. 기본 인자(`:memory:`)로 생성 시 즉시 명시적 실패 또는 정상 동작.
6. 전체 `pytest` Exit Code 0 유지 (기존 25건 무회귀).

---

## 📢 요약 브리핑

> **🧒 ELI10**: 테스트 25개가 전부 초록불이라 시스템이 멀쩡해 보였습니다. 그런데 실제로 돌려보니 ① 문을 잠그는 자물쇠가 **세 사람에게 동시에 "네가 주인"이라고 말하고**, ② 창고에 물건 넣는 통로로 **창고 바깥에 파일을 만들 수 있고**, ③ 진짜 버그는 "손대지 마"라 하고 정체불명 오류는 "고쳐"라고 **정반대로** 지시하고, ④ 기본 설정으로 켜면 **아예 켜지지 않고**, ⑤ 기억이 안 지워지게 4분마다 찌른다던 장치는 **사실 자기 노트에 낙서만 하고 있었습니다**. 초록불은 "좋은 길만 확인했다"는 뜻이었을 뿐입니다.

> **🧑‍💻 Expert**: 25/25 녹색 상태에서 실행 재현으로 4건의 결함을 확정했습니다 — `resource_locks`의 TOCTOU로 인한 상호배제 실패(경합 3/3 전원 ACQUIRED), `MemoryVault`의 무검증 `artifact_type` 연결로 인한 디렉터리 탈출 쓰기, 트리아지의 first-match-wins + INFRA 최우선 + CODE fallback 조합이 만든 판정 역전, `:memory:` 기본 경로의 다중 연결 격리로 인한 스키마 부재. 추가로 설계 전제 3건이 무효 또는 자기모순입니다: keep-alive 프로브가 원격 API를 호출하지 않아 TTL 갱신 효과가 0이며(유효화하더라도 약 62.5분 손익분기 및 구독 쿼터 소모와 충돌), MCP 버전이 레거시 고정에 협상 미구현이고, 이벤트별 CLI 재기동은 캐시 보존 목표와 상충하며 동시 기동 상한이 없습니다. 교정은 회귀 테스트 선작성 후 P0➔P2 순으로 진행합니다.

---

## 📎 참고 자료 (웹 딥리서치 출처)

* [Anthropic Prompt Cache TTL + Cost Mechanics — Brandon Wie](https://brandonwie.dev/posts/anthropic-prompt-cache-ttl)
* [Tokenomics: the 62.5-minute rule for Claude's cache — Ryan Skidmore](https://skids.dev/blog/anthropic-cache-tokenomics/)
* [Claude Prompt Caching Pricing: 5-Min vs 1-Hour Cache (2026) — Respan](https://www.respan.ai/articles/claude-prompt-caching)
* [Cache TTL regression 1h ➔ 5m · anthropics/claude-code Issue #46829](https://github.com/anthropics/claude-code/issues/46829)
* [MCP Specification Version Timeline — hidekazu-konishi.com](https://hidekazu-konishi.com/entry/mcp_specification_version_timeline.html)
* [MCP 2025-06-18 Spec Update — ForgeCode](https://forgecode.dev/blog/mcp-spec-updates/)
* [modelcontextprotocol/modelcontextprotocol Releases](https://github.com/modelcontextprotocol/modelcontextprotocol/releases)
* [Multi-Agent Orchestration: Running 10+ Claude Instances in Parallel — DEV](https://dev.to/bredmond1019/multi-agent-orchestration-running-10-claude-instances-in-parallel-part-3-29da)


---

## ✅ Stage 4 검증 결과 (VERIFIED) — 2026-09-12 교정 실행 기록

### 테스트 기준선 변화

| 항목 | 교정 전 | 교정 후 |
|---|---|---|
| 통과 테스트 | 25 passed | **64 passed, 9 subtests** |
| 실패 경로 테스트 | 0건 | **39건 신규** (`tests/test_failure_paths.py`) |
| Exit Code | 0 | **0** |

`tests/test_failure_paths.py`는 패치 전 실행 시 **32 failed / 4 errors**(Red)였고, 패치 후 전량 Green입니다.

### 재현 결과 대조

| ID | 교정 전 관측 | 교정 후 관측 |
|---|---|---|
| C-1 | `['ACQUIRED','ACQUIRED','ACQUIRED']` | `['ACQUIRED','LOCKED','LOCKED']` |
| C-2 | `escaped exists: True` | `ValueError: Invalid artifact_type ...` |
| C-3 (단정+timeout) | `INFRA_ENVIRONMENT` / edit=False | `CODE_DEFECT` / edit=True |
| C-3 (미분류) | `CODE_DEFECT` / edit=True | `UNKNOWN` / edit=False |
| C-4 | `no such table: message_bus` | `OK` |
| A-2 | `2024-11-05` 고정 | `2025-11-25` (요청 버전 협상) |

### 변경 파일

* `central_hub/hub_daemon.py` — C-4 공유캐시 DSN, M-1 상태 전파, M-2 읽기 연결 풀, M-3 `ThreadingHTTPServer`, M-4 Bearer 인증, M-8 인덱스, A-1 프로브 기본 비활성·정직 라벨
* `central_hub/mcp_server_adapter.py` — C-1 원자적 락 + lease, C-2 경로 봉쇄, M-5 리스 기반 인박스 + `trinity_ack_card`, M-6 선점 예약 멱등성, M-7 UTF-8 stdio, A-2 버전 협상 + 배치 거부
* `central_hub/triage_classifier.py` — C-3 가중 점수제 + 꼬리 가중 + CODE 우선권 + fail-safe 반전
* `central_hub/stdio_wrapper.py` — A-3 동시 기동 세마포어, 타임아웃 후 파이프 드레인
* `harness/ollama_worker.py` — O-1 파싱 실패 시 원본 유출 차단, O-2 예외 범위 축소, O-3 정규화 예외명 파싱
* `tests/test_failure_paths.py` (신규), `tests/test_hub_daemon.py` · `tests/test_mcp_adapter.py` · `tests/test_ollama_worker.py` (계약 변경 반영)

### 교정 중 추가 발견 (교정 완료)

* **X-1**: M-2의 초기 구현(스레드로컬 읽기 연결)은 `sqlite3`가 교차 스레드 `close()`를 거부하므로 연결이 실제로 닫히지 않았고, Windows에서 `WinError 32`로 임시 디렉터리 정리가 실패했습니다. 막으려던 결함을 재생산한 사례입니다. → **연결 풀(`check_same_thread=False`) + 전량 명시적 close + 실패 시 예외 표면화**로 교정.
* **X-2**: HTTP/1.1 keep-alive에서 401 응답 전 요청 본문을 읽지 않아 다음 요청이 파싱 오류를 일으켰습니다. → **401 전 본문 드레인** 추가.

### 잔존 미해결 (승인 필요 범위 밖)

* **W-1**: `docs/14` 3번 난제 **Static Recycled Worktree Pool**은 여전히 구현체가 없습니다. 문서상 해결, 코드상 미착수 상태이며 별도 작업 단위가 필요합니다.
* **A-3 배칭 윈도우**: 동시 기동 상한은 적용했으나 이벤트 배칭과 429 서킷 브레이커 연결은 허브 런루프가 실제 CLI를 기동하는 단계에서 구현해야 합니다.


---

## 🔁 2차 자기검증 (Step 1, 2026-09-13) — 교정본에 대한 /REDTEAM

1차 교정본을 다시 실행 공격한 결과, **교정이 새 결함을 만든 사례**가 확인되어 교정했습니다.

| ID | 결함 | 관측 | 교정 |
|---|---|---|---|
| T-1 | C-3 교정의 CODE 우선권 패턴 `\d+ failed`가 너무 느슨함 | `Retry 3 failed: Connection refused` → `CODE_DEFECT` edit=True (**C-3 역전 재발**) | pytest 요약 배너만 인정 + 꼬리에 **강한** 인프라 서명이 있으면 `INFRA` 판정 |
| T-2 | 허브 기동 시 Bearer 토큰을 stdout에 출력 | 비밀값이 로그·대화기록에 남음 (P2 위반) | `~/.trinity_hub_token`(0600)에 기록하고 경로만 출력 |
| T-3 | M-6 선점 예약이 호출자 크래시 시 `IN_FLIGHT`로 영구 고착 | 해당 키의 모든 재시도가 영구 차단 | 120초 초과 예약은 회수 후 재발행 |
| T-4 | 락 lease(900초) 갱신 방법이 문서화되지 않음 | 15분 넘는 작업이 탈취될 수 있음 | 도구 설명에 "같은 holder_id로 재획득 시 갱신" 명시 |
| T-5 | 본 문서 상태를 "전량 교정 완료"로 과장 | W-1·A-3 후속이 남아 있음 | 상태 문구 정정 |

**검증**: `python -m pytest tests -q` → **69 passed, 11 subtests** (Exit 0). T-1·T-3 회귀 테스트 5건을 추가했습니다.

**교훈**: 오분류를 막는 규칙은 반대 방향의 오분류를 만들기 쉽습니다. 트리아지 규칙을 바꿀 때마다 **양방향 반례**(진짜 버그 + 인프라 장애)를 함께 테스트해야 합니다.


---

## 🏗️ Step 2 (2026-09-13) — W-1 Static Recycled Worktree Pool 구현

`docs/14` 3번 난제는 문서에만 "해결"로 적혀 있었습니다. `central_hub/worktree_pool.py`로 실제 구현했습니다.

### /ALT3 결정

| 대안 | 판정 |
|---|---|
| A. 고정 풀 + 재사용(checkout·clean) | ✅ 채택 |
| B. 작업마다 생성·삭제 | ❌ 에디터가 파일을 잡고 있으면 `Access is denied` |
| C. 워크트리 없이 브랜치만 전환 | ❌ 에이전트끼리 작업 폴더를 공유해 충돌 |

### `docs/14` 원안에서 교정한 맹점

| ID | 원안의 가정 | 실제 | 교정 |
|---|---|---|---|
| W-a | `git checkout --force origin/main` | 원격이 없을 수 있고, 새 저장소 기본 브랜치는 `master`임(Git 2.55 실측) | ref를 커밋 SHA로 바꾼 뒤 **detached HEAD**로 체크아웃. 같은 브랜치 중복 체크아웃 제약도 함께 회피 |
| W-b | 재사용하면 끝 | `checkout --force`·`clean`은 **에이전트의 미커밋 작업을 지움** | 변경이 남은 슬롯은 `release()`가 `DIRTY`로 거부. `export_changes()`로 패치를 먼저 받거나 `discard=True`를 명시해야 함 |
| W-c | `git clean -fd` | ignored 파일(`*.db`, 캐시)이 남아 다음 작업으로 새어 나감 | 기본값 `clean -ffdx` |
| W-d | (언급 없음) | 파괴적 명령이 풀 밖을 가리키면 저장소 손상 | realpath 봉쇄 검사. 풀 루트와 저장소가 겹치면 생성 시 거부 |
| W-e | (언급 없음) | 크래시 후 `index.lock`이 남음 | 삭제하지 않고 슬롯을 **격리(QUARANTINED)**. 살아 있는 git 프로세스의 잠금일 수 있음 |
| W-f | (언급 없음) | Git for Windows는 파일 잠금 시 `Should I try again? (y/n)`를 물어 멈출 수 있음 | `GIT_ASK_YESNO=false`, `stdin=DEVNULL`, 120초 타임아웃 |

### Windows 실측으로 드러난 사실

에디터가 파일을 열어 둔 상태를 재현하자 git은 다음 메시지를 냈습니다.

```
warning: failed to remove held.txt: Invalid argument
```

흔히 예상하는 `Permission denied`가 **아닙니다**. 슬롯 격리는 정상 동작했지만, 트리아지가 이 문구를 몰라 `UNKNOWN`으로 분류했습니다. 실측 문자열로 인프라 패턴과 회귀 테스트를 추가했습니다. 추측만으로 패턴을 만들었다면 놓쳤을 결함입니다.

### 검증

* `tests/test_worktree_pool.py` 15건 신규 (Windows 전용 파일 잠금 실측 테스트 1건 포함)
* 모든 테스트는 임시 git 저장소에서 실행하며, 프로젝트 저장소는 건드리지 않음
* `python -m pytest tests -q` → **88 passed, 15 subtests** (Exit 0)

### Step 1 추가 자기비판

* **T-2 한계**: 토큰 파일의 `0o600` 권한은 POSIX 전용이라 **Windows NTFS에서는 효과가 없습니다**. 사용자 홈 폴더의 기본 ACL에 기대고 있을 뿐입니다. 엄격한 보호가 필요하면 `icacls`로 ACL을 설정해야 합니다.

### 남은 한계

* 슬롯 상태는 **프로세스 메모리에만** 있습니다. 허브가 재시작되면 BUSY 기록이 사라집니다. 풀은 허브 한 곳에서만 운영해야 합니다.
* 풀은 아직 허브·MCP 도구(`trinity_*`)와 **연결되지 않았습니다**. 에이전트가 쓰려면 다음 단계에서 도구로 노출해야 합니다.
