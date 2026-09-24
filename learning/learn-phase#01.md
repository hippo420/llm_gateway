# Phase 1 학습 — LLM Gateway 뼈대 구현

> 설계서: [`../docs/phases/phase-01-llm-gateway.md`](../docs/phases/phase-01-llm-gateway.md)
> 선행: [`common.md`](./common.md) 전체 (특히 2·3·4·5·6장)

이 Phase 는 **Python 웹 백엔드를 처음부터 끝까지 한 번 짓는 과정**이다.
분량이 가장 많지만, 여기서 배운 것이 Phase 2~10 에서 계속 재사용된다.

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | FastAPI 앱 구성 (factory / router / lifespan) | `@SpringBootApplication` + `@RestController` | 중 |
| 2 | Pydantic v2 로 OpenAI 호환 스키마 만들기 | Jackson DTO + Bean Validation | 중 |
| 3 | ASGI Middleware | Servlet Filter | 중 |
| 4 | `contextvars` 로 request_id 전파 | MDC / ThreadLocal | **상** |
| 5 | ABC + dataclass 로 Adapter 경계 만들기 | interface + record | 하 |
| 6 | httpx AsyncClient — 요청 / 스트리밍 / 커넥션 재사용 | WebClient | **상** |
| 7 | async generator 와 SSE 응답 | `Flux<T>` + SseEmitter | **상** |
| 8 | 예외 계층 + 전역 핸들러 | `@ControllerAdvice` | 하 |
| 9 | PyYAML + Pydantic 로 설정 로딩 | `@ConfigurationProperties` | 하 |
| 10 | pytest + ASGITransport 로 LLM 없이 API 테스트 | MockMvc + Mockito | 중 |

---

## 1. FastAPI 앱 구성

### 1.1 app factory 패턴

Spring 은 `@SpringBootApplication` 하나로 컨텍스트가 자동 구성되지만,
FastAPI 는 **앱을 만드는 함수를 직접 쓴다.**

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(lifespan=lifespan, title="LLM Gateway")
    app.include_router(v1_router)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)      # 나중에 add = 더 바깥
    app.add_exception_handler(GatewayError, gateway_error_handler)
    return app

app = create_app()      # uvicorn 이 import 하는 대상
```

factory 로 만드는 이유는 **테스트에서 설정을 바꿔 앱을 여러 번 만들기 위해서**다.
전역 `app` 하나만 있으면 테스트 격리가 안 된다.

### 1.2 lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)

    source = YamlConfigSource(settings.config_path)
    snapshot = source.load()                     # 실패하면 예외 → 기동 중단
    app.state.registry = ModelRegistry(snapshot)
    app.state.adapters = AdapterFactory()
    app.state.chat_service = ChatService(app.state.registry, app.state.adapters)

    yield                                        # ── 여기부터 서비스 중 ──

    await app.state.adapters.close_all()         # 빼먹으면 uvicorn 이 안 죽는다
```

> **`app.state` 는 Spring 의 ApplicationContext 자리다.** 싱글턴은 전부 여기 담는다.
> 모듈 전역 변수로 두면 테스트 격리가 깨진다.

**설계 판단 (설계서 명시):** 설정 로드 실패 시 **기동을 중단**한다.
잘못된 설정으로 뜨는 것보다 안 뜨는 게 낫다. (Phase 4 의 reload 실패와는 다르게 다룬다)

### 1.3 라우터 분리

```text
api/router.py          v1_router 조립 (include_router 로 하위 라우터 합침)
api/routes/chat.py     /v1/chat/completions, /v1/chat, /v1/chat/stream
api/routes/models.py   /v1/models
api/routes/health.py   /healthz, /readyz
```

`APIRouter` 는 Spring 의 `@RequestMapping("/v1")` 붙은 컨트롤러 묶음이라고 보면 된다.

### 1.4 의존성 주입

```python
# api/dependencies.py
def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service

# api/routes/chat.py
async def chat_completions(
    body: ChatCompletionRequest,
    service: ChatService = Depends(get_chat_service),
): ...
```

`Depends` 는 **요청마다 함수를 호출**한다. 무거운 객체를 여기서 만들면 안 된다.
lifespan 에서 만들어 `app.state` 에 두고, 여기서는 꺼내기만 한다.

