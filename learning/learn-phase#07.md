# Phase 7 학습 — A/B Test

> 설계서: [`../docs/phases/phase-07-ab-test.md`](../docs/phases/phase-07-ab-test.md)
> 기록 템플릿: [`../docs/operations/benchmark-template.md`](../docs/operations/benchmark-template.md)
> 선행: Phase 5(해시 버킷팅), Phase 2(지표)

이 Phase 의 어려움은 **코드가 아니라 실험 설계와 통계**다.
Python 지식은 Phase 5 에서 배운 것의 재사용이 대부분이고, 새로 배울 것은 **부하 스크립트** 정도다.

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | 실험 할당 (Phase 5 버킷팅 재사용 + salt) | — | 하 |
| 2 | 라우터와 실험의 우선순위 조합 | — | 중 |
| 3 | guardrail 감시 → 자동 중단 | Circuit breaker 유사 | 중 |
| 4 | **통계적 함정** (표본, peeking, 편향) | — | **상** |
| 5 | `asyncio.Semaphore` 로 동시성 제어 | `Semaphore` | 중 |
| 6 | `argparse` 로 CLI 스크립트 | `picocli` / args | 하 |
| 7 | `statistics` 모듈로 백분위 집계 | — | 하 |
| 8 | **단일 GPU 에서 A/B 가 성립하지 않는다는 사실** | — | **상** |

---

## 1. 이 환경에서 가장 중요한 제약부터

> **RTX 4070 Ti 12GB 한 장에서 Ollama 와 vLLM 을 동시에 올리는 A/B 는 신뢰할 수 없다.**

두 서빙이 같은 GPU 메모리와 스케줄러를 두고 경쟁한다.
control 이 느린 이유가 "모델이 나빠서" 인지 "treatment 가 GPU 를 먹어서" 인지 구분할 수 없다.

**따라서 이 프로젝트의 순서는:**

```text
1) 시간 분할 벤치마크   A 구간 10분 → 쿨다운 → B 구간 10분   ← 여기부터 시작
2) 실시간 A/B          자원이 확보된 뒤 (GPU 추가 또는 외부 API 비교)
```

실시간 A/B 코드는 만들되, **첫 결론은 시간 분할로 낸다.**
이 판단을 문서에 남기지 않으면 나중에 "왜 실시간으로 안 했나" 를 다시 논의하게 된다.

---

## 2. 실험 정의

```yaml
experiments:
  serving-compare-001:
    enabled: true
    description: "Qwen 7B: Ollama vs vLLM"
    bucket_key: session_id          # session_id | user_id | request_id
    variants:
      - name: control
        deployment_id: qwen-7b@ollama
        weight: 50
      - name: treatment
        deployment_id: qwen-7b@vllm
        weight: 50
    guardrail:
      error_rate_max: 0.05
      ttft_p95_max_sec: 8
```

Pydantic 모델로 검증한다 (Phase 4 와 같은 방식, `extra="forbid"`).

```python
class Variant(BaseModel):
    name: str
    deployment_id: str
    weight: int = Field(ge=0, le=100)


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    enabled: bool = True
    bucket_key: Literal["session_id", "user_id", "request_id"] = "session_id"
    variants: list[Variant] = Field(min_length=2)
    guardrail: Guardrail | None = None
```

---

## 3. 할당 — Phase 5 코드를 그대로 쓴다

```python
def assign(experiment: Experiment, ctx: RequestContext) -> Variant:
    key = _bucket_key_value(experiment.bucket_key, ctx)
    bucket = bucket_of(key, salt=experiment.id)        # ← salt 가 experiment_id
    return weighted_pick_variant(experiment.variants, bucket)
```

**`salt=experiment.id` 가 핵심이다.** Phase 5 에서 미리 salt 인자를 만들어둔 이유다.

salt 가 없으면 실험 A 에서 control 이던 사용자가 실험 B 에서도 항상 control 이 된다.
두 실험이 상관되어 **각 실험의 결과를 독립적으로 해석할 수 없다.**

같은 이유로 실험을 재시작할 때 id 를 바꾸면 할당이 전부 재섞인다 — 의도적으로 쓸 수도 있고,
실수로 결과를 오염시킬 수도 있다. **id 는 실험의 정체성이다.**

