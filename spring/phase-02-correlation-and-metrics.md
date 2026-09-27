# Spring 2단계 — 요청 추적 (Correlation) & Spring 측 지표

> 대응 Gateway 문서: [`../docs/phases/phase-02-instrumentation.md`](../docs/phases/phase-02-instrumentation.md) "6. Correlation",
> [`../docs/operations/observability-stack.md`](../docs/operations/observability-stack.md) "6. Correlation 확인 절차"
> 선행: [1단계](./phase-01-gateway-connection.md) 완료 (모든 LLM 호출이 `LlmGatewayClient` 를 지난다)

---

## 목표

```text
mony_batch 로그                          Gateway 로그                        Grafana
{JOB_NAME, REQUEST_ID=abc, elapsed}  ⇄  {request_id=abc, ttft, total}  ⇄  Gateway 지표 + Spring 지표
```

"느린 배치 스텝의 LLM 호출 하나"를 골라 **양쪽 로그를 같은 `request_id` 로 이어 붙이고**,
시간이 어디서 쓰였는지(컨텍스트 수집 / 네트워크 / 큐·적재 / 생성) 가를 수 있게 한다.

**Gateway 쪽은 이미 끝났다.** `X-Request-Id` 를 받으면 그대로 쓰고, 없으면 만들어 응답 헤더로 돌려주며,
JSON 로그의 `request_id` 필드에 남긴다. 남은 것은 Spring 쪽뿐이다.

---

## 2-1. 요청 ID 생성 규칙

배치에는 들어오는 HTTP 요청이 없으므로 **LLM 호출 1회당 1개**를 Spring 이 만든다.

```text
request_id = UUID 32자리 hex        예) 7d173fd5c2a94e0f8b1e3a6d9c0b4f21
```

- Gateway 는 `[A-Za-z0-9._:@-]{1,128}` 이 아닌 값을 **버리고 새로 만든다.** 이 규칙 안에서 만든다.
- 배치 Job 단위가 아니라 **호출 단위**인 이유: Job 하나가 LLM 을 수백~수천 번 부른다.
  Job 단위 추적은 이미 있는 MDC `JOB_NAME` / `STEP_NAME` 으로 한다.
- 호출 결과로 돌아온 `X-Request-Id` 응답 헤더가 보낸 값과 다르면 WARN 을 남긴다 (규칙 위반 값이었다는 뜻).

---

## 2-2. `LlmGatewayClient` 에 헤더 + MDC 추가

```java
public LlmResult complete(LlmRequest req) {
    String requestId = UUID.randomUUID().toString().replace("-", "");
    MDC.put("REQUEST_ID", requestId);
    long started = System.nanoTime();
    try {
        ResponseEntity<Map<String, Object>> res = gatewayWebClient.post()
                .uri("/v1/chat/completions")
                .header("X-Request-Id", requestId)
                .header("X-Request-Type", requestType())   // 2-3
                ...
                .block();
        LlmResult result = LlmResult.from(res);
        logSummary(requestId, result, started, "success", null);
        return result;
    } catch (LlmGatewayException e) {
        logSummary(requestId, null, started, "error", e.code());
        throw e;
    } finally {
        MDC.remove("REQUEST_ID");
    }
}
```

주의:

- `.block()` 으로 **호출 스레드에서** 기다리므로 MDC 가 유지된다.
  Reactor 연산자(`map`, `doOnNext`) **안에서** MDC 를 읽지 말 것 — 다른 스레드일 수 있다.
- 비동기 배치 실행은 `AsyncBatchLaunchConfig` 의 TaskDecorator 가 `JOB_NAME` 을 복사해 준다.
  `REQUEST_ID` 는 호출 단위로 넣고 빼므로 복사 대상이 아니다.

---

## 2-3. `X-Request-Type` 헤더

값: 현재 MDC 의 `JOB_NAME` (없으면 `system`). Gateway 는 지금 이 값을 **저장만** 하고,
Phase 5 에서 라우팅 힌트(`news_summary` / `report_analysis` ...)로 쓴다.
지금 보내두면 Phase 5 때 Spring 을 다시 배포하지 않아도 된다.

> Job 이름이 Gateway 허용 문자(`[A-Za-z0-9._:@-]`) 밖이면 Gateway 가 버린다. 현재 Job 이름은 영문 camelCase 라 문제없다.

---

## 2-4. LLM 호출 요약 로그 1줄

Gateway 의 `chat_completed` 와 짝이 되는 Spring 쪽 로그. **프롬프트/응답 원문은 넣지 않는다.**