### 1.5 헬스체크의 의미 구분

| 엔드포인트 | 질문 | 구현 |
|---|---|---|
| `/healthz` | 프로세스가 살아있는가 (liveness) | 항상 200. **upstream 을 호출하지 않는다** |
| `/readyz` | 트래픽을 받을 준비가 됐는가 (readiness) | registry 로드 여부 + adapter `health()` |

`/healthz` 에서 Ollama 를 호출하면, Ollama 가 죽었을 때 Gateway 프로세스까지 재시작 대상이 된다.
**둘을 섞지 않는 것이 핵심이다.**

📖 `.venv/Lib/site-packages/fastapi/.agents/skills/fastapi/references/path-operations.md`, `dependencies.md`

---

## 2. Pydantic v2 — OpenAI 호환 스키마

### 2.1 왜 까다로운가

Spring AI 의 OpenAI 클라이언트는 **응답 스키마를 엄격히 검증**한다.
필드 하나만 빠져도 파싱에 실패한다 (설계서 7장 리스크 표 1번).

필수 필드:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1756000000,
  "model": "qwen-7b",
  "choices": [{"index": 0, "message": {...}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}
}
```

### 2.2 알아야 할 Pydantic 기능

```python
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool = False

    model_config = ConfigDict(extra="ignore")   # 모르는 필드는 무시
```

| 기능 | 용도 |
|---|---|
| `Field(default=None, ge=, le=, gt=, min_length=)` | Bean Validation 대응 |
| `Literal[...]` | enum 제약 |
| `ConfigDict(extra="ignore" / "forbid")` | 모르는 필드 처리 정책 |
| `model_dump(exclude_none=True)` | `null` 필드 제거 (OpenAI 응답에서 중요) |
| `Field(alias="...")` | 예약어/네이밍 충돌 회피 |
| `@field_validator` | 커스텀 검증 |

> **`model_` 로 시작하는 필드명 주의.** Pydantic v2 는 `model_` 을 내부 예약 접두사로 쓴다.
> `model` 자체는 괜찮지만 `model_version` 같은 필드를 만들 때 경고가 뜬다.
> 필요하면 `model_config = ConfigDict(protected_namespaces=())` 로 푼다.

### 2.3 "미지정" 과 "명시적 null" 의 구분

이게 Phase 1 에서 가장 실수하기 쉬운 부분이다.

```python
# 요청에 temperature 가 없으면 None 으로 들어온다
merged = {**deployment.options}
if request.temperature is not None:          # ← None 체크를 반드시 해야 한다
    merged["temperature"] = request.temperature
```

`ChatService._build_adapter_request()` docstring 의 경고가 정확히 이것이다.
None 을 그대로 덮어쓰면 **deployment 기본값이 지워진다.**

병합 우선순위: `요청 파라미터 > deployment.options > defaults.options`

📖 Pydantic 공식 문서 "Models" / "Fields" / `.venv/.../fastapi/references/pydantic.md`

---

## 3. ASGI Middleware

```python
class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("X-Request-Id") or new_request_id()
        ctx = RequestContext(request_id=req_id)
        set_request_context(ctx)

        response = await call_next(request)      # 다음 필터 호출 (chain.doFilter)

        response.headers["X-Request-Id"] = req_id
        return response
```

- `BaseHTTPMiddleware` 를 상속하고 `dispatch` 만 구현한다.
- `await call_next(request)` 가 `chain.doFilter(...)` 에 해당한다.
- **등록 순서 주의:** 나중에 `add_middleware` 한 것이 바깥이다 (common.md 5.4).

**함정:** 예외가 미들웨어를 통과하지 못하는 구성이 되기 쉽다.
그래서 `gateway_error_handler` 에서 `X-Request-Id` 를 **한 번 더** 붙인다 (`main.py` docstring 참고).

---

## 4. contextvars — request_id 전파 (이 Phase 최대 난관)

### 4.1 왜 ThreadLocal 이 안 되는가

Spring 은 요청당 스레드가 고정이라 `MDC` 가 성립한다.
asyncio 는 **한 스레드에서 수천 개 코루틴이 번갈아 실행**되므로 ThreadLocal 이 무의미하다.

`ContextVar` 는 값을 **태스크(코루틴) 단위로 격리**한다. `await` 를 넘어가도 유지된다.

```python
_request_context: ContextVar[RequestContext | None] = ContextVar(
    "llm_gateway_request_context", default=None
)

