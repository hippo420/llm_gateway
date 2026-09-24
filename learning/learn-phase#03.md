# Phase 3 학습 — 규칙 기반 진단 엔진

> 설계서: [`../docs/phases/phase-03-rule-based-diagnosis.md`](../docs/phases/phase-03-rule-based-diagnosis.md)
> 선행: Phase 2 완료 + **최소 2주치 지표 축적**

이 Phase 는 새 웹 기능이 아니라 **주기적으로 도는 배치 + 규칙 엔진**이다.
Spring 으로 치면 `@Scheduled` 메서드 하나에 룰 엔진(Drools 급은 아닌)이 붙은 것.

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | Prometheus HTTP Query API 호출 | RestTemplate + 외부 API | 하 |
| 2 | PromQL 실전 (Phase 2 보다 깊게) | — | **상** |
| 3 | asyncio 백그라운드 주기 루프 | `@Scheduled(fixedDelay=)` | 중 |
| 4 | 선언적 규칙 자료구조 (데이터로서의 규칙) | Drools / Spec 패턴 | 중 |
| 5 | `enum` / `operator` 모듈 / `Callable` | enum + Comparator | 하 |
| 6 | baseline 통계 (중앙값, 비율 정규화, EWMA) | — | 중 |
| 7 | 상태 유지 (연속 N회, cooldown) — `deque` | — | 중 |
| 8 | `datetime` / 타임존 / UTC | `Instant` / `ZonedDateTime` | 중 |

---

## 1. Prometheus HTTP API

Prometheus 는 자기 자신이 쿼리 엔진이다. HTTP 로 PromQL 을 던지면 JSON 이 온다.

```python
class PrometheusClient:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    async def query(self, promql: str) -> float | None:
        resp = await self._client.get("/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        body = resp.json()

        if body.get("status") != "success":
            return None

        result = body["data"]["result"]
        if not result:
            return None                      # 데이터 없음 = None (0 이 아니다!)

        return float(result[0]["value"][1])   # ["value"] = [timestamp, "값(문자열)"]
```

주의점:

| 항목 | 내용 |
|---|---|
| 값이 **문자열**로 온다 | `float(...)` 변환 필수 |
| 결과가 비어 있을 수 있다 | `[]` → **`None`**. 0 으로 바꾸면 규칙이 오작동한다 |
| `NaN` 이 올 수 있다 | `histogram_quantile` 이 데이터 부족 시 NaN. `math.isnan()` 체크 |
| 두 종류의 엔드포인트 | `/api/v1/query` (순간값), `/api/v1/query_range` (구간) |

```python
import math

value = await client.query(q)
if value is None or math.isnan(value):
    return None
```

**`float("nan") == float("nan")` 은 False 다.** NaN 비교는 반드시 `math.isnan()` 으로 한다.
이걸 모르면 `if value != value` 같은 코드를 보고 당황하게 된다 (그것도 NaN 판별 관용구다).

---

## 2. SignalSnapshot 설계

```python
@dataclass(frozen=True)
class SignalSnapshot:
    at: datetime
    ttft_p95: float | None = None
    ttft_p95_ratio: float | None = None        # baseline 대비 배수
    output_tps: float | None = None
    output_tps_ratio: float | None = None
    queue_depth: float | None = None
    gpu_utilization: float | None = None
    gpu_memory_used_ratio: float | None = None
    input_tokens_p95: float | None = None
    error_rate: float | None = None
    concurrency: float | None = None
```

- `frozen=True` = 불변 (Java 의 `record`). 스냅샷은 만들어진 뒤 변하면 안 된다.
- **전부 `| None` 이다.** "지표를 못 가져옴" 과 "값이 0" 을 구분해야 한다.
  Ollama 는 queue_depth 를 주지 않으므로 실제로 자주 `None` 이다.

### 절대값이 아니라 비율

```python
ttft_p95_ratio = ttft_p95 / baseline_ttft_p95
```

GPU 나 모델이 바뀌면 절대 임계값(`ttft > 3s`)은 즉시 무의미해진다.
`ratio > 1.5` 는 살아남는다. 설계서가 강조하는 지점이다.

---

## 3. Baseline 계산

### 3.1 단계적으로 간다

| 단계 | 방법 | 시점 |
|---|---|---|
| 1 | **설정 파일의 고정값** | 처음. Phase 2 에서 측정한 값을 손으로 적는다 |
| 2 | rolling median (최근 7일 동시간대) | 데이터가 쌓인 뒤 |
| 3 | EWMA (지수가중이동평균) | 필요 시 |

