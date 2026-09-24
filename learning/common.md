# 공통 — Java 백엔드 개발자를 위한 Python / 이 프로젝트의 기반 지식

> 대상: **Spring Boot 로 백엔드를 짜 봤지만 Python 은 처음**인 사람.
> 목적: Phase 1~10 을 관통하는 공통 지식만 여기 모은다.
> Phase 별로 새로 필요한 것은 `learn-phase#01.md` ~ `learn-phase#10.md` 에 있다.

읽는 순서:

```text
common.md (2~3장은 코드 짜기 전에 반드시)
   └─► learn-phase#01.md ─► #02 ─► #03 ─► ... ─► #10
```

전 Phase 공통 원칙은 [`../docs/00-architecture.md`](../docs/00-architecture.md) "7. 개발 규약" 이 정본이다.
이 문서는 그 규약을 **지키려면 Python 의 무엇을 알아야 하는가**를 설명한다.

---

## 0. 한 장 요약 — Spring ↔ Python 대응표

| Spring / Java | 이 프로젝트 | 위치 |
|---|---|---|
| `pom.xml` / `build.gradle` | `pyproject.toml` + `requirements.txt` | 루트 |
| Maven Central | PyPI (`pip install`) | |
| JVM/의존성 격리 | **가상환경(venv)** | `.venv/` |
| `@RestController` | `APIRouter` + `@router.post(...)` | `api/routes/*.py` |
| `@RequestBody` DTO + Bean Validation | **Pydantic `BaseModel`** | `schemas/chat.py` |
| Jackson | Pydantic (직렬화/역직렬화 겸함) | |
| `@Autowired` / 생성자 주입 | `Depends(...)` | `api/dependencies.py` |
| `@Configuration` / `@Bean` | app factory 함수 `create_app()` | `main.py` |
| `@PostConstruct` / `@PreDestroy` | **lifespan** (`asynccontextmanager`) | `main.py` |
| `Filter` / `HandlerInterceptor` | **ASGI Middleware** | `middleware/*.py` |
| `@ControllerAdvice` + `@ExceptionHandler` | `app.add_exception_handler(...)` | `main.py` |
| `interface` / `abstract class` | `ABC` + `@abstractmethod` | `adapters/base.py` |
| `record` / Lombok `@Value` | `@dataclass` | `adapters/base.py` |
| `enum` | `enum.Enum` / `Literal[...]` | |
| `ThreadLocal` / MDC | **`contextvars.ContextVar`** | `core/context.py` |
| `RestTemplate` / `WebClient` | **`httpx.AsyncClient`** | `adapters/ollama.py` |
| Micrometer | **`prometheus-client`** | `observability/metrics.py` |
| Logback + JSON encoder | 표준 `logging` + 커스텀 Formatter | `core/logging.py` |
| JUnit 5 + Mockito | **pytest + monkeypatch** | `tests/` |
| `@BeforeEach` / 테스트용 `@Bean` | **fixture** (`conftest.py`) | `tests/conftest.py` |
| Checkstyle / SpotBugs | **ruff** | `pyproject.toml` |
| 컴파일러 타입 검사 | **mypy** (선택적, 실행에는 영향 없음) | `pyproject.toml` |
| 내장 Tomcat | **uvicorn** (ASGI 서버) | |
| Reactor / WebFlux | **asyncio** (언어 내장) | |
| `application.yml` | `.env`(프로세스) + `config/gateway.yaml`(모델) | 9장 참고 |

---

## 1. 개발 환경

### 1.1 가상환경(venv) — Java 에 없는 개념

Java 는 의존성이 프로젝트 `target/` 에 격리되지만, Python 은 기본적으로 **인터프리터 전역에 설치**된다.
그래서 프로젝트마다 인터프리터 사본을 따로 만든다. 그게 venv 다.

```powershell
python -m venv .venv          # .venv/ 생성
.venv\Scripts\activate        # Windows PowerShell
# source .venv/bin/activate   # WSL / Linux
```

활성화되면 프롬프트 앞에 `(.venv)` 가 붙는다. **활성화하지 않고 `pip install` 하면 전역이 오염된다.**
`.venv/` 는 `.gitignore` 대상이다 (`target/` 과 같은 취급).

### 1.2 의존성

