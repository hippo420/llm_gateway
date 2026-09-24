# Phase 6 학습 — Timeout / Retry / Fallback / Circuit Breaker

> 설계서: [`../docs/phases/phase-06-resilience.md`](../docs/phases/phase-06-resilience.md)
> 선행: Phase 4(Registry), Phase 5(`RoutingDecision.alternatives`)

Resilience4j 를 직접 만드는 Phase 다. 라이브러리를 쓰지 않고 만드는 이유는
**LLM 은 일반 API 와 재시도 규칙이 근본적으로 다르기** 때문이다.

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | `httpx.Timeout` 4종 (connect/read/write/pool) | `HttpClient` 타임아웃 | 중 |
| 2 | **streaming 에서 read timeout 의 의미** | — | **상** |
| 3 | httpx 예외 계층과 분류 | `IOException` 계열 | 중 |
| 4 | 지수 백오프 + 지터 (`random`) | Resilience4j Retry | 하 |
| 5 | `asyncio.timeout()` / `wait_for` | `Future.get(timeout)` | 중 |
| 6 | 상태 머신을 dataclass + Enum 으로 | Resilience4j CircuitBreaker | 중 |
| 7 | 스트림을 감싸면서 첫 chunk 를 감지하기 | — | **상** |
| 8 | 장애 주입 테스트 (`respx`, monkeypatch) | WireMock / MockWebServer | 중 |

---

## 1. Timeout 3단계 — 하나로는 진단이 불가능하다

| 종류 | 의미 | 초과 시 해석 |
|---|---|---|
| `connect` | TCP/TLS 연결 | Serving 프로세스 death, 네트워크 |
| `read` | **chunk 간** 무응답 시간 | 생성 정지, GPU hang |
| `total` | 전체 요청 시간 | 응답이 너무 김 (정상일 수도 있음) |

### 1.1 httpx 의 timeout 은 4개다

```python
httpx.Timeout(
    connect=5.0,     # 연결 수립
    read=30.0,       # 한 번의 read 작업 = 다음 바이트를 기다리는 시간
    write=10.0,      # 요청 본문 전송
    pool=5.0,        # 커넥션 풀에서 커넥션을 얻기까지
)
```

`TimeoutConfig(connect, read, total)` → httpx 매핑:

```python
def to_httpx(cfg: TimeoutConfig) -> httpx.Timeout:
    return httpx.Timeout(connect=cfg.connect, read=cfg.read, write=10.0, pool=5.0)
```

**`total` 은 httpx 에 없다.** 별도로 구현해야 한다 → 2절.

### 1.2 streaming 에서 read timeout 이 왜 "chunk 간" 인가

httpx 의 `read` 타임아웃은 **"다음 데이터가 올 때까지 기다리는 시간"** 이지 전체 시간이 아니다.
스트리밍에서는 이게 정확히 우리가 원하는 의미다.

```text
[chunk] --0.2s-- [chunk] --0.3s-- [chunk] ...   ← 각 간격이 read timeout 대상
전체 응답이 120초 걸려도, 간격이 30초를 넘지 않으면 통과
```

**전체 시간에 read timeout 을 걸면 긴 정상 응답이 잘린다.** 설계서 2장의 핵심 지적이다.

권장 초기값 (Qwen 7B / RTX 4070 Ti):

```yaml
timeout: { connect: 5, read: 30, total: 180 }
```

첫 호출은 모델 로딩(cold start) 때문에 오래 걸린다 → warm-up 요청으로 해결하거나 넉넉히 잡는다.

---

## 2. `total` 타임아웃 — `asyncio.timeout()`

Python 3.11+ 에 전체 시간 제한 도구가 있다.

```python
try:
    async with asyncio.timeout(cfg.total):          # 3.11+
        response = await adapter.chat(req)
except TimeoutError:
    raise UpstreamTimeoutError("total timeout exceeded", kind="total")
```

스트리밍에도 쓸 수 있다.

```python
async with asyncio.timeout(cfg.total):
    async for chunk in adapter.stream_chat(req):
        yield chunk
```

