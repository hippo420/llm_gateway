# Phase 2 학습 — 계측 & 관측 (Instrumentation & Observability)

> 설계서: [`../docs/phases/phase-02-instrumentation.md`](../docs/phases/phase-02-instrumentation.md)
> 명세: [`../docs/specs/metrics-spec.md`](../docs/specs/metrics-spec.md),
> [`../docs/operations/observability-stack.md`](../docs/operations/observability-stack.md)
> 선행: Phase 1 완료 (특히 `Stopwatch` 자리와 `_aggregate_stream` 자리)

**이 Phase 가 프로젝트의 핵심이다.** Python 지식보다 **측정의 개념**이 어렵다.
Spring 에서 Micrometer 를 써봤다면 도구는 익숙하지만, LLM 특유의 지표는 처음일 것이다.

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | Prometheus 데이터 모델 (Counter/Gauge/Histogram) | Micrometer | 중 |
| 2 | `prometheus-client` 라이브러리 | `MeterRegistry` | 하 |
| 3 | 히스토그램과 백분위 — **왜 평균을 쓰면 안 되는가** | 동일 | **상** |
| 4 | 카디널리티 관리 | 동일 (Micrometer tag) | **상** |
| 5 | 단조 시계 `time.perf_counter()` vs `time.time()` | `System.nanoTime()` vs `currentTimeMillis()` | 하 |
| 6 | 표준 `logging` 으로 JSON 로그 만들기 | Logback JSON encoder | 중 |
| 7 | ASGI mount (`/metrics`) | actuator 엔드포인트 | 하 |
| 8 | Docker Compose + Prometheus + Grafana | 동일 | 중 |
| 9 | PromQL 기초 | 동일 | **상** |
| 10 | (후반) OpenTelemetry Python SDK | Sleuth / Micrometer Tracing | 상 |

---

## 1. LLM 지표가 왜 특별한가 (개념 — 가장 중요)

REST API 는 "응답까지 걸린 시간" 하나면 충분하다. LLM 은 아니다.

```text
Total Latency
 ├── Queue Latency       요청이 서빙 큐에서 대기한 시간
 ├── Prompt Processing   입력 토큰 처리 (prefill)
 ├── TTFT                첫 토큰까지 (= queue + prefill + α)
 └── Generation          나머지 토큰 생성 (decode)
```

| 지표 | 나쁠 때 의심할 것 |
|---|---|
| **TTFT** | 큐 / 동시성 / 컨텍스트 길이 / GPU 스케줄링 |
| **TPS** | decode 단계 GPU 병목 / batch 설정 / 양자화 |

**두 지표는 원인이 다르다. 합쳐서 보면 원인을 못 찾는다.**
Phase 3 의 규칙 R1~R6 이 전부 이 분리에 의존한다.

---

## 2. 시간 측정 — Python 의 시계

```python
import time

time.time()            # epoch 초. 시스템 시계 변경(NTP)에 영향받는다 → 구간 측정에 쓰지 말 것
time.perf_counter()    # 단조 증가 고해상도 카운터 → 구간 측정용
time.monotonic()       # 단조 증가 (해상도 낮음)
```

| 용도 | 함수 | Java |
|---|---|---|
| 구간(latency, TTFT) 측정 | `time.perf_counter()` | `System.nanoTime()` |
| 로그/응답의 timestamp | `time.time()` / `datetime.now(UTC)` | `currentTimeMillis()` |

`ChatCompletionResponse.created` 는 epoch **초(int)** 다 → `int(time.time())`.
latency 는 `perf_counter()` 차이다. **섞으면 안 된다.**

### Stopwatch 구현 스케치 (`core/timing.py`)

```python
class Stopwatch:
    def start(self) -> "Stopwatch":
        self._t0 = time.perf_counter()
        return self

    def mark_first_token(self) -> None:
        if self._t_first is None:              # 첫 번째만 기록 (멱등)
            self._t_first = time.perf_counter()

    def finish(self) -> ChatTimings:
        t_end = time.perf_counter()
        ttft = None if self._t_first is None else self._t_first - self._t0
        total = t_end - self._t0
        return ChatTimings(ttft_sec=ttft, total_sec=total, ...)
```