---

## 4. Router 와의 통합 — 우선순위

```python
def _select(self, request, ctx) -> ModelDeployment:
    experiment = self._experiments.match(request.model, ctx)
    if experiment is not None:
        variant = assign(experiment, ctx)
        ctx.experiment = experiment.id
        ctx.variant = variant.name
        deployment = self._registry.by_id(variant.deployment_id)
        if deployment is not None and deployment.enabled:
            return deployment
        # 폴백: 실험 대상이 사라졌으면 일반 라우팅으로

    return self._router.select(...).deployment
```

**실험이 라우팅보다 우선한다** (설계서 6장 체크리스트).
단, 실험이 지정한 deployment 가 disabled 이면 일반 라우팅으로 떨어져야 한다 —
설정 오류로 서비스가 죽으면 안 된다.

`ctx.experiment` / `ctx.variant` 필드는 Phase 1 의 `RequestContext` 주석에 이미 예고되어 있다.

---

## 5. 메트릭에 label 추가

```python
REQUESTS.labels(..., experiment=ctx.experiment or "none", variant=ctx.variant or "none")
```

카디널리티: 실험 수 × variant 수 — 유한하므로 허용된다 (설계서 5장 명시).

**`or "none"` 이 중요하다.** label 값에 `None` 을 넣으면 `"None"` 문자열이 되거나 에러가 난다.
실험 대상이 아닌 요청도 같은 시계열 구조를 유지해야 비교가 가능하다.

> 기존 메트릭에 label 을 추가하면 **과거 시계열과 이어지지 않는다.**
> Prometheus 에서 label 집합이 다르면 다른 시계열이다.
> `sum without (experiment, variant) (...)` 로 합쳐서 보는 쿼리를 준비해둔다.

---

## 6. Guardrail — 실험 자동 중단

```python
@dataclass(frozen=True)
class Guardrail:
    error_rate_max: float | None = None
    ttft_p95_max_sec: float | None = None
    min_samples: int = 100                 # 표본이 적으면 판단하지 않는다


async def check(self, experiment: Experiment) -> GuardrailResult:
    for variant in experiment.variants:
        samples = await self._prom.query(_COUNT_Q.format(exp=experiment.id, var=variant.name))
        if samples is None or samples < experiment.guardrail.min_samples:
            return GuardrailResult(ok=True, reason="insufficient_samples")
        ...
```

Phase 3 의 `PrometheusClient` 를 그대로 재사용한다.

**`min_samples` 를 빼먹으면 실험 시작 직후 표본 3개로 중단된다.**
첫 요청이 cold start 로 8초 걸리면 즉시 guardrail 위반이다.

위반 시 동작: **control 100%** 로 되돌린다 (실험 비활성화).
이건 Phase 9 의 "정책 자동 적용" 의 전신이다 — 되돌리기가 자명한 유일한 자동 동작이라 안전하다.

---

## 7. 통계적 함정 (설계서 4장 — 코드보다 중요)

| 함정 | 무슨 일이 벌어지나 | 대응 |
|---|---|---|
| **표본 부족** | 20건 비교로 "vLLM 이 30% 빠르다" 결론 → 재현 안 됨 | 최소 요청 수를 **미리** 정한다 |
| **요청 종류 편향** | control 에 짧은 질의가 몰림 | `request_type` / input_token 구간별로 나눠 비교 |
| **Peeking** | 유리해 보일 때 실험 중단 → 우연을 결론으로 | 종료 조건을 **미리** 정한다 |
| **Cold start 오염** | 첫 몇 분의 모델 로딩이 평균을 지배 | warm-up 구간 제외 |
| **단일 GPU 간섭** | 두 서빙이 서로를 느리게 만듦 | 시간 분할 (1절) |

### 최소 표본 수를 어떻게 정하는가

정식 검정력 분석은 과하다. 실용적 기준:

```text
"P95 를 비교하려면 각 variant 당 최소 1,000건"
"평균 TTFT 를 비교하려면 각 300건 이상, 그리고 차이가 20% 이상일 때만 의미를 둔다"
```