def set_request_context(ctx: RequestContext) -> None:
    _request_context.set(ctx)          # Token 을 반환한다

def get_request_context() -> RequestContext | None:
    return _request_context.get()
```

### 4.2 `set()` 이 반환하는 Token

```python
token = _request_context.set(ctx)
...
_request_context.reset(token)      # 이전 값으로 되돌림
```

ASGI 는 요청마다 별도 태스크에서 실행되므로 보통 reset 없이도 누수되지 않는다
(태스크가 끝나면 컨텍스트도 사라진다).
**단, `BackgroundTasks` 를 쓰기 시작하면 달라진다** — `core/context.py` docstring 의 TODO 가 이 얘기다.

### 4.3 로깅과 연결

```python
class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True                     # False 를 반환하면 로그가 버려진다
```

Logback 의 MDC converter 자리다. `Filter` 를 핸들러에 붙이면 모든 로그에 `request_id` 가 들어간다.

### 4.4 절대 하면 안 되는 것

**`request_id` 를 Metric label 로 쓰지 않는다.** (`core/context.py` 첫 주석)
카디널리티가 무한대로 폭발해 Prometheus 가 죽는다. Phase 2 에서 다시 다룬다.

---

## 5. Adapter 경계 — ABC + dataclass

`adapters/base.py` 는 **이 프로젝트에서 가장 중요한 파일**이다.
설계서가 "여기서 시간 쓸 가치가 있다" 고 명시했다.

### 5.1 왜 중립 DTO 인가

```python
@dataclass
class AdapterChatRequest:
    model: str                       # upstream 모델명 (논리명 아님!)
    messages: list[AdapterMessage]
    temperature: float | None = None
    ...
```

OpenAI 스키마(`ChatCompletionRequest`)를 adapter 까지 내리면,
vLLM/외부 API adapter 를 붙일 때 **추상화가 새기 시작한다**.
Java 로 치면 도메인 계층에 `HttpServletRequest` 를 넘기는 것과 같다.

변환 지점은 `ChatService._build_adapter_request()` 하나뿐이다.

### 5.2 5가지 계약 (파일 상단 주석)

1. Adapter 만이 upstream 프로토콜을 안다
2. Adapter 는 Gateway 의 OpenAI 스키마를 모른다
3. **Adapter 는 재시도하지 않는다** (Phase 6 의 상위 책임)
4. Adapter 는 타이밍/토큰 원자료를 반드시 채운다 (Phase 2 의 입력)
5. Adapter 는 자기 예외를 `GatewayError` 로 변환해 올린다

3번을 어기면 Phase 6 에서 재시도가 **중첩**되어 LLM 호출 비용이 폭증한다.

### 5.3 factory 와 커넥션 재사용

```python
class AdapterFactory:
    def __init__(self) -> None:
        self._cache: dict[str, LLMAdapter] = {}

    def get(self, deployment: ModelDeployment) -> LLMAdapter:
        key = deployment.id
        if key not in self._cache:
            self._cache[key] = _REGISTRY[deployment.adapter](deployment)
        return self._cache[key]

    async def close_all(self) -> None:
        for adapter in self._cache.values():
            await adapter.aclose()