| 항목 | 내용 |
|---|---|
| `asyncio.timeout()` | 3.11+ context manager. **권장** |
| `asyncio.wait_for(coro, t)` | 구버전 방식. async generator 에는 쓰기 불편 |
| 발생 예외 | Python 3.11+ 에서 `TimeoutError` (`asyncio.TimeoutError` 는 별칭) |
| 내부 동작 | **태스크를 cancel 한다** → `CancelledError` 가 먼저 발생한다 |

**중요:** `asyncio.timeout` 은 내부적으로 취소를 쓴다.
따라서 `except asyncio.CancelledError` 블록이 코드 안에 있으면 그게 먼저 걸린다.
"클라이언트 이탈" 과 "타임아웃" 을 구분하려면 `ctx` 플래그나 예외 재분류가 필요하다.

```python
except TimeoutError:                 # timeout 이 CancelledError 를 이걸로 변환해준다
    ...
except asyncio.CancelledError:       # 진짜 클라이언트 이탈
    raise
```

순서를 이렇게 두면 자연스럽게 구분된다.

---

## 3. 예외 분류 — 재시도 가능 여부 판정

### 3.1 httpx 예외 계층

```text
httpx.HTTPError
├── httpx.RequestError               (요청을 보내지 못했거나 응답을 못 받음)
│   ├── httpx.TransportError
│   │   ├── httpx.TimeoutException
│   │   │   ├── ConnectTimeout        재시도 O
│   │   │   ├── ReadTimeout           조건부 (첫 토큰 전이면 O)
│   │   │   ├── WriteTimeout
│   │   │   └── PoolTimeout
│   │   ├── httpx.ConnectError        재시도 O  (Ollama 죽음)
│   │   ├── httpx.ReadError
│   │   └── httpx.RemoteProtocolError 스트림 중간 끊김
│   └── ...
└── httpx.HTTPStatusError            상태 코드로 판정 (raise_for_status)
```

### 3.2 판정 함수

```python
_RETRYABLE_STATUS = frozenset({502, 503, 504})


def is_retryable(exc: Exception, ctx: RequestContext) -> bool:
    # 1. 첫 토큰이 이미 나갔으면 무조건 금지 — 다른 어떤 조건보다 우선한다
    if ctx.stream_started:
        return False

    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    if isinstance(exc, httpx.ReadTimeout):
        return True                     # 첫 토큰 전이므로 여기까지 왔다
    return False
```

- `isinstance(x, A | B)` 는 Python 3.10+ 문법 (`A` 또는 `B`). 튜플 `(A, B)` 도 된다.
- `frozenset` = 불변 Set. 상수로 쓰기 좋다.

### 3.3 재시도 금지 목록 (설계서 3장)

| 상황 | 이유 |
|---|---|
| **첫 토큰이 이미 나간 뒤** | 클라이언트가 일부를 봤다. 재시도하면 중복 생성 |
| 4xx | 요청 자체가 잘못됨. 재시도해도 같은 결과 |
| context length 초과 | 위와 동일 |
| 사용자가 취소한 요청 | `CancelledError` — 재시도 대상이 아니다 |

**멱등성:** LLM 생성은 멱등이 아니다. 재시도는 "응답을 못 받았을 때" 만 정당하다.
HTTP 메서드가 POST 라는 사실이 아니라, **응답을 받았는가**가 기준이다.

---

## 4. 백오프 + 지터

```python
import random

def backoff_delay(attempt: int, cfg: RetryConfig) -> float:
    """attempt 는 1부터."""
    delay = cfg.initial_delay_ms * (2 ** (attempt - 1))
    delay = min(delay, cfg.max_delay_ms)
    if cfg.jitter:
        delay = random.uniform(delay * 0.5, delay)      # full jitter 의 변형
    return delay / 1000.0
```

**지터가 왜 필요한가:** 여러 요청이 동시에 실패하면 정확히 같은 시각에 재시도한다.
그러면 회복 중인 upstream 을 다시 쓰러뜨린다(thundering herd).
랜덤을 섞어 재시도를 흩뿌린다.

> 여기서는 `random` 을 써도 된다. **재현성이 필요 없는 유일한 곳**이다.
> (Phase 5 의 버킷팅과 반대 — 거기서는 절대 `random` 을 쓰면 안 됐다)

`max_attempts: 2` 는 **최초 시도를 포함해 총 2회**다. 재시도 1회라는 뜻.
LLM 호출은 비싸다. 상한을 낮게 유지한다.