```powershell
pip install -r requirements-dev.txt   # 런타임 + 개발 도구
pip install -e .                      # 이 프로젝트 자체를 "편집 가능" 모드로 설치
```

`pip install -e .` 이 하는 일: `src/llm_gateway` 를 import 경로에 등록한다.
이게 없으면 `from llm_gateway.core.errors import ...` 가 `ModuleNotFoundError` 로 죽는다.
Java 로 치면 자기 모듈을 클래스패스에 넣는 작업이다.

> `requirements.txt` = Maven 의 `<dependencies>`.
> `pyproject.toml` = `pom.xml` 전체(빌드 + 도구 설정 + 메타).
> 이 저장소는 둘 다 두되 `pyproject.toml` 을 정본으로 본다.

### 1.3 src 레이아웃

```text
src/llm_gateway/...     ← 실제 패키지 (src/main/java)
tests/                  ← 테스트     (src/test/java)
```

패키지 디렉터리마다 `__init__.py` 가 있다. Java 의 `package-info.java` 와 비슷하되,
**없으면 패키지로 인식되지 않을 수 있으므로 빈 파일이라도 둔다.**

### 1.4 실행 / 검사

```powershell
uvicorn llm_gateway.main:app --reload   # 개발 서버 (--reload = 파일 변경 시 재기동)
pytest                                  # 테스트
ruff check src tests                    # 린트
ruff check --fix src tests              # 자동 수정
mypy                                    # 타입 검사
```

`llm_gateway.main:app` = "`llm_gateway/main.py` 모듈의 `app` 변수".

---

## 2. 언어 — Java 개발자가 헛디디는 지점만

### 2.1 타입 힌트는 "주석"이다

```python
def resolve(self, model: str) -> ModelDeployment: ...
```

Java 와 달리 **런타임에 검사하지 않는다.** `str` 자리에 `int` 를 넣어도 실행된다.
검사는 `mypy` 가 별도로 한다. 그래도 반드시 붙인다 — 이 저장소의 규약이다.

| Java | Python |
|---|---|
| `String s` | `s: str` |
| `Optional<String>` / nullable | `s: str \| None` |
| `List<String>` | `list[str]` |
| `Map<String, Object>` | `dict[str, Any]` |
| `Object` | `Any` |
| `static final` 상수 | `NAME: ClassVar[str] = ""` |
| enum 값만 허용 | `Literal["json", "text"]` |
| `Iterator<T>` (비동기) | `AsyncIterator[T]` |

파일 맨 위의 `from __future__ import annotations` 는 "타입 힌트를 문자열로 취급하라"는 지시다.
순환 import 와 forward reference 문제를 없애준다.
**이 저장소의 모든 파일에 들어 있고, 새 파일에도 넣는다.**

### 2.2 `None` 은 `null` 이지만, 이 프로젝트에서는 의미가 더 강하다

```python
input_tokens: int | None = None   # None = "모른다", 0 = "토큰이 없었다"
```

`docs/00-architecture.md` 규약 4번과 `adapters/base.py` 의 `AdapterUsage` 주석이 반복해 강조한다.
**모르는 값을 0 으로 채우면 Phase 2 의 평균·백분위가 오염되고 Phase 3 진단이 틀린다.**

None 비교는 `==` 가 아니라 `is` 로 한다.

```python
if usage.input_tokens is None:      # O
if usage.input_tokens == None:      # X (ruff 가 잡는다)
```

### 2.3 dataclass = record / Lombok

```python
@dataclass
class AdapterMessage:
    role: str
    content: str
```

`__init__`, `__eq__`, `__repr__` 이 자동 생성된다. Java `record` 와 거의 같다.

**함정 — mutable default:**

```python
extra: dict[str, Any] = field(default_factory=dict)   # O
extra: dict[str, Any] = {}                            # X: 모든 인스턴스가 같은 dict 를 공유
```

Java 에는 없는 함정이다. 기본값이 **정의 시점에 한 번만** 평가되기 때문이다.
일반 함수 인자도 마찬가지 (`def f(items=[])` 금지).

### 2.4 ABC = interface + abstract class