1단계부터 시작한다. 처음부터 3단계를 만들면 검증할 수가 없다.

### 3.2 Python 통계 도구

```python
import statistics

statistics.median(values)          # 중앙값 — 이상치에 강하다
statistics.mean(values)            # 평균 — LLM 지표에는 부적합
statistics.quantiles(values, n=20)[18]   # 대략 P95
```

**표준 라이브러리 `statistics` 로 충분하다.** numpy 는 Phase 8 전까지 필요 없다.

평균이 아니라 **중앙값**을 쓰는 이유: cold start 한 번이 평균을 크게 끌어올린다.

### 3.3 PromQL 로 직접 구할 수도 있다

```promql
# 지난 7일 같은 시간대 TTFT P95 의 중앙값 (offset 활용)
histogram_quantile(0.95, sum by (le) (rate(llm_gateway_ttft_seconds_bucket[5m] offset 1d)))
```

계산을 Prometheus 에 맡길지 Python 에서 할지는 선택이다.
**Prometheus 에 맡기는 쪽이 코드가 단순하다.**

---

## 4. 규칙을 "데이터"로 정의하기

이 Phase 의 설계 핵심이다. **`if` 문으로 규칙을 쓰지 않는다.**

```python
class Op(str, Enum):                   # str 을 함께 상속하면 JSON 직렬화가 쉬워진다
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="


_OPS: dict[Op, Callable[[float, float], bool]] = {
    Op.GT: operator.gt,                # 표준 라이브러리 operator 모듈
    Op.GTE: operator.ge,
    Op.LT: operator.lt,
    Op.LTE: operator.le,
}


@dataclass(frozen=True)
class Condition:
    signal: str                        # SignalSnapshot 의 필드명
    op: Op
    threshold: float

    def evaluate(self, snap: SignalSnapshot) -> bool | None:
        value = getattr(snap, self.signal, None)
        if value is None:
            return None                # "판단 불가" — False 와 다르다
        return _OPS[self.op](value, self.threshold)


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    conditions: tuple[Condition, ...]
    hypothesis: str
    actions: tuple[str, ...]
```

### 4.1 3-값 논리가 필요하다

`Condition.evaluate()` 가 `True / False / None` 을 반환하는 것에 주목한다.

```text
True   조건 성립
False  조건 불성립
None   신호가 없어서 판단 불가
```

**`None` 을 `False` 로 취급하면 안 된다.**
queue_depth 를 못 가져왔는데 "queue_depth 정상" 으로 판정하면 R2/R5/R6 이 잘못 발화한다.

```python
def evaluate_rule(rule: Rule, snap: SignalSnapshot) -> RuleResult:
    results = [c.evaluate(snap) for c in rule.conditions]
    if any(r is None for r in results):
        return RuleResult(matched=False, reason="insufficient_signal")
    return RuleResult(matched=all(results))
```

### 4.2 왜 데이터로 만드는가

설계서 4장: **Phase 9 에서 같은 Rule 구조에 `actions` 만 붙이면 정책이 된다.**
`if` 문으로 짜두면 Phase 9 에서 전부 다시 짜야 한다.

부수 효과로 규칙을 YAML 로 옮기기 쉬워지고, 규칙 변경 이력이 설정 커밋으로 남는다.

### 4.3 `getattr` — Java 의 리플렉션

```python
value = getattr(snap, "ttft_p95_ratio", None)
```

문자열로 필드에 접근한다. Java 리플렉션보다 훨씬 가볍고 관용적이다.
단, **오타가 런타임까지 안 잡힌다.** 규칙 로딩 시점에 필드명 검증을 넣는다.

```python
_VALID_SIGNALS = {f.name for f in dataclasses.fields(SignalSnapshot)}
if condition.signal not in _VALID_SIGNALS:
    raise ValueError(f"unknown signal: {condition.signal}")
```

---

## 5. R1~R6 을 표현하기

```python
RULES = (
    Rule(
        id="TTFT_QUEUE_GPU_SATURATION",              # R1
        severity="warning",
        conditions=(
            Condition("ttft_p95_ratio", Op.GT, 1.5),
            Condition("queue_depth", Op.GT, 5),
            Condition("gpu_utilization", Op.GT, 90),
        ),
        hypothesis="Serving Concurrency 또는 GPU Scheduling 병목 가능성",
        actions=("Serving concurrency 상한 확인", "동시 요청 수 제한 검토", ...),
    ),
    ...
)
```

