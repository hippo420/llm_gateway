# Phase 5 학습 — Model Router

> 설계서: [`../docs/phases/phase-05-model-router.md`](../docs/phases/phase-05-model-router.md)
> 선행: Phase 4 (`registry.candidates()` 가 복수 deployment 를 반환할 것)

새 인프라가 없는 Phase 다. **알고리즘과 설계 패턴**만 필요하다.
분량은 적지만 **해시 버킷팅에 Python 특유의 함정이 하나 있다.**

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | Python 식 전략 패턴 (ABC / dict 레지스트리) | Strategy + `Map<String, Strategy>` | 하 |
| 2 | **결정론적 해시** — `hash()` 를 쓰면 안 되는 이유 | `String.hashCode()` (안정적) | **상** |
| 3 | `hashlib` 로 안정적 버킷 만들기 | `MessageDigest` | 중 |
| 4 | 가중 선택 알고리즘 (누적합 + 이분 탐색) | 동일 | 중 |
| 5 | `bisect` / `itertools.accumulate` | `Collections.binarySearch` | 하 |
| 6 | 결정 객체에 이유를 담기 (관측 가능성) | — | 하 |

---

## 1. 전략 패턴 — Python 식

### 1.1 ABC 로 인터페이스

```python
class RoutingStrategy(ABC):
    name: ClassVar[str] = ""

    @abstractmethod
    def select(
        self, ctx: RoutingContext, candidates: list[ModelDeployment]
    ) -> RoutingDecision: ...
```

### 1.2 레지스트리 = `Map<String, Class>`

```python
_STRATEGIES: dict[str, type[RoutingStrategy]] = {}


def register(cls: type[RoutingStrategy]) -> type[RoutingStrategy]:
    _STRATEGIES[cls.name] = cls
    return cls


@register                                    # 데코레이터로 자동 등록
class WeightedStrategy(RoutingStrategy):
    name = "weighted"
    ...


def create_strategy(name: str) -> RoutingStrategy:
    if name not in _STRATEGIES:
        raise ConfigError(f"unknown routing strategy: {name}")
    return _STRATEGIES[name]()
```

- `type[RoutingStrategy]` = Java 의 `Class<? extends RoutingStrategy>`
- **데코레이터**는 함수/클래스를 인자로 받아 가공해 돌려주는 함수다.
  `@register` 는 "이 클래스를 dict 에 넣고 그대로 반환" 한다.
  Spring 의 `@Component` + 컴포넌트 스캔을 손으로 만든 것.
- 단, **모듈이 import 되어야 등록된다.** `routing/strategies/__init__.py` 에서
  각 전략 모듈을 import 해두지 않으면 `unknown strategy` 가 난다. (흔한 실수)

Phase 1 의 `adapters/factory.py` 와 정확히 같은 패턴이다. 한 번 익히면 계속 쓴다.

### 1.3 설정에서 선택

```yaml
routing:
  strategy: weighted        # static | weighted | health_aware
  bucket_header: X-Session-Id
```

---

## 2. 단계적 구현 — 한 번에 지능적인 라우터를 만들지 않는다

| 단계 | 전략 | 필수 여부 |
|---|---|---|
| 5-a | `StaticStrategy` — 첫 enabled deployment | 필수 |
| 5-b | `WeightedStrategy` — 가중치 분배 | **필수 (여기까지가 Phase 5 범위)** |
| 5-c | `HealthAwareStrategy` — 최근 에러율/지연 나쁜 것 제외 | Phase 2 지표 축적 후 |
| 5-d | `MetricAwareStrategy` — queue_depth/GPU 기반 | Phase 2 지표 축적 후 |

5-a 를 먼저 만드는 이유: **Phase 4 까지의 동작과 동일함을 보장**하기 위해서다.
전략을 끼워도 기존 동작이 안 바뀌는 것을 확인한 뒤 weighted 로 넘어간다.

---

## 3. 결정론적 해시 — 이 Phase 의 유일한 진짜 함정

### 3.1 `hash()` 를 쓰면 안 된다

```python
hash("session-123")     # 실행할 때마다 다른 값이 나온다!
```

Python 은 보안(해시 충돌 DoS 방지)을 위해 **프로세스마다 문자열 해시에 랜덤 시드**를 쓴다
(`PYTHONHASHSEED`). Java 의 `String.hashCode()` 는 규격에 고정돼 있지만 Python 은 아니다.

이걸 버킷팅에 쓰면:

- Gateway 를 재시작할 때마다 같은 세션이 다른 deployment 로 간다
- 워커를 여러 개 띄우면 워커마다 결과가 다르다
- **A/B 테스트(Phase 7) 결과가 통째로 오염된다**

### 3.2 `hashlib` 을 쓴다

```python
import hashlib

def bucket_of(key: str, salt: str = "") -> int:
    """0~9999 사이의 안정적인 버킷 번호."""
    digest = hashlib.md5(f"{salt}:{key}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 10000
```