**중요한 건 숫자가 아니라 "미리 정하고 지키는 것" 이다.**
실험 시작 전에 `operations/benchmark-template.md` 에 종료 조건을 적어둔다.

### 결과를 보는 방법

```text
❌ "vLLM 이 더 빠르다"
✅ "input_token 500~1500 구간, 각 1,200건 기준
    TTFT P95: Ollama 2.8s → vLLM 1.1s (-61%)
    Output TPS: 42 → 71 (+69%)
    Error Rate: 0.3% → 0.4% (차이 없음)
    단, 단일 GPU 시간 분할 측정이므로 동시 부하 하의 결과는 아님"
```

---

## 8. 벤치마크 스크립트 (`scripts/benchmark.py`)

시간 분할 비교를 위한 부하 도구다. Python 으로 처음 쓰는 CLI 스크립트일 것이다.

### 8.1 동시성 제어 — `asyncio.Semaphore`

```python
import asyncio

async def run_load(
    client: httpx.AsyncClient, prompts: list[str], concurrency: int
) -> list[Sample]:
    sem = asyncio.Semaphore(concurrency)

    async def one(prompt: str) -> Sample:
        async with sem:                       # 동시 실행 수를 concurrency 로 제한
            return await _single_request(client, prompt)

    return await asyncio.gather(*[one(p) for p in prompts])
```

`asyncio.gather(*coros)` 는 전부 동시에 시작한다.
**Semaphore 없이 1,000개를 gather 하면 1,000개 요청이 한꺼번에 나간다** — 부하 테스트가 아니라 자폭이다.

`async with sem:` 은 Java 의 `sem.acquire(); try { ... } finally { sem.release(); }` 이다.

### 8.2 TTFT 를 클라이언트에서 직접 재기

```python
async def _single_request(client, prompt) -> Sample:
    t0 = time.perf_counter()
    ttft = None
    tokens = 0

    async with client.stream("POST", "/v1/chat/completions", json={...,"stream": True}) as r:
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            delta = chunk["choices"][0]["delta"].get("content", "")
            if delta:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                tokens += 1

    total = time.perf_counter() - t0
    return Sample(ttft=ttft, total=total, tokens=tokens)
```

> 클라이언트 측 TTFT 는 Gateway 측 TTFT 보다 **네트워크 왕복만큼 크다.**
> 두 값을 섞어 비교하지 않는다. 벤치마크는 클라이언트 값끼리, Grafana 는 Gateway 값끼리.
>
> 여기서 `tokens` 는 **chunk 수**지 토큰 수가 아니다. 정확한 TPS 는 응답의 `usage` 를 쓴다.

### 8.3 집계

```python
import statistics

def summarize(samples: list[Sample]) -> dict[str, float]:
    ttfts = sorted(s.ttft for s in samples if s.ttft is not None)
    return {
        "n": len(samples),
        "ttft_p50": statistics.median(ttfts),
        "ttft_p95": ttfts[int(len(ttfts) * 0.95)],      # 간단한 백분위
        "tps_mean": statistics.mean(s.tokens / s.gen_sec for s in samples),
    }
```

**Prometheus 의 `histogram_quantile` 과 값이 다를 수 있다.**
전자는 실제 값 기반, 후자는 버킷 근사다. 어느 쪽을 쓰는지 결과에 명시한다.

### 8.4 CLI 인자

```python
import argparse

parser = argparse.ArgumentParser(description="LLM Gateway benchmark")
parser.add_argument("--model", required=True)
parser.add_argument("--deployment", help="X-Gateway-Deployment 강제 지정")
parser.add_argument("--concurrency", type=int, default=4)
parser.add_argument("--requests", type=int, default=200)
parser.add_argument("--warmup", type=int, default=10, help="집계에서 제외할 초기 요청 수")
args = parser.parse_args()

asyncio.run(main(args))          # 스크립트에서 이벤트 루프를 시작하는 방법
```

`asyncio.run()` 은 **최상위에서 한 번만** 호출한다. FastAPI 안에서는 절대 쓰지 않는다
(이미 루프가 돌고 있어서 `RuntimeError` 가 난다).

`--warmup` 이 cold start 오염을 막는 장치다. 앞의 N건을 버린다.