R6(cold start) 은 "요청 간격이 김" 이라는 조건이 있다.
이건 지표 하나가 아니라 **파생 신호**다 — `request_rate` 를 조회해 `SignalSnapshot` 에 넣는다.
"조건을 만들 수 없으면 신호를 추가한다" 가 원칙이다. 규칙 구조를 특수화하지 않는다.

---

## 6. 백그라운드 평가 루프

### 6.1 `@Scheduled` 의 대체물

```python
async def diagnosis_loop(engine: DiagnosisEngine, interval_sec: int = 60) -> None:
    while True:
        try:
            await engine.evaluate_once()
        except asyncio.CancelledError:
            raise                                  # 취소는 반드시 통과시킨다
        except Exception:
            log.exception("diagnosis evaluation failed")   # 루프는 죽지 않는다
        await asyncio.sleep(interval_sec)
```

**핵심 규칙 3가지:**

1. `except Exception` 으로 감싸 **루프가 죽지 않게** 한다. 한 번의 조회 실패로 진단이 영구 중단되면 안 된다.
2. `CancelledError` 는 먼저 잡아서 **re-raise** 한다 (common.md 3.6).
   `except Exception` 아래에 두면 안 된다 — 순서가 중요하다.
3. `log.exception()` 은 스택트레이스를 함께 남긴다 (`log.error()` 는 안 남긴다).

### 6.2 lifespan 에 붙이기

```python
# lifespan startup
app.state.diagnosis_task = asyncio.create_task(diagnosis_loop(engine))

# lifespan shutdown
app.state.diagnosis_task.cancel()
await asyncio.gather(app.state.diagnosis_task, return_exceptions=True)
```

`return_exceptions=True` 가 없으면 `CancelledError` 가 종료 과정에서 다시 튀어나온다.

### 6.3 주기 정렬

scrape 간격(15s)과 평가 주기(60s)를 맞춘다.
**scrape 보다 빠르게 평가하면 같은 데이터를 반복해서 보게 된다.**

> `APScheduler` 같은 라이브러리도 있지만, 이 규모에서는 `while True + sleep` 으로 충분하다.
> 의존성을 늘리지 않는다.

---

## 7. Flapping 방지 — 상태를 들고 있어야 한다

지표는 흔들린다. 한 번 성립했다고 바로 알리면 오탐 폭탄이 된다.

### 7.1 연속 N회

```python
from collections import deque

class RuleState:
    def __init__(self, required: int = 3) -> None:
        self._history: deque[bool] = deque(maxlen=required)
        self._required = required

    def push(self, matched: bool) -> bool:
        self._history.append(matched)
        return len(self._history) == self._required and all(self._history)
```

`deque(maxlen=N)` 은 **고정 길이 링버퍼**다. 넘치면 앞에서 자동으로 밀려난다.
Java 의 `EvictingQueue` 에 해당하고, 직접 리스트를 자르는 것보다 안전하다.

### 7.2 cooldown

```python
if self._last_fired_at is not None:
    if (now - self._last_fired_at).total_seconds() < cooldown_sec:
        return None          # 아직 재발화 금지
```

### 7.3 상위 3개만 보고

```python
fired.sort(key=lambda d: (_SEVERITY_ORDER[d.severity], d.confidence), reverse=True)
return fired[:3]
```

`sort(key=...)` 는 Java 의 `Comparator.comparing(...)` 이다.
튜플을 반환하면 다중 정렬 키가 된다 (앞 요소 우선).

목표는 원인을 단정하는 게 아니라 **사람이 확인할 후보를 3개 이하로 줄이는 것**이다.

---

## 8. 시간 다루기

```python
from datetime import datetime, timedelta, UTC

now = datetime.now(UTC)                    # O: timezone-aware
now = datetime.utcnow()                    # X: deprecated, naive datetime

now.isoformat()                            # "2026-08-27T10:12:00+00:00"
(a - b).total_seconds()                    # float 초
```

| 함정 | 내용 |
|---|---|
| naive vs aware | 둘을 빼면 `TypeError`. **항상 aware(UTC)로 통일** |
| `datetime.utcnow()` | Python 3.12 에서 deprecated. `datetime.now(UTC)` 사용 |
| 로컬 시간 저장 | 진단 결과에는 반드시 UTC. Grafana 가 알아서 변환한다 |
| 구간 측정 | `perf_counter()` 사용 (Phase 2 참고). datetime 은 timestamp 용 |

Java 의 `Instant` = aware datetime, `LocalDateTime` = naive datetime 이라고 생각하면 정확하다.