```java
log.info("llm_call", kv("event", "llm_call"),
        kv("request_id", requestId),
        kv("model", props.model()),
        kv("deployment", result == null ? null : result.deployment()),   // X-Gateway-Deployment
        kv("status", status),                                            // success | error
        kv("gw_code", gwCode),                                           // GW-5004 등 (에러 시)
        kv("elapsed_ms", elapsedMs),
        kv("input_tokens", result == null ? null : result.inputTokens()),
        kv("output_tokens", result == null ? null : result.outputTokens()),
        kv("finish_reason", result == null ? null : result.finishReason()));
```

실제 출력 (E2E):

```text
llm_call request_id=4307ac716056437aacc2a313a9be8594 job=llmGatewayE2E model=exaone-7.8b
         deployment=exaone-7.8b@ollama status=success elapsed_ms=2590
         input_tokens=1162 output_tokens=66 finish_reason=stop
```

같은 id 의 Gateway 로그: `total_sec=2.25, ttft_sec=0.55, load_sec=0.14, output_tps=38.8`.
Spring 2,590ms − Gateway 2,252ms ≈ 340ms 는 네트워크 + Gateway 앞단 + JVM 첫 호출 워밍업이다.

(`kv` = `net.logstash.logback.argument.StructuredArguments.kv`. 이미 의존성에 있다.)

### `logback-spring.xml`

JSON appender 에 MDC 키 추가, 콘솔 패턴에도 노출:

```xml
<includeMdcKeyName>REQUEST_ID</includeMdcKeyName>
```

```text
[%d{...}] [%-5level] [%X{JOB_NAME}/%X{STEP_NAME}] [%X{REQUEST_ID:-}] [%logger{5}] - %msg%n
```

그러면 `llm_call` 뿐 아니라 **호출 도중 남는 모든 로그**(JSON 파싱 실패 등)에 같은 `REQUEST_ID` 가 붙는다.

---

## 2-5. Spring 측 지표 (Micrometer → Prometheus)

Gateway 지표만으로는 "LLM 호출 **앞**에서 쓴 시간"(벡터 검색, DB 조회로 컨텍스트 만들기)을 모른다.
Spring 이 그 구간과 자기 관점의 LLM 호출 시간을 노출한다.

### 의존성 / 설정

```groovy
implementation 'org.springframework.boot:spring-boot-starter-actuator'
implementation 'io.micrometer:micrometer-registry-prometheus'
```

```properties
management.endpoints.web.exposure.include=health,prometheus
management.metrics.tags.application=mony-batch
```

> `/actuator/**` 는 `ApiKeyAuthFilter`(`/api/batch/**` 전용) 범위 밖이라 **인증 없이 열린다.**
> 노출은 `health,prometheus` 로만 제한하고, 외부 접근은 방화벽/리버스 프록시에서 막는다.

### 추가할 지표 (2개만)

| 이름 | 타입 | 태그 | 측정 구간 |
|---|---|---|---|
| `mony_llm_call_seconds` | Timer (histogram) | `job`, `model`, `outcome` | `LlmGatewayClient.complete()` 전체 (Spring → Gateway → 응답) |
| `mony_llm_context_seconds` | Timer (histogram) | `job`, `source` | LLM 호출 전 컨텍스트 수집. 현재 `source` = `stock_context` (`StockContextCollector.collect`, AI 요약) / `news_vector` (`StockAlertTasklet` 벡터 검색) |

- `outcome` = `success` / `error` / `timeout` (3개). **`gw_code` 는 태그로 넣지 않는다** — 원인 분해는 Gateway 의 `llm_gateway_errors_total` 이 한다.
- `job` = MDC `JOB_NAME` (현재 수십 개, 유한). `source` = `vector` / `db` / `es` 처럼 **고정 열거형**만.
- **금지 태그**: request_id, 종목코드, 기사 ID, 프롬프트. (Gateway [metrics-spec](../docs/specs/metrics-spec.md) 과 같은 cardinality 규칙)
- histogram bucket 은 Gateway 와 같은 초 단위로 맞춘다: `0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300`.

```java
Timer.builder("mony_llm_call_seconds")
     .tag("job", jobName).tag("model", props.model()).tag("outcome", outcome)
     .serviceLevelObjectives(/* 위 bucket */)
     .register(registry)
     .record(Duration.ofNanos(System.nanoTime() - started));
```

### Prometheus 수집 대상 추가 (Gateway 저장소 `observability/prometheus.yml`)