```python
class LLMAdapter(ABC):
    @abstractmethod
    async def chat(self, request: AdapterChatRequest) -> AdapterChatResponse: ...

    async def aclose(self) -> None:      # 기본 구현이 있는 메서드도 가능
        return None
```

`@abstractmethod` 미구현은 **인스턴스화 시점**에 `TypeError` 로 터진다 (컴파일 타임이 아니다).
다중 상속이 가능하지만 이 프로젝트에서는 쓰지 않는다.

구조적 타이핑인 `Protocol` 도 있으나, 이 저장소는 명시적 `ABC` 를 쓴다 — "경계는 눈에 보여야 한다".

### 2.5 접근 제어자가 없다

`private` / `public` 이 없고 관례만 있다.

```python
self._registry     # 앞의 _ = "내부용". 문법적 강제는 없다
def _select(...)   # 내부 메서드
```

`ChatService._select()` 처럼 `_` 로 시작하는 메서드는 **Phase 5 에서 교체될 자리**라는 설계 의도의 표현이다.
밖에서 호출하지 않는다.

### 2.6 `self` 는 명시적이다

```python
class ChatService:
    def __init__(self, registry: ModelRegistry, adapters: AdapterFactory) -> None:
        self._registry = registry          # 대입이 곧 필드 선언
```

- 생성자는 `__init__`
- 인스턴스 메서드의 첫 인자는 항상 `self` (Java 의 암묵적 `this`)

### 2.7 읽을 수만 있으면 되는 문법

```python
# f-string (String.format)
msg = f"model={model} took {elapsed:.3f}s"

# 컴프리헨션 (Stream map/filter 축약)
ids = [d.id for d in deployments if d.enabled]
by_id = {d.id: d for d in deployments}

# 언패킹
first, *rest = candidates

# dict 병합 — Phase 1 파라미터 병합에서 그대로 쓴다
merged = {**defaults, **deployment_options, **request_options}

# 키워드 전용 인자 (* 뒤는 반드시 이름으로 전달)
def __init__(self, deployment, *, chunks=None, error=None): ...

# 삼항
value = a if cond else b

# 진위값: 빈 문자열/빈 리스트/0/None 은 전부 False
if not candidates:
    raise NoAvailableDeploymentError(...)
```

### 2.8 예외

- **checked exception 이 없다.** `throws` 가 없으므로 무엇이 튀어나오는지는 docstring 으로 알린다.
- 계층 구조는 Java 와 같다 (`GatewayError` → 하위 에러). `core/errors.py` 참고.
- 원인 체이닝: `raise UpstreamError(...) from exc` (= `new E(msg, cause)`)
- `try/except/else/finally` — `else` 는 예외가 없을 때만 실행되는 블록 (Java 에 없음)

```python
try:
    resp = await client.post(url, json=payload)
except httpx.ConnectError as exc:
    raise UpstreamUnavailableError(...) from exc
```

### 2.9 `with` = try-with-resources

```python
with open(path) as f:                        # 블록을 나가면 자동 close
    data = yaml.safe_load(f)

async with httpx.AsyncClient() as client:    # 비동기 버전
    ...
```

`AutoCloseable` 에 해당하는 것이 context manager 다.
`@asynccontextmanager` 로 직접 만들 수도 있다 (`main.py` 의 `lifespan` 이 그 예).

---

## 3. asyncio — 이 프로젝트의 실행 모델

**전 Phase 를 통틀어 가장 중요한 공통 지식이다.** Spring MVC 만 해봤다면 여기에 시간을 써야 한다.

### 3.1 스레드가 아니라 이벤트 루프다

| Spring MVC | 이 프로젝트 |
|---|---|
| 요청당 스레드 1개 (톰캣 풀 200개) | **스레드 1개 + 이벤트 루프**, 요청은 코루틴 |
| 블로킹해도 다른 스레드가 다른 요청 처리 | **블로킹하면 서버 전체가 멈춘다** |
| `synchronized`, `ConcurrentHashMap` 필요 | 단일 스레드라 대부분 불필요 (3.5 참고) |

WebFlux 경험이 있다면 그것과 가깝다. `Mono/Flux` → `코루틴 / async generator`.

### 3.2 async / await

```python
async def chat(self, request):      # 코루틴 함수
    resp = await client.post(...)   # await 지점에서 제어권을 이벤트 루프에 넘긴다
    return resp
```