`mark_first_token` 을 **멱등하게** 만드는 게 포인트다. 매 chunk 마다 호출해도 안전해야 한다.

---

## 3. TTFT 측정 — 이 Phase 의 1번 함정

```text
t0 = adapter 요청 직전
t1 = 첫 content chunk 수신 시각      ← "content 가 비어있지 않은" 첫 chunk
TTFT = t1 - t0
```

**OpenAI 호환 스트림의 첫 chunk 는 `delta.role="assistant"` 만 담긴 경우가 많다.**
이걸 첫 토큰으로 세면 TTFT 가 실제보다 짧게 나온다.

```python
async for chunk in adapter.stream_chat(req):
    if chunk.delta:                     # 빈 문자열은 False → 자연스럽게 걸러진다
        stopwatch.mark_first_token()
        ctx.stream_started = True       # Phase 6 재시도 차단 플래그도 여기서 함께
    ...
```

`if chunk.delta:` 한 줄이 전부다. Python 의 진위값 규칙(빈 문자열 = False)을 이용한다.

### non-streaming 요청의 TTFT

Phase 1 에서 `_aggregate_stream()` 자리를 비워둔 이유가 이것이다.
클라이언트가 `stream: false` 로 요청해도 **내부적으로는 streaming 호출**하고 chunk 를 합쳐 반환한다.
그래야 모든 요청에서 TTFT 를 얻는다.

---

## 4. TPS 계산 — 0으로 나누기

```python
generation_sec = total_sec - ttft_sec
output_tps = output_tokens / generation_sec
```

**division guard 가 필수다** (설계서 3.2). 매우 짧은 응답에서 `generation_sec` 이 0에 수렴한다.

```python
MIN_GEN_SEC = 1e-3

if output_tokens is None or generation_sec is None or generation_sec < MIN_GEN_SEC:
    output_tps = None          # 0 이 아니라 None. 기록 자체를 건너뛴다
else:
    output_tps = output_tokens / generation_sec
```

> Python 에서 `1/0` 은 `ZeroDivisionError` 예외지만, `1.0/0.0` 도 예외다
> (Java 의 float 처럼 `Infinity` 가 되지 않는다). 어느 쪽이든 guard 를 둔다.
> **없는 값을 0 이나 Infinity 로 기록하면 백분위가 통째로 망가진다.**

---

## 5. prometheus-client

### 5.1 Micrometer 와의 대응

| Micrometer | prometheus-client |
|---|---|
| `Counter` | `Counter` |
| `Gauge` | `Gauge` |
| `Timer` / `DistributionSummary` | `Histogram` (또는 `Summary`) |
| `MeterRegistry` | 전역 `REGISTRY` (모듈 임포트만으로 등록) |
| `Tags.of("model", m)` | `.labels(model=m)` |

### 5.2 정의

```python
from prometheus_client import Counter, Histogram, Gauge

REQUESTS = Counter(
    "llm_gateway_requests_total",
    "총 chat 요청 수",
    ["model", "deployment_id", "adapter", "stream", "status"],
)

TTFT = Histogram(
    "llm_gateway_ttft_seconds",
    "첫 토큰까지 걸린 시간",
    ["model", "deployment_id"],
    buckets=(0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 8, 13, 21, float("inf")),
)
```

**메트릭 객체는 모듈 최상위에 한 번만 선언한다.** 함수 안에서 만들면
같은 이름을 두 번 등록해 `Duplicated timeseries` 로 죽는다 (Python 모듈은 한 번만 실행되므로 자연스럽게 싱글턴).

> 테스트에서 앱을 여러 번 만들면 이 재등록 문제가 터진다.
> 대응: 메트릭 모듈을 import 만 하고 재정의하지 않기 / 테스트에서 `REGISTRY` 를 건드리지 않기.

### 5.3 기록

```python
REQUESTS.labels(
    model=model, deployment_id=dep_id, adapter=adapter,
    stream=str(is_stream).lower(), status="success",
).inc()

if ttft is not None:
    TTFT.labels(model=model, deployment_id=dep_id).observe(ttft)
```