```yaml
  - job_name: mony-batch
    metrics_path: /actuator/prometheus
    static_configs:
      - targets: ["host.docker.internal:21820"]    # local 기준 server.port
        labels: { service: mony-batch }
```

---

## 2-6. 시간 분해 — 이 단계의 실질적 가치

같은 `request_id` 의 두 로그를 놓고 이렇게 읽는다.

```text
Spring  mony_llm_context_seconds          컨텍스트 수집 (LLM 과 무관)
Spring  llm_call.elapsed_ms       ─┐
Gateway chat_completed.total_sec   ├─ 차이 = 네트워크 + Gateway 오버헤드 (보통 수 ms)
Gateway   ├ load_sec                │   모델 적재 (cold start, num_ctx 변경)
Gateway   ├ ttft_sec               │   큐 + prefill + 적재
Gateway   └ generation_sec        ─┘   decode
```

판단 규칙:

| 관측 | 원인 쪽 |
|---|---|
| `context` 가 전체의 대부분 | **LLM 문제가 아니다.** 벡터 검색 / DB |
| `elapsed_ms` ≫ `total_sec` | 네트워크, Gateway 앞단 (드묾) |
| `load_sec` 가 호출마다 큼 | `num_ctx` 가 호출마다 바뀌고 있다 → [1단계 1-6](./phase-01-gateway-connection.md#1-6-조건부-num_ctx-구간화) 적용 |
| TTFT 큼, TPS 정상 | 큐 / 긴 프롬프트 / cold start |
| TTFT 정상, generation 큼 | 출력이 길다 (`output_tokens` 확인) 또는 decode 가 느리다 |

---

## 2-7. 로그를 한곳에서 보기

mony_batch JSON 로그는 이미 Filebeat → Elasticsearch 로 간다 (`logs/json/*-json.log`).
Gateway 로그는 **지금은 stdout 뿐**이다. 둘을 Kibana 에서 `request_id` 로 한 번에 보려면
Gateway 의 stdout 을 파일로 남기고 같은 Filebeat 가 읽게 한다 (Gateway 쪽 작업, 필드명은 이미 `request_id` 로 맞춰져 있다).

필드명 차이: Spring MDC 는 `REQUEST_ID`, Gateway 는 `request_id`.
Kibana 에서 둘을 같이 검색하거나, Filebeat processor 로 `REQUEST_ID → request_id` rename 한다.

그 전까지는:

```bash
grep '"REQUEST_ID":"7d173fd5..."' logs/mony_batch/json/*.log
grep '"request_id": "7d173fd5..."' <gateway 로그>
```

---

## 완료 기준 (DoD)

1. 임의의 LLM 호출 하나를 골라 Spring 로그와 Gateway 로그에서 **같은 `request_id`** 로 찾을 수 있다.
2. 호출 도중 난 Spring 쪽 에러 로그(JSON 파싱 실패 등)에도 `REQUEST_ID` 가 붙어 있다.
3. Prometheus Targets 에 `mony-batch` 가 UP 이고, `mony_llm_call_seconds` / `mony_llm_context_seconds` 가 나온다.
4. 배치 하나를 돌린 뒤 2-6 의 표로 "시간이 어디서 쓰였는가"를 한 줄로 말할 수 있다.
5. `count({__name__=~"mony_llm_.*"})` 가 수백 이하다 (태그 cardinality 통제).

---

## 완료 체크리스트

- [x] `LlmGatewayClient`: `X-Request-Id` / `X-Request-Type` 헤더, MDC `REQUEST_ID` put/remove
- [x] 응답 `X-Request-Id` 불일치 WARN
- [x] `llm_call` 요약 로그 (원문 없음). E2E 에서 Spring `request_id` 와 Gateway `chat_completed.request_id` 일치 확인
- [x] `logback-spring.xml`: `REQUEST_ID` MDC 키 (JSON + 콘솔. 없을 때 빈 `[]` 가 찍히지 않게 `%replace`)
- [x] actuator + micrometer-registry-prometheus, 노출 범위 제한
- [x] `mony_llm_call_seconds`, `mony_llm_context_seconds`
- [x] Gateway `prometheus.yml` 에 `mony-batch` job 추가
- [ ] 앱 기동 후 Prometheus Targets 에서 `mony-batch` UP 확인 (앱 기동 시 스케줄러가 실제 Job 을 돌리므로 미실시)
- [ ] (선택) Gateway 로그 Filebeat 수집, `REQUEST_ID`/`request_id` 필드 통일
- [ ] Gateway 설계서 `phase-02-instrumentation.md` 체크리스트의 "Spring → X-Request-Id 전파" 체크