- `async def` 함수는 호출해도 실행되지 않는다. **`await` 해야 실행된다.**
  (`CompletableFuture` 를 만들기만 하고 `join()` 안 한 상태)
- `await` 는 `async def` 안에서만 쓸 수 있다.
- 흔한 실수: `await` 누락 → 코루틴 객체가 그대로 반환되고
  `RuntimeWarning: coroutine was never awaited` 만 뜬 채 조용히 오동작한다.

### 3.3 절대 하면 안 되는 것 — 블로킹

```python
time.sleep(1)              # X  이벤트 루프 전체 정지
requests.get(url)          # X  동기 HTTP 라이브러리 (requests 를 안 쓰는 이유)

await asyncio.sleep(1)     # O
await client.get(url)      # O  httpx.AsyncClient
```

CPU 를 오래 먹는 계산(예: Phase 8 의 임베딩 유사도)은 `await asyncio.to_thread(fn, ...)` 로 스레드에 넘긴다.

> GIL: 한 프로세스에서 파이썬 바이트코드는 한 번에 하나만 실행된다.
> 그래서 CPU 병렬화는 스레드가 아니라 **프로세스**(`uvicorn --workers N`)로 한다.
> 이 Gateway 는 I/O 대기가 대부분이라 단일 프로세스로 충분하다.

### 3.4 async generator — 스트리밍의 핵심

Phase 1 부터 끝까지 쓰인다.

```python
async def stream_chat(self, request) -> AsyncIterator[AdapterChatChunk]:
    async for line in response.aiter_lines():
        yield AdapterChatChunk(delta=...)      # 본문에 yield 가 있으면 async generator
```

```python
async for chunk in adapter.stream_chat(req):   # 소비 측
    ...
```

**핵심 규칙 (`adapters/base.py` 주석에도 있다):**
`stream_chat` 은 `async def` + `yield` 로 만든다. 그러면 **호출 즉시 iterator 가 반환된다**(await 불필요).
`yield` 가 없으면 코루틴이 되어 호출부에서 `await` 를 해야 iterator 가 나온다.
이 차이를 놓치면 호출부가 전부 어긋난다.

가장 가까운 Java 대응물은 `Flux<T>` 다.

### 3.5 동시성 / 자료구조 안전성

이벤트 루프는 단일 스레드이므로 **`await` 가 없는 구간은 원자적**이다.

```python
self._snapshot = new_snapshot          # 참조 대입은 안전 (Phase 4 원자적 교체가 이걸 쓴다)
```

`await` 를 사이에 끼면 다른 코루틴이 끼어든다.

```python
value = self._counter
await something()                      # 여기서 다른 코루틴이 값을 바꿀 수 있다
self._counter = value + 1              # 갱신 유실 가능
```

이때만 `asyncio.Lock` 을 쓴다 (`threading.Lock` 이 아니다).

### 3.6 취소(Cancellation)

클라이언트가 스트리밍 도중 연결을 끊으면 `asyncio.CancelledError` 가 발생한다.
Java 의 `InterruptedException` 에 해당한다.

```python
try:
    async for chunk in upstream:
        yield chunk
except asyncio.CancelledError:
    log.info("client disconnected")
    raise                    # 반드시 다시 raise. 삼키면 안 된다
finally:
    await upstream.aclose()  # upstream 연결은 반드시 닫는다
```

`CancelledError` 는 `BaseException` 상속이라 `except Exception` 에 걸리지 않는다 — 의도된 설계다.
bare `except:` 는 이걸 깨뜨리므로 금지.

### 3.7 백그라운드 태스크 (Phase 3, 4, 9, 10)

```python
task = asyncio.create_task(watch_config())     # 시작
...
task.cancel()                                  # 종료
await asyncio.gather(task, return_exceptions=True)
```

`@Scheduled` 의 대체물이다. **lifespan 에서 만들고 lifespan 에서 취소**한다.
`create_task` 의 반환값을 변수에 담아두지 않으면 GC 에 수거될 수 있다.

---

## 4. Pydantic v2 — DTO + 검증 + 직렬화

Jackson + Bean Validation + Lombok 을 하나로 합친 것.

```python
class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stream: bool = False
```