**label 을 하나라도 빠뜨리면 런타임 에러다.** 정의한 label 전부를 매번 넘겨야 한다.

### 5.4 `/metrics` 노출

```python
from prometheus_client import make_asgi_app

if settings.metrics_enabled:
    app.mount("/metrics", make_asgi_app())
```

`mount` 는 서브 ASGI 앱을 붙이는 것이다. actuator 엔드포인트를 다는 것과 같다.

### 5.5 히스토그램 버킷 설계 (여기서 실수하면 되돌리기 어렵다)

Prometheus 히스토그램은 **미리 정한 버킷 경계**로만 백분위를 근사한다.
버킷이 잘못되면 P95 가 무의미해진다.

| 지표 | 버킷 성격 |
|---|---|
| TTFT | 0.05 ~ 20초, **앞쪽을 촘촘하게** (정상값이 1초 이하) |
| Total latency | 0.1 ~ 300초, 지수 간격 |
| Output TPS | 1 ~ 200 (지표 성격상 Gauge/Histogram 둘 다 검토) |
| tokens | 16 ~ 32768, 2배씩 |

```python
from prometheus_client.utils import INF
# 지수 버킷 생성 도우미가 없으므로 직접 만든다
BUCKETS = tuple(0.05 * (1.6 ** i) for i in range(16)) + (INF,)
```

**버킷을 바꾸면 과거 데이터와 비교가 불가능해진다.** Phase 2 초반에 신중히 정하고 문서에 남긴다.

### 5.6 왜 평균이 아니라 백분위인가

LLM 응답 시간은 **롱테일 분포**다. 평균 2초여도 P95 가 15초면 사용자 20명 중 1명은 최악을 겪는다.
Phase 3 의 규칙, Phase 7 의 A/B 비교가 전부 P95 기준이다. 평균은 쓰지 않는다.

---

## 6. 카디널리티 — 어기면 Prometheus 가 죽는다

시계열 개수 = **모든 label 값 조합의 곱**.

```text
model(3) × deployment_id(4) × adapter(2) × stream(2) × status(2) = 96 개  → 안전
model(3) × request_id(무한)                                      → 폭발
```

| 허용 label | 금지 label |
|---|---|
| `model`, `deployment_id`, `adapter`, `stream`, `status`, `error_type`, `finish_reason` | `request_id`, `user_id`, `prompt`, `content`, 모든 자유 문자열 |

**`error_type` 은 반드시 열거형이다.** upstream 에러 메시지를 그대로 넣으면
`"connection refused to 127.0.0.1:11434"` 같은 문자열이 매번 새 시계열을 만든다.

```python
def classify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "upstream_unavailable"
    if isinstance(exc, httpx.ReadTimeout):
        return "upstream_timeout"
    return "internal_error"          # 모르는 건 전부 한 바구니에
```

request_id 는 **로그에만** 남긴다. 그게 로그와 메트릭의 역할 분담이다.

확인 방법: Prometheus 에서 `count by (__name__)({__name__=~"llm_gateway.*"})`

---

## 7. 토큰 출처 구분

```python
usage.source = "upstream"    # Ollama 가 준 값
usage.source = "estimated"   # tokenizer 로 근사한 값
```

메트릭 label 로 `token_source` 를 붙인다. **섞으면 비용 계산과 A/B 비교가 전부 오염된다.**
값이 아예 없으면 **기록하지 않는다** (0 으로 채우지 않는다).

---

## 8. JSON 구조화 로깅

### 8.1 Formatter 직접 구현

```python
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        for key, value in getattr(record, "__dict__", {}).items():
            if key not in _STD_FIELDS:        # extra= 로 넘긴 값만 추가
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)   # 한글이 \uXXXX 로 깨지지 않게
```

`ensure_ascii=False` 를 빼먹으면 한글 로그가 전부 유니코드 이스케이프로 나온다.

### 8.2 요청 요약 1줄

설계서 요구사항: 요청당 요약 로그 한 줄.

```python
log.info(
    "chat_completed",
    extra={
        "model": model, "deployment_id": dep_id,
        "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
        "ttft_sec": ttft, "total_sec": total, "status": "success",
    },
)
```