> `tenacity` 라이브러리를 쓸 수도 있지만, **"첫 토큰 이후 금지" 같은 도메인 조건**을
> 표현하기 번거롭다. 이 프로젝트는 직접 구현한다.

---

## 5. `stream_started` 플래그 전파 — 이 Phase 의 핵심 난제

### 5.1 문제

```python
async for chunk in adapter.stream_chat(req):
    yield chunk          # ← 이 시점에 첫 chunk 가 클라이언트로 나갔다
```

이후 발생하는 어떤 에러도 재시도할 수 없다. 그런데 재시도 판단은
**스트림을 감싸는 바깥 계층**에서 한다. 어떻게 알리는가?

### 5.2 해결 — 스트림을 감싸면서 플래그를 세운다

```python
async def _guarded_stream(
    self, adapter: LLMAdapter, req: AdapterChatRequest, ctx: RequestContext
) -> AsyncIterator[AdapterChatChunk]:
    async for chunk in adapter.stream_chat(req):
        if chunk.delta:                    # content 가 있는 첫 chunk 기준 (Phase 2 와 동일)
            ctx.stream_started = True      # ← 되돌릴 수 없는 지점 표시
        yield chunk
```

`ctx` 는 `RequestContext` 객체이므로 **참조로 공유된다.**
바깥의 재시도 루프가 같은 객체를 보고 있으므로 즉시 반영된다.
Phase 1 에서 `ctx.stream_started` 필드를 미리 만들어둔 이유다.

### 5.3 재시도 루프

```python
async def execute(self, decision, call, ctx):
    last_exc: Exception | None = None

    for attempt in range(1, self._retry.max_attempts + 1):
        try:
            return await call(decision.deployment)
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc, ctx):
                raise                                  # 즉시 포기
            if attempt >= self._retry.max_attempts:
                break
            RETRY.labels(deployment_id=decision.deployment.id, reason=...).inc()
            await asyncio.sleep(backoff_delay(attempt, self._retry))

    raise last_exc                                     # 마지막 예외를 그대로 올린다
```

`ChatService` 는 `ResiliencePolicy.execute(...)` 한 번만 호출한다.
**서비스 코드에 retry 루프를 직접 쓰지 않는다** (설계서 7장).

---

## 6. Fallback

```python
async def with_fallback(self, decision, call, ctx):
    chain = (decision.deployment, *decision.alternatives)[: self._max_chain]

    for i, deployment in enumerate(chain):
        if ctx.stream_started:
            break                                  # 스트리밍 시작 후에는 폴백 불가
        if self._breaker.is_open(deployment.id):
            continue                               # OPEN 인 곳은 건너뛴다
        try:
            result = await self._retry_execute(deployment, call, ctx)
            if i > 0:
                FALLBACK.labels(
                    from_deployment=chain[0].id, to_deployment=deployment.id, reason=...
                ).inc()
                ctx.extra["fallback_to"] = deployment.id
            return result
        except GatewayError as exc:
            last = exc
            continue

    raise last
```

- `(a, *b)` = 튜플 앞에 원소 붙이기 (언패킹 문법)
- `[:n]` 슬라이싱으로 체인 길이 상한 (기본 2단계)
- **Fallback 발생 자체가 지표다.** 조용히 성공시키면 문제를 영원히 못 본다.

응답 헤더에도 남긴다: `X-Gateway-Fallback: qwen-7b@ollama`

---

## 7. Circuit Breaker

### 7.1 상태 머신

```text
CLOSED ──(연속 실패 N회)──► OPEN ──(cooldown 경과)──► HALF_OPEN ──(성공)──► CLOSED
                                                          └──(실패)──► OPEN
```

```python
class BreakerState(str, Enum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


@dataclass
class BreakerEntry:
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_at: float | None = None            # perf_counter 기준


class CircuitBreaker:
    def __init__(self, threshold: int, cooldown_sec: float) -> None:
        self._entries: dict[str, BreakerEntry] = defaultdict(BreakerEntry)
        ...

    def allow(self, deployment_id: str) -> bool:
        e = self._entries[deployment_id]
        if e.state is BreakerState.OPEN:
            if time.perf_counter() - e.opened_at >= self._cooldown:
                self._transition(deployment_id, BreakerState.HALF_OPEN)
                return True                    # 탐색 요청 1건 허용
            return False
        return True

    def on_success(self, deployment_id: str) -> None: ...
    def on_failure(self, deployment_id: str) -> None: ...
```