```

`_REGISTRY` 는 `{"ollama": OllamaAdapter}` 형태의 dict 다.
Python 에서 **클래스는 일급 객체**라서 dict 값으로 그냥 넣을 수 있다.
Java 의 `Map<String, Supplier<Adapter>>` + 팩토리 메서드에 해당한다.

이 캐시가 없으면 요청마다 `httpx.AsyncClient` 가 새로 생겨 커넥션 풀이 붕괴한다
(설계서 7장 리스크 표 3번).

---

## 6. httpx — Ollama 호출

### 6.1 클라이언트 생성 (adapter 가 소유)

```python
class OllamaAdapter(LLMAdapter):
    name = "ollama"

    def __init__(self, deployment: ModelDeployment) -> None:
        super().__init__(deployment)
        self._client = httpx.AsyncClient(
            base_url=str(deployment.endpoint),
            timeout=httpx.Timeout(
                connect=deployment.timeout.connect,
                read=deployment.timeout.read,
                write=10.0,
                pool=5.0,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
```

`httpx.Timeout` 은 4가지를 따로 받는다. 자세한 의미는 Phase 6 에서 다룬다.

### 6.2 non-streaming 호출

```python
resp = await self._client.post("/api/chat", json=payload)
resp.raise_for_status()          # 4xx/5xx 면 HTTPStatusError
data = resp.json()
```

### 6.3 streaming 호출 (핵심)

Ollama 는 **NDJSON**(줄마다 JSON 한 개)을 흘려보낸다. SSE 가 아니다.

```python
async def stream_chat(self, request):                 # async def + yield
    payload = self._build_payload(request, stream=True)
    async with self._client.stream("POST", "/api/chat", json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.strip():
                continue
            obj = json.loads(line)
            yield AdapterChatChunk(delta=obj["message"]["content"], ...)
```

| 포인트 | 이유 |
|---|---|
| `async with client.stream(...)` | 응답 본문을 반드시 닫아야 한다. 안 닫으면 커넥션 누수 |
| `aiter_lines()` | 줄 단위 비동기 iterator |
| `json.loads(line)` | Ollama 는 NDJSON — SSE `data: ` 접두사가 없다 |
| 빈 줄 건너뛰기 | 스트림 끝/중간에 빈 줄이 온다 |

### 6.4 Ollama 응답 매핑

| Ollama 필드 | Gateway | 주의 |
|---|---|---|
| `prompt_eval_count` | `usage.input_tokens` | 없으면 `None` |
| `eval_count` | `usage.output_tokens` | 없으면 `None` |
| `prompt_eval_duration` | `timings.prompt_eval_sec` | **나노초 → 초 변환** (`/ 1e9`) |
| `eval_duration` | `timings.generation_sec` | 나노초 |
| `load_duration` | `timings.load_sec` | 나노초. 크면 cold start (진단 R6) |
| `done_reason` | `finish_reason` | `stop` / `length` 매핑 |

**나노초 단위를 초로 바꾸는 것을 빼먹는 실수가 흔하다.** 지표가 10억 배로 나온다.

### 6.5 예외 분류 (Phase 6 의 기초)

| httpx 예외 | 의미 |
|---|---|
| `httpx.ConnectError` | 연결 실패 (Ollama 프로세스 죽음) |
| `httpx.ConnectTimeout` | 연결 타임아웃 |
| `httpx.ReadTimeout` | 응답/청크 대기 타임아웃 |
| `httpx.HTTPStatusError` | 4xx/5xx (`raise_for_status()` 가 던짐) |
| `httpx.RemoteProtocolError` | 스트림 중간에 연결 끊김 |

전부 `GatewayError` 하위로 변환해서 올린다 (계약 5번).
`docs/specs/error-codes.md` 의 코드 체계를 따른다.

📖 httpx 공식 문서 "Async Support", "Streaming Responses"

---

## 7. SSE 스트리밍 응답

### 7.1 Ollama NDJSON → OpenAI SSE 변환

들어오는 것과 나가는 것의 포맷이 다르다.

```text
Ollama (NDJSON)                     Gateway (SSE)
{"message":{"content":"삼"},...}  →  data: {"object":"chat.completion.chunk",...}\n\n
                                     ...
                                     data: [DONE]\n\n
```

```python
async def sse_generator() -> AsyncIterator[str]:
    async for chunk in service.stream(body, ctx):
        yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
    yield "data: [DONE]\n\n"

return StreamingResponse(sse_generator(), media_type="text/event-stream")
```

**`\n\n` 두 개가 필수다.** 하나면 클라이언트가 이벤트 경계를 인식하지 못한다.

### 7.2 스트림 도중 에러 — HTTP 상태를 바꿀 수 없다

첫 chunk 를 보낸 순간 HTTP 200 이 이미 나갔다. 500 으로 바꿀 방법이 없다.

```python
try:
    async for chunk in service.stream(body, ctx):
        yield f"data: {...}\n\n"
except GatewayError as exc:
    yield f"data: {json.dumps(exc.to_error_body(req_id))}\n\n"   # 에러 chunk 로 알린다
finally:
    yield "data: [DONE]\n\n"
```

이 제약이 Phase 6 의 "첫 토큰 이후 재시도 금지" 규칙의 근거이기도 하다.

### 7.3 클라이언트 이탈

```python
except asyncio.CancelledError:
    ctx.extra["cancelled"] = True
    raise                    # 반드시 re-raise (common.md 3.6)
```

upstream 스트림은 `async with` 가 닫아준다.

---

## 8. 예외 계층과 전역 핸들러

```python
class GatewayError(Exception):
    code: ClassVar[str] = "GW-5010"
    error_type: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail          # 로그 전용. 응답 body 에 넣지 않는다
```

핸들러 3종을 등록한다.

| 대상 | 결과 |
|---|---|
| `GatewayError` | 정의된 코드 + 상태로 직렬화 |
| `RequestValidationError` (Pydantic) | `GW-4000 invalid_request` 로 통일 |
| `Exception` | `GW-5010 internal_error`, 스택트레이스는 로그에만 |

**보안 주의 (`main.py` docstring):**
Pydantic 검증 에러 상세에는 **요청 body 조각이 들어간다 = 프롬프트가 샌다.**
필드 이름만 남기고 값은 제거해야 한다.

FastAPI 기본 422 응답을 그대로 두지 않는 이유는, Spring 쪽에서 에러 파싱 코드를 하나만 두게 하기 위해서다.

---

## 9. 설정 로딩 (YAML)

```python
class YamlConfigSource(ConfigSource):
    def load(self) -> RegistrySnapshot:
        with open(self._path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)          # safe_load 만 사용
        return GatewayConfig.model_validate(raw).to_snapshot()
```

- **`yaml.safe_load()` 만 쓴다.** `yaml.load()` 는 임의 파이썬 객체를 만들 수 있어 위험하다.
- `encoding="utf-8"` 을 반드시 명시한다. **Windows 기본 인코딩은 UTF-8 이 아니다** (cp949).
  한글 주석이 있는 `gateway.yaml` 에서 바로 터진다.
- 파싱 결과는 dict 다. **Pydantic 모델로 검증**해서 오타를 즉시 잡는다.
  (`endpont:` 같은 오타가 조용히 무시되면 안 된다)

경로 처리는 `pathlib.Path` 를 쓴다 (`settings.py` 가 이미 `Path` 타입).
문자열 `+` 로 경로를 만들지 않는다.

📖 `docs/specs/config-spec.md`

---

## 10. 테스트

### 10.1 실제 서버 없이 API 호출

```python
@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

`ASGITransport` 는 **소켓을 열지 않고** ASGI 앱을 직접 호출한다.
MockMvc 와 같은 역할이면서 실제 미들웨어/핸들러를 전부 통과한다.

> fixture 안에서 `yield` 를 쓰면 `yield` 뒤가 teardown(`@AfterEach`)이다.

### 10.2 FakeAdapter

`tests/conftest.py` 의 `FakeAdapter` 가 이 프로젝트 테스트 전략의 핵심이다.

```python
class FakeAdapter(LLMAdapter):
    name = "fake"

    def __init__(self, deployment, *, chunks=None, error=None, first_token_delay=0.0):
        ...

    async def stream_chat(self, request):
        if self._first_token_delay:
            await asyncio.sleep(self._first_token_delay)   # TTFT 검증용
        for c in self._chunks:
            yield AdapterChatChunk(delta=c)
        yield AdapterChatChunk(delta="", finish_reason="stop", usage=..., timings=...)
```

- `first_token_delay` 덕분에 **Phase 2 의 TTFT 측정 로직까지 테스트 가능**하다.
- `self.calls` 에 요청을 기록하면 "파라미터 병합이 제대로 됐는지" 검증할 수 있다
  (Mockito 의 `verify(...)` 대응).

### 10.3 검증 항목 (DoD 대응)

| 테스트 | DoD |
|---|---|
| 응답이 OpenAI 스키마 필드를 전부 갖는가 | 1, 3 |
| `stream: true` 가 SSE 로 오고 `[DONE]` 으로 끝나는가 | 2 |
| 모든 응답 헤더에 `X-Request-Id` 가 있는가 (에러 응답 포함) | 4 |
| 헤더로 받은 request_id 를 승계하는가 | 4 |
| YAML 의 endpoint 를 바꾸면 대상이 바뀌는가 | 5 |
| adapter 가 `ConnectError` 를 던지면 정의된 에러 코드로 나오는가 | 6 |
| 요청 파라미터가 deployment 기본값을 올바르게 덮는가 | — |
| `model` 응답 필드가 **논리명**인가 (upstream 명이 아니라) | — |

마지막 항목이 중요하다. 응답 body 에서 논리/물리 분리를 깨뜨리면 안 된다.
실제 deployment 는 `X-Gateway-Deployment` 헤더로 알린다.

---

## 11. 실습 과제 (권장 순서)

설계서 5장 체크리스트와 같은 순서다. 각 단계마다 테스트를 하나 추가한다.

1. `settings.py` 를 그대로 두고 `python -c "from llm_gateway.settings import get_settings; print(get_settings())"` 실행
   → import 경로와 venv 가 제대로 됐는지 확인
2. `core/errors.py` → `core/logging.py` → `core/context.py` (여기서 contextvars 실습)
3. `middleware/request_id.py` → 이 시점에 `/healthz` 만 열어 서버를 띄우고 헤더 확인
4. `registry/loader.py` + `registry/models.py` → YAML 로딩 테스트
5. `adapters/base.py` 확정 → `FakeAdapter` 구현 → **어댑터 없이 서비스 테스트가 도는 상태**를 먼저 만든다
6. `adapters/ollama.py` → 실제 Ollama 로 수동 확인
7. `service/chat_service.py` → `api/routes/*` → `main.py`
8. Spring `base-url` 변경 후 실제 호출

> 5번이 중요하다. **실물 Ollama 를 붙이기 전에 테스트가 도는 상태**를 만들어야
> 이후 디버깅에서 "내 코드 문제인지 Ollama 문제인지" 를 가를 수 있다.

---

## 12. 이 Phase 의 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| `stream_chat` 을 `async def` + `return iterator` 로 작성 | 호출부 전체가 어긋남 | 본문에 `yield` 를 넣어 async generator 로 |
| `httpx.AsyncClient` 를 요청마다 생성 | 커넥션 풀 붕괴 | factory 캐시 |
| `aclose()` 미호출 | uvicorn 종료 무한 대기 | lifespan 에서 `close_all()` |
| 나노초 → 초 변환 누락 | 지표가 10억 배 | `/ 1e9` |
| `usage` 를 0 으로 채움 | Phase 2/3 오염 | `None` 유지 |
| SSE 에 `\n\n` 하나만 | 클라이언트가 파싱 못 함 | `\n\n` |
| YAML 읽을 때 인코딩 미지정 | Windows 에서 `UnicodeDecodeError` | `encoding="utf-8"` |
| 검증 에러에 body 값 노출 | 프롬프트 유출 | 필드명만 남기기 |
| 미들웨어 등록 순서 반대 | 로그에 request_id 없음 | request_id 를 **나중에** add |
| 응답 `model` 에 upstream 모델명 | 논리/물리 분리 붕괴 | 논리명 + 헤더로 deployment 노출 |

---

## 13. 다음 Phase 를 위해 남겨야 할 것

Phase 1 의 진짜 산출물은 동작하는 서버가 아니라 **확장 지점**이다.

| 남길 자리 | 쓰이는 곳 |
|---|---|
| `ChatService._select()` 를 별도 메서드로 분리 | Phase 5 에서 내부만 router 호출로 교체 |
| `ChatService._aggregate_stream()` | Phase 2 에서 non-stream 요청의 TTFT 확보 |
| `Stopwatch` / `ChatTimings` (`core/timing.py`) | Phase 2 계측 |
| `RoutingContext` 없이도 `ctx` 에 `session_id`/`request_type` 필드 존재 | Phase 5, 7 |
| `ctx.stream_started` 플래그 | Phase 6 재시도 차단 |
| `ModelDeployment.weight` (사용 안 하지만 스키마에 존재) | Phase 5 |

**"지금 안 쓰는 필드를 왜 넣는가" 에 대한 답: 설정 포맷을 나중에 바꾸지 않기 위해서다.**