**프롬프트 원문은 넣지 않는다** (`GATEWAY_LOG_PROMPT=false` 가 기본).

### 8.3 uvicorn 로거 정리

uvicorn 은 자체 access log 를 남기는데 request_id 를 모른다.
`main()` 에서 `access_log=False, log_config=None` 으로 끄고, `AccessLogMiddleware` 로 대체한다
(이미 스켈레톤에 되어 있다).

---

## 9. Observability Stack

### 9.1 구성

```text
LLM Gateway  ──/metrics──┐
Ollama       ──/metrics──┤
node_exporter────────────┼──► Prometheus ──► Grafana
nvidia/DCGM exporter ────┘
```

**Gateway 는 L1/L2 만 만든다.** L3(서빙 내부), L4(GPU/호스트)는 Prometheus 가 직접 scrape 한다.
Gateway 가 GPU 지표를 대신 수집하려 들면 안 된다 — 책임이 섞인다.

### 9.2 Docker Compose (WSL 환경 주의)

```yaml
services:
  prometheus:
    image: prom/prometheus
    ports: ["9090:9090"]
    volumes: ["./prometheus.yml:/etc/prometheus/prometheus.yml:ro"]
  grafana:
    image: grafana/grafana
    ports: ["3000:3000"]
```

```yaml
# prometheus.yml
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: llm-gateway
    static_configs:
      - targets: ["host.docker.internal:8080"]   # 컨테이너에서 호스트를 보는 주소
```

**Windows/WSL 네트워크가 이 Phase 의 실질적 난관이다.**

| 상황 | 주소 |
|---|---|
| 컨테이너 → 호스트(Windows) | `host.docker.internal` |
| Windows → WSL 서비스 | `localhost` (WSL2 는 대개 포워딩됨) |
| WSL → Windows | `/etc/resolv.conf` 의 nameserver IP |

`localhost` 를 코드에 하드코딩하지 않는다 — Phase 1 리스크 표 마지막 항목과 같은 이유다.

### 9.3 Grafana

- 데이터소스로 Prometheus 추가 (`http://prometheus:9090`)
- 대시보드 4종: Overview / Model / Serving / GPU
- **대시보드 JSON 을 `grafana/dashboards/` 에 커밋한다.** UI 로만 만들면 재현이 불가능하다.

---

## 10. PromQL 기초 (SQL 과 다른 사고방식)

| 하고 싶은 것 | 쿼리 |
|---|---|
| 초당 요청 수 | `rate(llm_gateway_requests_total[5m])` |
| 모델별 요청 수 | `sum by (model) (rate(llm_gateway_requests_total[5m]))` |
| 에러율 | `sum(rate(...{status="error"}[5m])) / sum(rate(...[5m]))` |
| TTFT P95 | `histogram_quantile(0.95, sum by (le) (rate(llm_gateway_ttft_seconds_bucket[5m])))` |
| 평균 응답 토큰 | `rate(..._sum[5m]) / rate(..._count[5m])` |

핵심 개념 3가지:

1. **`rate()` 는 Counter 에만 쓴다.** Counter 는 단조 증가하는 누적값이고,
   `rate` 가 초당 증가량으로 바꿔준다. Gauge 에 쓰면 안 된다.
2. **`histogram_quantile` 은 `_bucket` 시계열과 `le` label 이 필요하다.**
   `sum by (le)` 를 빠뜨리는 게 가장 흔한 실수다.
3. **`[5m]` 은 룩백 윈도우다.** scrape 간격의 최소 4배는 되어야 한다 (15s scrape → 1m 이상).

Phase 3 의 진단 엔진이 이 쿼리들을 HTTP API 로 그대로 호출한다. 여기서 쓴 쿼리를 재사용하게 된다.

---

## 11. Correlation (Spring ↔ Gateway)

### 11.1 최소 구현 — 이것부터 한다

```text
Spring MDC 요청 ID ──X-Request-Id 헤더──► Gateway (승계) ──응답 헤더로 반환
                                              └─► JSON 로그의 request_id 필드
```