| 요소 | 이유 |
|---|---|
| `hashlib.md5` | 암호학적 강도는 필요 없다. **빠르고 안정적**이면 된다 (sha256 도 무방) |
| `.encode()` | `str` → `bytes`. 기본 UTF-8 |
| `int.from_bytes(..., "big")` | 바이트를 정수로. Java 의 `ByteBuffer.getInt()` |
| `% 10000` | 0.01% 해상도. weight 가 정수 %면 100 으로도 충분 |
| `salt` | **실험마다 다른 분배**를 만들기 위해. Phase 7 에서 `experiment_id` 를 넣는다 |

> salt 가 없으면 서로 다른 실험이 항상 같은 사용자 집합을 같은 쪽으로 보낸다.
> 실험 간 상관이 생겨 결과를 신뢰할 수 없다. **Phase 7 을 위해 지금 넣어둔다.**

### 3.3 버킷 키 선택

```python
bucket_key = (
    ctx.session_id                    # X-Session-Id
    or ctx.user_bucket                # X-User-Bucket
    or ctx.request_id                 # 최후 수단 = 사실상 랜덤
)
```

`request_id` 로 떨어지면 sticky 하지 않다. **그건 정상이다** — 세션 정보가 없으면 붙일 수가 없다.
다만 로그/메트릭에서 "sticky 하지 않은 요청 비율" 을 볼 수 있게 해두면 좋다.

---

## 4. 가중 선택 알고리즘

### 4.1 누적합 + 이분 탐색

```python
from itertools import accumulate
from bisect import bisect_right


def weighted_pick(
    candidates: list[ModelDeployment], bucket: int, total_buckets: int = 10000
) -> ModelDeployment:
    weights = [d.weight for d in candidates]
    total = sum(weights)
    if total <= 0:
        raise NoAvailableDeploymentError("all weights are zero")

    # 누적 경계를 버킷 스케일로 환산: [3000, 10000] 같은 형태
    cumulative = list(accumulate(w * total_buckets // total for w in weights))
    cumulative[-1] = total_buckets                 # 정수 나눗셈 오차 보정

    index = bisect_right(cumulative, bucket)
    return candidates[min(index, len(candidates) - 1)]
```

| 도구 | 설명 |
|---|---|
| `itertools.accumulate` | 누적합. `[30,70]` → `[30,100]` |
| `bisect.bisect_right` | 정렬된 리스트에서 삽입 위치를 이분 탐색 (`Collections.binarySearch` 계열) |
| `//` | **정수 나눗셈** (Java 의 int `/`). `/` 는 항상 float 를 반환한다 |
| 마지막 보정 | 정수 나눗셈 때문에 합이 total_buckets 에 못 미칠 수 있다 |

### 4.2 왜 `random.choices` 를 쓰지 않는가

```python
random.choices(candidates, weights=weights)   # X: 재현 불가능
```

같은 세션이 매번 다른 결과를 받는다. **sticky routing 이 깨지고 A/B 가 불가능해진다.**
설계서: "랜덤이 아니라 **해시 기반**이어야 재현 가능하다."

### 4.3 weight 정규화와 경고

```python
total = sum(d.weight for d in candidates)
if total != 100:
    log.warning("weights do not sum to 100", extra={"total": total, "model": model})
# 정규화는 위 알고리즘이 total 로 나누므로 자동으로 된다
```

죽이지 않고 경고만 한다. 운영 중 weight 를 조정하는 도중에는 합이 100이 아닌 순간이 있다.

---

## 5. RoutingDecision — 이유와 대안을 함께 담는다

```python
@dataclass(frozen=True)
class RoutingDecision:
    deployment: ModelDeployment
    strategy: str
    reason: str                                   # "weight 80/100, bucket=4213"
    alternatives: tuple[ModelDeployment, ...]     # ← Phase 6 이 그대로 쓴다
    bucket_key: str | None = None
```

**`alternatives` 를 놓치면 Phase 6 에서 라우팅을 다시 짜야 한다** (설계서 4장 명시).

Fallback 은 "실패했으니 다른 걸 고른다" 가 아니라
"이미 순위가 매겨진 후보 목록을 순서대로 시도한다" 이다. 그 목록을 여기서 만든다.

```python
alternatives = tuple(d for d in candidates if d.id != selected.id)   # 순위 유지
```

`reason` 은 로그/디버깅용이다. "왜 이 deployment 로 갔는가" 를 사후에 설명할 수 없으면
Phase 7 의 A/B 결과를 신뢰할 수 없다.

---

## 6. ChatService 와의 통합 — 호출부는 바뀌지 않는다

Phase 1 에서 `_select()` 를 별도 메서드로 분리해둔 이유가 여기서 드러난다.