| 도구 | 설명 |
|---|---|
| `str, Enum` 다중 상속 | 값이 문자열이라 JSON/로그 직렬화가 자연스럽다 |
| `is` 로 Enum 비교 | Enum 은 싱글턴이므로 `is` 가 관용적 (`==` 도 동작) |
| `defaultdict(BreakerEntry)` | 없는 키 접근 시 자동 생성 (`computeIfAbsent`) |
| `time.perf_counter()` | 단조 시계 (Phase 2 와 동일 이유) |

### 7.2 상태 전이는 반드시 관측한다

```python
def _transition(self, deployment_id: str, to: BreakerState) -> None:
    log.warning("circuit breaker transition", extra={"deployment_id": ..., "to": to.value})
    BREAKER_TRANSITION.labels(deployment_id=deployment_id, to_state=to.value).inc()
    BREAKER_STATE.labels(deployment_id=deployment_id).set(_STATE_VALUE[to])   # Gauge
```

Gauge 값 매핑: `0=closed, 1=half, 2=open` (설계서 6장).

### 7.3 Router 와의 연결

**OPEN 인 deployment 는 Phase 5 의 후보 목록에서 제외한다.**
`HealthAwareStrategy` 가 breaker 상태를 참조하면 자연스럽게 이어진다.

### 7.4 GPU 1장 환경의 현실

설계서 경고: breaker 가 열려도 **대체 자원이 없을 수 있다.**
`breaker OPEN → 외부 API fallback` 이 있어야 의미가 있다.
없다면 breaker 는 "빨리 실패시켜 지연만 줄이는" 역할에 그친다 — 그것도 가치는 있다.

---

## 8. 자원 정리 — 스트림 중간에 끊길 때

```python
async def stream_with_cleanup(adapter, req, ctx):
    stream = adapter.stream_chat(req)
    try:
        async for chunk in stream:
            yield chunk
    finally:
        await stream.aclose()          # async generator 도 close 가 필요하다
```

또는 `contextlib.aclosing` 을 쓴다.

```python
from contextlib import aclosing

async with aclosing(adapter.stream_chat(req)) as stream:
    async for chunk in stream:
        yield chunk
```

**async generator 를 중간에 버리면 정리 코드(`finally`)가 늦게 실행되거나 경고가 뜬다.**
`aclosing` 이 `try-with-resources` 역할을 한다.

---

## 9. 장애 주입 테스트

### 9.1 FakeAdapter 확장

Phase 1 의 `FakeAdapter` 에 `error` 파라미터가 이미 있다.

```python
FakeAdapter(dep, error=httpx.ConnectError("refused"))                    # 즉시 실패
FakeAdapter(dep, chunks=["안녕"], error=httpx.RemoteProtocolError(...))  # 부분 응답 후 끊김
FakeAdapter(dep, first_token_delay=60.0)                                 # read timeout 재현
```

### 9.2 respx (httpx 전용 목킹)

```powershell
pip install respx
```

```python
import respx

@respx.mock
async def test_connect_error_falls_back():
    respx.post("http://localhost:11434/api/chat").mock(side_effect=httpx.ConnectError("x"))
    ...
```

WireMock 대응물이다. **실제 OllamaAdapter 의 HTTP 처리까지 검증**할 때 쓴다.
서비스 계층 테스트는 `FakeAdapter` 로 충분하다.

### 9.3 반드시 검증할 시나리오 (DoD)

| 시나리오 | 기대 동작 | DoD |
|---|---|---|
| Ollama 프로세스 강제 종료 | fallback 또는 정의된 에러 | 1 |
| **첫 토큰 후 upstream 끊김** | **재시도 없이** 스트림 정상 종료 | 2 |
| 연속 실패 → cooldown → 복구 | breaker OPEN → HALF_OPEN → CLOSED | 4 |
| 재시도로 인한 중복 생성 | 발생하지 않음 | 5 |
| 응답 지연 (chunk 간 40초) | read timeout, `kind="read"` 로 기록 | — |