Phase 1 에서 이미 구현했다면 Phase 2 에서는 **Spring 쪽에서 헤더를 실어 보내는 작업**만 남는다.
양쪽 로그의 필드명을 `request_id` 로 통일하는 것이 포인트다.

### 11.2 OpenTelemetry (후반)

```powershell
pip install opentelemetry-sdk opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx
```

- W3C `traceparent` 헤더로 trace 컨텍스트를 전파한다
- FastAPI/httpx 자동 계측(instrumentation)을 붙이면 코드 변경이 거의 없다
- **11.1 없이 11.2 부터 하지 않는다** (설계서 6.2 명시)

---

## 12. 계측 코드를 어디에 넣는가

Phase 1 에서 자리를 비워뒀으므로, **`ChatService` 한 곳에만 추가**된다.

```python
timings = sw.finish()
record_chat_metrics(          # ← Phase 2 가 추가하는 유일한 줄
    model=request.model,
    deployment=deployment,
    timings=timings,
    usage=adapter_response.usage,
    stream=request.stream,
    status="success",
)
```

**라우터나 adapter 에 계측 코드를 흩뿌리지 않는다.** 계측 지점이 여러 곳이면
Phase 5(라우팅) / Phase 6(재시도) 이후 중복 카운트가 발생한다.

에러 경로도 잊지 말 것 — `gateway_error_handler` 에서 `record_error()` 를 호출한다.

---

## 13. 실습 과제

1. `observability/metrics.py` 에 Counter 1개(`requests_total`)만 정의하고 `/metrics` 노출 → curl 로 확인
2. `ChatService` 에 기록 한 줄 추가 → 요청 몇 번 보내고 숫자가 오르는지 확인
3. Histogram 추가 → `_bucket`, `_sum`, `_count` 세 시계열이 생기는 것을 눈으로 확인
4. `FakeAdapter(first_token_delay=0.5)` 로 TTFT 가 0.5 근처로 기록되는지 **테스트**로 검증
5. Docker Compose 로 Prometheus 기동 → target 이 UP 인지 확인
6. Grafana 에서 `histogram_quantile` 패널 하나 만들기
7. 부하를 주고(간단한 스크립트) Overview 대시보드 완성
8. `count by (__name__)` 으로 시계열 수 확인 (카디널리티 점검)

---

## 14. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| role-only chunk 를 첫 토큰으로 계산 | TTFT 과소평가 | `if chunk.delta:` |
| `time.time()` 으로 구간 측정 | NTP 보정 시 음수 latency | `perf_counter()` |
| 나노초 변환 누락 (Ollama) | 지표 10억 배 | `/ 1e9` |
| `generation_sec` 0 나눗셈 | `ZeroDivisionError` 또는 무한대 TPS | guard + `None` |
| 없는 값을 0 으로 기록 | 백분위 왜곡 | 기록 건너뛰기 |
| 메트릭을 함수 안에서 정의 | `Duplicated timeseries` | 모듈 최상위 |
| `error_type` 에 원문 메시지 | 카디널리티 폭발 | 열거형으로 분류 |
| `sum by (le)` 누락 | `histogram_quantile` 오류/이상값 | 쿼리 수정 |
| 버킷을 나중에 변경 | 과거 데이터와 비교 불가 | 초반에 확정 + 문서화 |
| `ensure_ascii` 기본값 | 한글 로그 깨짐 | `ensure_ascii=False` |
| adapter/라우터에도 계측 추가 | 중복 카운트 | ChatService 한 곳만 |

---

## 15. 다음 Phase 진입 조건 (Python 이 아니라 데이터 문제)

- **최소 2주치 지표 축적.** 임계값을 데이터 없이 정하면 Phase 3 은 오탐 생성기가 된다.
- 정상 상태 baseline(TTFT P95, Output TPS, GPU util)을 **문서에 숫자로** 기록.

이 조건은 코드로 앞당길 수 없다. Phase 2 를 끝내고 나면 **기다리는 기간**이 필요하다.
그 사이에 Phase 4~6(코드 작업)을 먼저 진행하는 것이 현실적이다
(`docs/README.md` 3장의 "Phase 6 은 5보다 먼저 착수해도 무방" 과 같은 취지).