- 객체 생성 시점에 **검증이 실행된다.** 실패 시 `ValidationError`.
  (`@Valid` 가 항상 켜져 있는 상태)
- `ge/le/min_length` = `@Min/@Max/@Size`
- `model.model_dump()` → dict, `model.model_dump_json()` → JSON 문자열.
  `exclude_none=True` 를 자주 쓴다 (OpenAI 응답에서 `null` 필드를 빼야 할 때)
- 예약어 충돌 시 alias: `Field(alias="schema")`

**주의:** Pydantic v1/v2 는 API 가 크게 다르다 (`.dict()` → `.model_dump()`,
`@validator` → `@field_validator`). 인터넷 예제는 **v2 인지 확인**한다. 이 프로젝트는 v2 다.

dataclass 와 Pydantic 의 역할 분담 (이 저장소 규칙):

| 용도 | 선택 |
|---|---|
| 외부 경계(HTTP 요청/응답, YAML 설정) — 검증 필요 | Pydantic `BaseModel` |
| 내부 전달 DTO (`AdapterChatRequest` 등) — 검증 불필요, 가볍게 | `@dataclass` |

---

## 5. FastAPI — Spring Boot 와의 대응

### 5.1 라우터

```python
router = APIRouter(prefix="/v1", tags=["chat"])

@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    body: ChatCompletionRequest,                       # @RequestBody + @Valid
    service: ChatService = Depends(get_chat_service),  # 의존성 주입
) -> ChatCompletionResponse:
    ...
```

- **타입 힌트가 곧 바인딩 규칙이다.** Pydantic 모델이면 body, `str` 이면 query 파라미터.
- 명시하려면 `Header(...)`, `Query(...)`, `Path(...)`.
- OpenAPI 문서가 자동 생성된다 (`/docs`).

### 5.2 의존성 주입 — 컨테이너가 아니다

Spring 은 컨텍스트가 빈을 관리하지만, FastAPI 의 `Depends` 는 **요청마다 함수를 호출**한다.

```python
def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service     # lifespan 에서 만들어둔 싱글턴을 꺼낸다
```

싱글턴은 `app.state` 에 담는다. `@lru_cache` 를 붙인 함수도 싱글턴 역할을 한다
(`settings.py` 의 `get_settings()` 가 그 방식).

테스트에서는 `app.dependency_overrides[get_chat_service] = fake` 로 갈아끼운다 (`@MockBean` 대응).

### 5.3 lifespan — `@PostConstruct` / `@PreDestroy`

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # startup: 설정 로드, registry/adapter 생성
    yield
    # shutdown: adapter 정리, 백그라운드 태스크 취소
```

`yield` 앞이 기동, 뒤가 종료. **종료 처리를 빼먹으면 uvicorn 이 안 죽는다** (`main.py` 주석 참고).

### 5.4 미들웨어 — Filter

```python
app.add_middleware(AccessLogMiddleware)     # 안쪽
app.add_middleware(RequestIdMiddleware)     # 바깥쪽 ← 나중에 add 한 것이 바깥
```

**Servlet Filter 와 순서 규칙이 반대다.** 나중에 등록한 것이 먼저 실행된다.
request_id 가 access log 보다 바깥이어야 로그에 id 가 찍힌다.

### 5.5 예외 핸들러 — `@ControllerAdvice`

```python
app.add_exception_handler(GatewayError, gateway_error_handler)
```

### 5.6 스트리밍 응답

```python
return StreamingResponse(sse_generator(), media_type="text/event-stream")
```

async generator 를 그대로 넘긴다. SSE 포맷은 Phase 1 문서 참고.

---

## 6. httpx — WebClient 대응

```python
self._client = httpx.AsyncClient(base_url=..., timeout=httpx.Timeout(...))

resp = await self._client.post("/api/chat", json=payload)
resp.raise_for_status()
data = resp.json()

async with self._client.stream("POST", "/api/chat", json=payload) as resp:
    async for line in resp.aiter_lines():
        ...