DoD 2번과 5번이 이 Phase 의 존재 이유다. 테스트로 못 박아둔다.

```python
async def test_no_retry_after_first_token():
    adapter = FakeAdapter(dep, chunks=["안녕"], error=httpx.RemoteProtocolError("cut"))
    chunks = [c async for c in service.stream(req, ctx)]
    assert adapter.call_count == 1          # 재시도하지 않았다
    assert ctx.stream_started is True
```

---

## 10. 메트릭

```python
RETRY = Counter("llm_gateway_retry_total", "", ["deployment_id", "reason"])
FALLBACK = Counter("llm_gateway_fallback_total", "", ["from_deployment", "to_deployment", "reason"])
TIMEOUT = Counter("llm_gateway_timeout_total", "", ["deployment_id", "kind"])     # connect|read|total
BREAKER_STATE = Gauge("llm_gateway_circuit_breaker_state", "", ["deployment_id"])
BREAKER_TRANSITION = Counter("llm_gateway_circuit_breaker_transition_total", "", ["deployment_id", "to_state"])
```

`reason` 과 `kind` 는 **열거형**이다 (Phase 2 카디널리티 규칙).
`reason="connect_error"` 는 되고 `reason="Connection refused to 127.0.0.1"` 은 안 된다.

---

## 11. 실습 과제

1. `TimeoutConfig → httpx.Timeout` 매핑 + **streaming 에서 read timeout 이 chunk 간에 걸리는지 실험**
   (`FakeAdapter` 로는 검증이 안 된다. 느린 HTTP 서버를 하나 띄워 확인)
2. `is_retryable()` + 단위 테스트 (예외 종류 × `stream_started` 조합)
3. `backoff_delay()` — 지터가 범위 안에 드는지
4. 재시도 루프 → `FakeAdapter(error=ConnectError)` 로 2회 호출 확인
5. `_guarded_stream()` 으로 `stream_started` 전파 → **DoD 2 테스트 작성**
6. fallback 체인 → deployment 2개로 1순위 실패 시 2순위 성공 확인
7. circuit breaker 상태 머신 단위 테스트 (시간은 주입 가능하게 설계할 것)
8. Ollama 를 실제로 죽이고 요청 (DoD 1)
9. Grafana 에 retry/fallback/timeout 패널 추가 (DoD 3)

> 7번 힌트: `time.perf_counter` 를 직접 부르지 말고 `clock: Callable[[], float] = time.perf_counter`
> 로 주입받으면 테스트에서 시간을 마음대로 조작할 수 있다.

---

## 12. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| `read` 를 전체 시간으로 이해 | 긴 정상 응답이 잘림 | chunk 간 간격임을 이해 |
| `total` 을 httpx 에 기대 | 전체 제한이 없음 | `asyncio.timeout()` |
| `stream_started` 미전파 | **중복 생성** (가장 심각) | 스트림 래퍼에서 플래그 |
| adapter 내부에서 재시도 | 재시도 중첩 → 비용 폭증 | adapter 계약 3번 준수 |
| `CancelledError` 를 재시도 대상으로 | 취소한 요청을 다시 실행 | 별도 처리 후 re-raise |
| 지터 없는 백오프 | thundering herd | `random.uniform` |
| `max_attempts` 를 "추가 재시도 횟수" 로 오해 | 예상보다 2배 호출 | 최초 시도 포함 |
| async generator 미종료 | 커넥션 누수, 경고 | `aclosing` / `finally: aclose()` |
| breaker 상태를 로그만 남김 | 운영 중 상태 파악 불가 | Gauge + transition Counter |
| `reason` 에 원문 메시지 | 카디널리티 폭발 | 열거형 |
| fallback 을 조용히 성공 처리 | 장애를 영원히 못 봄 | metric + 헤더 + 로그 |

---

## 13. 설계 원칙 한 줄

> **LLM 요청은 비싸다. 무조건 재시도하지 않는다.**
> 재시도가 정당한 유일한 조건은 "응답을 못 받았다" 이고,
> 그 판단 기준은 HTTP 메서드가 아니라 **첫 토큰이 나갔는가** 다.