---

## 9. 진단 결과 직렬화

```python
@dataclass(frozen=True)
class Diagnosis:
    detected_at: datetime
    severity: str
    rule_id: str
    hypothesis: str
    confidence: float
    evidence: dict[str, str]                # 사람이 읽을 형태
    suggested_actions: tuple[str, ...]
```

**`evidence` 에 실제 수치를 넣는 것이 DoD 3번이다.**

```python
evidence = {
    "ttft_p95": f"{ttft:.1f}s (baseline {base:.1f}s, x{ratio:.1f})",
    "queue_depth": f"{depth:.0f} (baseline 0~1)",
    "gpu_utilization": f"{util:.0f}%",
}
```

사람이 검증할 수 없는 진단은 쓸모가 없다. 규칙이 틀렸을 때 evidence 가 있어야 규칙을 고칠 수 있다.

### confidence 를 어떻게 정하는가

정답은 없다. 시작점:

```text
confidence = 성립한 조건 수 / 전체 조건 수  ×  규칙별 사전 신뢰도(0.5~0.9)
```

**중요한 건 값이 아니라 "이 값이 어떻게 나왔는지 설명 가능한가" 다.**

---

## 10. Admin API

```python
@router.get("/admin/diagnosis")
async def get_diagnosis(engine: DiagnosisEngine = Depends(get_engine)) -> list[Diagnosis]:
    return engine.current()
```

FastAPI 는 dataclass 도 응답으로 직렬화한다. `datetime` 은 ISO 문자열이 된다.
인증은 Phase 4 에서 `/admin` 전체에 붙인다.

---

## 11. 실습 과제

1. `PrometheusClient.query()` 하나 만들고, Phase 2 에서 쓴 P95 쿼리를 실행해 값 출력
2. 결과가 없을 때 / NaN 일 때 `None` 이 나오는지 확인
3. `SignalSnapshot` 채우기 — 6~8개 쿼리를 `asyncio.gather` 로 동시에 던진다
4. `Condition` / `Rule` 자료구조 + 단위 테스트 (Prometheus 없이, 스냅샷을 손으로 만들어서)
5. R4(GPU 메모리) 하나만 먼저 완성 → 12GB 환경에서 가장 자주 걸린다
6. 평가 루프를 lifespan 에 붙이고 로그로 관찰
7. **의도적으로 부하를 걸어 R1 또는 R4 를 발화시킨다** (DoD 1번)
8. 24시간 돌려 오탐 확인 → 임계값 조정 → **조정 이력을 문서에 남긴다**

```python
# 3번 힌트 — 여러 쿼리 동시 실행
ttft, tps, gpu = await asyncio.gather(
    client.query(Q_TTFT_P95),
    client.query(Q_OUTPUT_TPS),
    client.query(Q_GPU_UTIL),
)
```

---

## 12. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| 빈 결과를 `0` 으로 처리 | 규칙 오발화 | `None` 반환 + 3-값 논리 |
| NaN 을 `==` 로 비교 | 항상 False | `math.isnan()` |
| `None` 을 `False` 로 취급 | "정상" 으로 오판 | `insufficient_signal` 로 분리 |
| 루프에서 예외 미처리 | 진단이 조용히 영구 중단 | `try/except Exception` + `log.exception` |
| `CancelledError` 를 `except Exception` 이 삼킴 | 종료 안 됨 | 먼저 잡아 re-raise |
| 임계값을 상상으로 결정 | 오탐 생성기 | Phase 2 baseline 사용 (설계서 7장) |
| 규칙을 `if` 문으로 구현 | Phase 9 에서 전면 재작성 | 데이터 구조로 |
| naive datetime 혼용 | `TypeError` | `datetime.now(UTC)` 통일 |
| 한 번 성립에 즉시 알림 | 알림 피로 | 연속 N회 + cooldown |
| evidence 없는 진단 | 검증 불가 → 규칙 개선 불가 | 실측치 포함 |

---

## 13. 이 Phase 의 진짜 어려움

Python 문법은 쉽다. 어려운 것은:

1. **임계값을 정하는 일.** 데이터 없이 정하면 반드시 틀린다.
2. **규칙이 틀렸음을 인정하는 일.** 설계서 7장: "규칙이 틀렸을 때 규칙을 고치는 것이 정상이다."
   수정 이력을 문서에 남긴다.
3. **LLM 으로 원인 분석하고 싶은 유혹을 참는 일.** 이 Phase 가 안정된 이후에 검토한다.