```

**규칙: `AsyncClient` 를 요청마다 새로 만들지 않는다.** 커넥션 풀이 붕괴한다.
그래서 `adapters/factory.py` 가 adapter 인스턴스를 캐시하고, adapter 가 client 를 소유한다.
종료 시 `aclose()` 로 닫는 것까지가 한 세트다.

`requests` 라이브러리는 동기이므로 **이 프로젝트에서 쓰지 않는다.**

---

## 7. 로깅

Java 의 SLF4J 처럼 표준 `logging` 모듈이 파사드 역할을 한다.

```python
import logging
log = logging.getLogger(__name__)   # __name__ = "llm_gateway.service.chat_service"

log.info("chat completed", extra={"model": model, "ttft": ttft})
```

- MDC 는 없다. **`contextvars` + 커스텀 `logging.Filter`** 로 만든다 (`core/logging.py`).
- JSON 출력도 `Formatter` 를 직접 구현한다 (또는 `structlog`).
- 레벨/포맷은 `.env` 의 `GATEWAY_LOG_LEVEL`, `GATEWAY_LOG_FORMAT` 으로 제어한다.
- **프롬프트 원문은 기본적으로 로그에 남기지 않는다** (`GATEWAY_LOG_PROMPT=false`). 규약 5번.

### contextvars = ThreadLocal 의 async 버전

```python
_request_context: ContextVar[RequestContext | None] = ContextVar(..., default=None)
```

`ThreadLocal` 은 스레드 단위지만 코루틴은 스레드를 공유하므로 쓸 수 없다.
`ContextVar` 는 **태스크(코루틴) 단위**로 격리된다. MDC 를 대체하는 유일한 수단이다.

---

## 8. 테스트 (pytest)

```python
@pytest.fixture
def registry() -> ModelRegistry:            # @BeforeEach + 테스트용 @Bean
    return ModelRegistry(...)

async def test_chat_returns_openai_shape(client):   # 이름이 test_ 로 시작하면 테스트
    resp = await client.post("/v1/chat/completions", json={...})
    assert resp.status_code == 200          # assertThat 대신 그냥 assert