---

## 9. 시간 분할 벤치마크 절차

```text
1. Ollama 만 GPU 에 상주  →  warm-up 10건  →  측정 200건  →  결과 저장
2. Ollama 언로드 / vLLM 기동  →  GPU 메모리 회수 확인 (nvidia-smi)
3. vLLM warm-up 10건       →  측정 200건  →  결과 저장
4. 같은 프롬프트 집합 사용   ← 이게 핵심
5. 1~3 을 순서를 바꿔 한 번 더 (순서 효과 제거)
```

4번과 5번이 시간 분할의 신뢰도를 결정한다.
프롬프트가 다르면 비교가 아니고, 순서를 안 바꾸면 "나중에 측정한 쪽이 유리한" 편향이 남는다.

결과는 `operations/benchmark-template.md` 형식으로 커밋한다.

---

## 10. Grafana 비교 대시보드

```promql
# variant 별 TTFT P95 를 나란히
histogram_quantile(
  0.95,
  sum by (le, variant) (
    rate(llm_gateway_ttft_seconds_bucket{experiment="serving-compare-001"}[5m])
  )
)
```

`sum by (le, variant)` — `le` 와 함께 `variant` 를 남기는 것이 포인트다.
`le` 를 빠뜨리면 `histogram_quantile` 이 동작하지 않고,
`variant` 를 빠뜨리면 두 조건이 합쳐져 비교가 안 된다.

---

## 11. 실습 과제

1. `Experiment` / `Variant` Pydantic 모델 + 설정 로딩
2. `assign()` — **salt 유무에 따라 할당이 달라지는지** 테스트로 확인
3. 같은 `bucket_key` 가 항상 같은 variant 인지 (DoD 1)
4. metric label 추가 → 기존 대시보드가 깨지지 않는지 확인 (`sum without` 쿼리 준비)
5. `scripts/benchmark.py` 작성 → Semaphore 동시성 4로 200건
6. **시간 분할로 Ollama 단독 측정** → 결과를 템플릿에 기록
7. vLLM 을 띄울 수 있으면 같은 절차로 측정, 아니면 `temperature 0.2 vs 0.7` 로 연습
8. guardrail 감시 루프 (Phase 3 루프 패턴 재사용) → 강제 위반시켜 자동 중단 확인 (DoD 3)
9. control/treatment 비교 대시보드 (DoD 2)
10. **결과로 서빙 선택 결정을 내리고 근거를 문서에 남긴다** (DoD 5)

---

## 12. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| salt 없이 버킷팅 | 실험 간 상관 | `salt=experiment.id` |
| 단일 GPU 실시간 A/B | 결과 전체가 무의미 | 시간 분할 |
| `min_samples` 없는 guardrail | 시작 직후 자동 중단 | 표본 하한 설정 |
| Semaphore 없는 `gather` | 부하 도구가 자기를 죽임 | `asyncio.Semaphore` |
| warm-up 미제외 | cold start 가 결과 지배 | `--warmup N` |
| 서로 다른 프롬프트로 비교 | 비교가 아님 | 동일 프롬프트 집합 |
| 순서 효과 무시 | 나중 측정이 유리 | 순서 바꿔 재측정 |
| 유리할 때 중단 (peeking) | 우연을 결론으로 | 종료 조건 사전 확정 |
| label 값에 `None` | 시계열 오류 | `or "none"` |
| 클라이언트 TTFT 와 서버 TTFT 혼용 | 값이 안 맞음 | 출처를 명시 |
| 성능만 보고 결론 | 절반의 판단 | Phase 8 품질 평가 필요 |

---

## 13. 이 Phase 를 끝내는 조건

DoD 4번: **"Ollama vs vLLM 비교 결과가 수치와 함께 문서에 기록"**
DoD 5번: **"그 결과로 서빙 선택 결정을 내리고, 근거를 남겼다"**

코드가 도는 것으로는 이 Phase 가 끝나지 않는다.
**결정을 내리고 그 근거를 남기는 것**이 산출물이다.

그리고 설계서 3장의 경고: **성능만 보고 결론 내지 않는다. Phase 8 없이는 절반의 판단이다.**