```python
def _select(self, request, ctx) -> ModelDeployment:
    candidates = self._registry.candidates(request.model)      # Phase 4
    decision = self._router.select(RoutingContext.of(request, ctx), candidates)
    ctx.routing_decision = decision                            # Phase 6 이 alternatives 를 쓴다
    ctx.deployment_id = decision.deployment.id
    return decision.deployment
```

`complete()` / `stream()` 본문은 **한 줄도 바뀌지 않는다.** 그게 Phase 1 설계의 목적이었다.

---

## 7. 메트릭

```python
ROUTING = Counter(
    "llm_gateway_routing_decision_total", "",
    ["model", "deployment_id", "strategy"],
)
```

카디널리티: 모델 수 × deployment 수 × 전략 수 — 유한하므로 안전하다.
**`bucket_key` 를 label 에 넣으면 안 된다** (세션 ID = 무한).

Grafana 패널: `sum by (deployment_id) (rate(llm_gateway_routing_decision_total[5m]))`
→ 실제 분배 비율을 눈으로 확인한다 (DoD 1).

---

## 8. 에러 처리

```python
if not candidates:
    raise NoAvailableDeploymentError(...)     # GW-4004
```

모든 후보가 disabled 일 때 **명확한 에러 코드**로 응답한다.
`IndexError: list index out of range` 가 500 으로 새어나가면 원인 파악이 오래 걸린다.

`docs/specs/error-codes.md` 에 `GW-4004 no_available_deployment` 가 정의되어 있다.

---

## 9. 테스트 — 결정론이라 테스트하기 쉽다

```python
def test_sticky_routing_is_stable():
    """같은 세션 키는 항상 같은 deployment."""
    picks = {weighted_pick(candidates, bucket_of("session-abc")).id for _ in range(100)}
    assert len(picks) == 1


def test_weight_distribution_is_approximate():
    """80/20 설정에서 실제 분배가 근사한가."""
    counts = Counter(
        weighted_pick(candidates, bucket_of(f"session-{i}")).id
        for i in range(10_000)
    )
    assert 0.75 < counts["a@ollama"] / 10_000 < 0.85


def test_hash_is_process_stable():
    """해시가 하드코딩된 기대값과 일치하는가 = 재시작해도 같은가."""
    assert bucket_of("session-abc") == 4213      # 실제 값으로 바꿔서 고정
```

세 번째 테스트가 **`hash()` 실수를 막는 회귀 테스트**다. 반드시 넣는다.

`collections.Counter` 는 Java 의 `Map<T, Integer>` + 카운팅 관용구다.

---

## 10. 실습 과제

1. `StaticStrategy` 만 만들어 끼우고 **기존 동작이 그대로인지** 확인
2. `bucket_of()` + 고정값 회귀 테스트
3. `weighted_pick()` + 10,000회 분배 테스트
4. `RoutingDecision` 에 `alternatives` 포함 → Phase 6 을 위한 준비
5. `gateway.yaml` 에 `qwen-7b@vllm` 을 enabled 로 추가하고 80/20 설정
6. 실제 트래픽으로 Grafana 분배 패널 확인 (DoD 1)
7. Redis override 로 weight 변경 → 재시작 없이 분배가 바뀌는지 (DoD 2)
8. 같은 `X-Session-Id` 로 20번 요청 → 전부 같은 deployment 인지 (DoD 3)
9. deployment 하나 disable → 나머지로만 가는지 (DoD 4)

---

## 11. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| `hash()` 로 버킷팅 | 재시작/워커마다 분배가 달라짐 | `hashlib` |
| `random.choices` 사용 | sticky 깨짐, A/B 불가 | 해시 기반 결정론 |
| salt 없이 버킷팅 | 실험 간 상관 발생 (Phase 7) | `f"{experiment_id}:{key}"` |
| `alternatives` 미포함 | Phase 6 에서 라우팅 재작성 | `RoutingDecision` 에 포함 |
| 전략 모듈 import 누락 | `unknown strategy` | `strategies/__init__.py` 에서 import |
| `/` 를 정수 나눗셈으로 착각 | float 인덱스 → `TypeError` | `//` |
| weight 합이 0 | `ZeroDivisionError` | 명시적 `NoAvailableDeploymentError` |
| 후보 0개를 리스트 인덱싱 | `IndexError` → 500 | `GW-4004` |
| `bucket_key` 를 metric label 로 | 카디널리티 폭발 | 로그에만 |
| 5-c/5-d 를 먼저 구현 | 데이터 없이 만든 휴리스틱 | 5-b 까지만. 지표 축적 후 확장 |

---

## 12. 이 Phase 의 산출물이 만드는 것

DoD 5번: **"deployment 별 TTFT/TPS 를 나란히 비교할 수 있다."**

이게 Phase 7(Ollama vs vLLM 비교)의 기반이다.
라우터가 트래픽을 나눠 보내고 Phase 2 메트릭이 deployment_id 별로 쪼개져 있으면,
A/B 테스트의 절반은 이미 완성된 셈이다.