```

| JUnit | pytest |
|---|---|
| `@Test` | 함수명 `test_*` |
| `@BeforeEach` / 테스트 `@Bean` | `@pytest.fixture` (인자로 받으면 주입) |
| 공통 설정 클래스 | `conftest.py` (해당 디렉터리 이하 자동 적용) |
| `@ParameterizedTest` | `@pytest.mark.parametrize` |
| `assertThrows` | `with pytest.raises(GatewayError):` |
| Mockito | `monkeypatch` fixture, 또는 이 저장소의 `FakeAdapter` |
| MockMvc / WebTestClient | `httpx.AsyncClient(transport=ASGITransport(app=app))` |

`pyproject.toml` 의 `asyncio_mode = "auto"` 덕분에 `async def test_...` 에 데코레이터를 안 붙여도 된다.

**철칙:** 테스트는 **실제 Ollama 없이 전부 통과**해야 한다 (`tests/conftest.py` 의 `FakeAdapter`).
LLM 이 떠 있어야만 도는 테스트는 회귀 감지에 쓸 수 없다.

---

## 9. 설정의 두 계층 — 경계를 헷갈리지 말 것

| | `.env` (`settings.py`) | `config/gateway.yaml` |
|---|---|---|
| 질문 | "프로세스를 **어떻게 띄우는가**" | "**무엇을** 서빙하는가" |
| 예 | 포트, 로그 레벨, Redis URL, API 키 | 논리 모델, endpoint, timeout, weight |
| 변경 | 재기동 필요 | 무중단 reload (Phase 4) |
| 도구 | `pydantic-settings` | `PyYAML` + Pydantic 검증 |

`GATEWAY_` 접두사 환경변수는 `Settings` 필드에 자동 매핑된다 (`GATEWAY_LOG_LEVEL` → `log_level`).

YAML 은 `yaml.safe_load()` 로만 읽는다. `yaml.load()` 는 임의 객체를 생성할 수 있어 위험하다.

---

## 10. 이 저장소를 읽는 법

1. 스켈레톤의 **docstring 이 사양서다.** 본문은 `raise NotImplementedError` 지만,
   그 위에 구현 순서와 함정이 이미 적혀 있다. 먼저 읽는다.
2. `docs/specs/*.md` 가 계약의 정본이다. 코드와 다르면 **문서가 맞다.**
3. 구현 순서는 각 Phase 문서의 "구현 체크리스트" 를 따른다. 위에서부터 `NotImplementedError` 를 지운다.

---

## 11. Java 개발자가 실제로 밟는 지뢰 목록

| 함정 | 증상 | 대응 |
|---|---|---|
| `await` 누락 | 코루틴 객체 반환, 조용히 오동작 | `RuntimeWarning` 을 무시하지 않기 |
| mutable default 인자 | 인스턴스 간 상태 공유 | `field(default_factory=...)` |
| 블로킹 호출 (`time.sleep`, `requests`) | 서버 전체 응답 정지 | async 라이브러리만 사용 |
| `except Exception` 으로 `CancelledError` 처리 시도 | 클라이언트 끊김 처리 실패, 커넥션 누수 | 취소는 별도 `except` 후 re-raise |
| `httpx.AsyncClient` 매 요청 생성 | 커넥션 풀 붕괴, 소켓 고갈 | factory 캐시 (Phase 1 리스크 표) |
| 순환 import | 기동 시 `ImportError` | `from __future__ import annotations` + `TYPE_CHECKING` |
| `None` 을 `0` 으로 대체 | 지표 오염 → 진단 오류 | 규약 4번. `None` 유지 |
| 가상환경 미활성 | `ModuleNotFoundError` | `.venv\Scripts\activate` |
| Pydantic v1 예제 복붙 | `AttributeError` | v2 API 확인 |
| `hash()` 로 버킷팅 | 프로세스마다 결과가 달라짐 | Phase 5 참고. `hashlib` 사용 |

---

## 12. 최소 학습 자원

전부 읽지 말고 **필요할 때 해당 장만** 본다.

| 주제 | 자원 |
|---|---|
| 언어 문법 속성 | Python 공식 튜토리얼 3~9장 |
| 타입 힌트 | 공식 문서 `typing` 모듈 |
| asyncio | 공식 문서 "Coroutines and Tasks" (**필수**) |
| FastAPI | 공식 튜토리얼 — First Steps ~ Dependencies, Middleware, Lifespan |
| Pydantic | 공식 문서 "Models", "Fields", "Validators" (**v2**) |
| httpx | 공식 문서 "Async Support", "Streaming Responses", "Timeouts" |
| pytest | 공식 문서 "Fixtures", "Parametrize", "monkeypatch" |

> 이 저장소의 `.venv/Lib/site-packages/fastapi/.agents/skills/fastapi/references/` 아래에
> FastAPI 레퍼런스(dependencies / streaming / pydantic / responses)가 들어 있다. 오프라인 참고용.

---

## 13. Phase 별 문서 색인

| Phase | 문서 | 새로 배우는 것 |
|---|---|---|
| 1 | `learn-phase#01.md` | FastAPI, Pydantic, ABC/dataclass, contextvars, httpx 스트리밍, SSE, pytest |
| 2 | `learn-phase#02.md` | prometheus-client, 히스토그램/백분위, 단조 시계, JSON 로깅, PromQL, Docker Compose |
| 3 | `learn-phase#03.md` | Prometheus HTTP API, 백그라운드 루프, 선언적 규칙 엔진, baseline 통계, 시간 처리 |
| 4 | `learn-phase#04.md` | redis-py(asyncio) + pub/sub, 설정 병합, 원자적 스냅샷 교체, 파일 감시, 인증 |
| 5 | `learn-phase#05.md` | 전략 패턴, 결정론적 해시 버킷팅, 가중 선택 알고리즘 |
| 6 | `learn-phase#06.md` | httpx 타임아웃 4종, 예외 분류, 백오프+지터, 상태 머신, 장애 주입 테스트 |
| 7 | `learn-phase#07.md` | 실험 할당, 표본/통계 기초, 부하 스크립트(Semaphore), 시간 분할 벤치마크 |
| 8 | `learn-phase#08.md` | JSONL, numpy/코사인 유사도, LLM-as-Judge, 상관계수, 배치 실행 |
| 9 | `learn-phase#09.md` | 상태 전이 워크플로, 역연산 설계, SQLite 영속화, 알림 연동, guard |
| 10 | `learn-phase#10.md` | 킬 스위치, 변경 예산, 불변 감사 로그, 등급 승격/강등, 카오스 테스트 |
