# Spring 1단계 — Gateway 경유로 전환

> 대응 Gateway 문서: [`../docs/phases/phase-01-llm-gateway.md`](../docs/phases/phase-01-llm-gateway.md),
> API 명세: [`../docs/specs/api-spec.md`](../docs/specs/api-spec.md)
> 대상 코드: `mony_batch/src/main/java/app/monybatch/mony/infra/llm/OllamaModelClient.java`

---

## 목표

```text
Before:  Batch Job ──► OllamaModelClient ──/api/generate──► Ollama
After:   Batch Job ──► OllamaModelClient ──/v1/chat/completions──► LLM Gateway ──► Ollama
                                   (llm.gateway.enabled=false 면 Before 로 복귀)
```

**완료 기준**: 플래그를 켜고 모든 LLM 사용 배치를 돌렸을 때 기존과 같은 결과가 나오고,
모든 호출이 Gateway 로그(`chat_completed`)와 Grafana 에 잡힌다.

---

## 0. Gateway 선행 작업 (Spring 작업 전에 끝나야 함)

Spring 만 바꿔서는 동작하지 않는다. 아래는 **Gateway 저장소에서 할 일**이다.

| # | 작업 | 이유 | 상태 |
|---|---|---|---|
| G1 | `response_format` 지원 → Ollama `format` 으로 변환 | `generateJSON()` 이 structured output 에 의존한다. 예전엔 `GW-4002` 로 거절됐다 | **완료** |
| G2 | 요청별 `num_ctx` 전달 (비표준 확장 필드) | 호출마다 다른 `num_ctx` 를 쓴다. 없으면 **기본 2048 로 프롬프트가 조용히 잘린다** | **완료** |
| G3 | `gateway.yaml` 에 mony_batch 가 쓰는 모델 등록 | local 은 `exaone3.5:7.8b`, dev/prod 는 `qwen2.5:14b` | **완료** (`exaone-7.8b`, `qwen-14b@ollama`) |
| G4 | 모델별 `max_tokens` 검토 | Gateway 기본 2048. Ollama 직접 호출 때는 상한이 없었다 | 두 모델 4096 으로 설정. 배치 검증에서 `length` 비율 확인 필요 |
| G5 | 포트 결정 | 8080 은 이 머신에서 Jenkins 가 쓰고 있다 | 임시로 **8089** 사용 중 (설정 기본값) |

### G1 매핑 (권장)

```text
OpenAI 요청                                           Ollama 요청
response_format: {type: "json_object"}          →    format: "json"
response_format: {type: "json_schema",
                  json_schema: {name, schema}}  →    format: <schema>
```

### G2 형식 (권장)

OpenAI 스펙에 없는 필드라 **최상위 확장 필드**로 받는다. api-spec 에 "Ollama 전용, 다른 adapter 는 무시"로 명시한다.

```json
{ "model": "qwen-14b", "messages": [...], "num_ctx": 8192 }
```

> ⚠️ **Ollama 는 `num_ctx` 가 바뀌면 모델을 다시 적재한다.**
> 지금처럼 호출마다 자동 산출한 값(예: 5,731 / 6,102 ...)을 보내면 호출할 때마다 cold start 가 날 수 있다.
> Gateway Phase 2 지표에서 `load_sec` / TTFT 가 튀는지 확인하고, 튄다면 Spring 쪽에서
> **값을 몇 개 구간(4096 / 8192 / 16384)으로 올림**해서 보내도록 바꾼다 (1-6 참고).

### G3 예시

```yaml
models:
  exaone-7.8b:                 # local 프로파일용
    deployments:
      - id: exaone-7.8b@ollama
        adapter: ollama
        endpoint: http://[::1]:11434
        upstream_model: exaone3.5:7.8b
        extra: { keep_alive: 30m }
  qwen-14b:                    # dev/prod 프로파일용 (기존 vllm deployment 옆에 ollama 추가)
    deployments:
      - id: qwen-14b@ollama
        adapter: ollama
        endpoint: http://localhost:11434
        upstream_model: qwen2.5:14b
        enabled: true
        timeout: { read: 120, total: 300 }
```

---

## 1-1. 설정 추가

`application-{profile}.properties` 에 추가한다. 기존 `apikey.ollama.*` 는 **지우지 않는다** (롤백 경로).

```properties
# --- LLM Gateway (1단계) ---
llm.gateway.enabled=${LLM_GATEWAY_ENABLED:false}     # 검증 끝나면 true
# local. Gateway 는 --host 0.0.0.0(IPv4) 로 뜨므로 [::1] 로 적으면 닿지 않는다.
# dev/prod(컨테이너)는 http://host.docker.internal:8089
llm.gateway.base-url=${LLM_GATEWAY_URL:http://127.0.0.1:8089}
llm.gateway.model=exaone-7.8b                   # 논리 모델명. dev/prod 는 qwen-14b
llm.gateway.api-key=                            # Gateway 의 GATEWAY_API_KEY 를 켰을 때만
# Gateway total timeout(모델별 180~300s) 보다 커야 한다.
# 작으면 Gateway 가 정리된 에러(GW-5005)를 주기 전에 Spring 이 먼저 끊어서 원인이 사라진다.
llm.gateway.timeout-seconds=320
```

`@ConfigurationProperties(prefix = "llm.gateway")` 로 `LlmGatewayProperties` 레코드를 만든다.

---

## 1-2. `LlmGatewayClient` 신규 작성 (`infra/llm/gateway/`)

OpenAI 호환 `/v1/chat/completions` 를 부르는 얇은 클라이언트. **non-streaming 만** 쓴다
(배치는 스트리밍이 필요 없다. Gateway 는 내부적으로 streaming 으로 돌려 TTFT 를 잰다).

```java
@Component
@RequiredArgsConstructor
@Slf4j
public class LlmGatewayClient {

    private final WebClient gatewayWebClient;      // base-url, timeout, Authorization 헤더를 설정한 전용 빈
    private final LlmGatewayProperties props;

    public LlmResult complete(LlmRequest req) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("model", props.model());
        body.put("messages", List.of(Map.of("role", "user", "content", req.prompt())));
        if (req.temperature() != null) body.put("temperature", req.temperature());
        if (req.numCtx() > 0)          body.put("num_ctx", req.numCtx());              // G2
        if (req.jsonSchema() != null)  body.put("response_format", Map.of(             // G1
                "type", "json_schema",
                "json_schema", Map.of("name", "result", "schema", req.jsonSchema())));

        ResponseEntity<Map<String, Object>> res = gatewayWebClient.post()
                .uri("/v1/chat/completions")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue(body)
                .retrieve()
                .onStatus(HttpStatusCode::isError, this::toGatewayException)
                .toEntity(new ParameterizedTypeReference<Map<String, Object>>() {})
                .block();

        return LlmResult.from(res);   // content, finish_reason, usage, X-Gateway-Deployment 헤더
    }
}
```

### 응답 매핑

| 기존 (Ollama) | Gateway (OpenAI 호환) |
|---|---|
| `/api/generate` → `response` | `choices[0].message.content` |
| `/api/chat` → `message.content` | `choices[0].message.content` |
| `done_reason` | `choices[0].finish_reason` (`stop` / `length`) |
| `prompt_eval_count` / `eval_count` | `usage.prompt_tokens` / `usage.completion_tokens` |
| — | 응답 헤더 `X-Gateway-Deployment` (실제 처리한 deployment) |

### 에러 매핑

Gateway 에러 body 는 항상 이 형식이다 ([`error-codes.md`](../docs/specs/error-codes.md)):

```json
{ "error": { "message": "...", "type": "upstream_read_timeout", "code": "GW-5004", "request_id": "..." } }
```

`LlmGatewayException(code, type, httpStatus, requestId)` 으로 바꿔 던진다.
**Spring 에서 재시도하지 않는다** (README 원칙 4). 기존 호출부의 예외 처리 흐름을 그대로 탄다.

### `finish_reason == "length"` 는 경고 로그

출력이 `max_tokens` 에서 잘렸다는 뜻이다. `generateJSON()` 경로에서는 **깨진 JSON** 이 되므로
파싱 실패보다 먼저 이 사실을 로그로 남긴다 (G4 의 조기 경보).

---

## 1-3. `OllamaModelClient` 전송 계층 교체

public 메서드(`generate`, `generateJSON`, `chat`, `classifyArticle` ...)는 **그대로 둔다.**
내부의 `postRequest(...)` 호출 지점만 분기한다.

```java
public String generateJSON(String prompt, Map<String, Object> jsonSchema, int numCtx) {
    return executeWithTimer("Generate API", () -> {
        if (gatewayProps.enabled()) {
            return gatewayClient.complete(LlmRequest.builder()
                    .prompt(PROMT_COND + prompt)
                    .temperature(0.2)
                    .numCtx(numCtx)
                    .jsonSchema(jsonSchema)
                    .build()).content();
        }
        // ↓ 기존 Ollama 직접 호출 (롤백 경로) - 수정하지 않는다
        ...
    });
}
```

| 메서드 | Gateway 요청 |
|---|---|
| `generate(prompt)` | `messages=[user]`, `temperature=0.2` |
| `generateJSON(prompt, schema, numCtx)` | + `response_format`, `num_ctx` |
| `chat(prompt)` | `messages=[user]`, temperature 미지정 → **Gateway 기본 0.2 적용** (1-5 참고) |

---

## 1-4. 임베딩 / Gemini 는 건드리지 않는다

- `VectorConfig` 의 `OllamaEmbeddingModel` 은 그대로 Ollama 직결. Gateway 에 embeddings API 가 없다.
- `spring.ai.ollama.*` 설정도 그대로 둔다 (임베딩이 쓴다).
- **Spring AI OpenAI starter 를 추가하지 않는다.** `OpenAiEmbeddingModel` 빈이 자동 구성되어
  `EmbeddingModel` 빈이 두 개가 된다 (`VectorConfig` 주석이 경고하는 바로 그 상황).
  채팅에 Spring AI `ChatClient` 를 쓰기 시작할 때 다시 검토한다.

---

## 1-5. 동작이 달라지는 지점 (검증 때 반드시 볼 것)

| 항목 | 기존 | Gateway 경유 | 영향 |
|---|---|---|---|
| `chat()` 의 temperature | 모델 Modelfile / Ollama 기본값 (보통 0.8) | Gateway 기본 0.2 | 출력이 더 결정적으로 바뀐다. 원래 값을 원하면 명시할 것 |
| 출력 길이 상한 | 없음 (`num_predict` 미지정) | `max_tokens` 2048 (G4) | 긴 요약/JSON 이 잘릴 수 있다 → `finish_reason=length` |
| `top_p` | Ollama 기본값 | Gateway 기본 0.9 | 미미 |
| 모델 유지 | Ollama 기본 5분 | `keep_alive: 30m` | cold start 감소 |
| 타임아웃 | Spring 180s 단일 | Gateway connect/read/total + Spring 320s | 원인별 에러 코드가 남는다 |

---

## 1-6. (조건부) `num_ctx` 구간화

G2 주의사항이 실제로 관측되면(Gateway 로그에서 호출마다 `load_sec` 가 수 초 이상) 적용한다.

```java
static int bucketNumCtx(int n) {
    if (n <= 0) return 0;
    for (int b : new int[]{4096, 8192, 16384, 32768}) if (n <= b) return b;
    return 32768;
}
```

`NewsRagProperties` / `ReportRagProperties` 의 자동 산출 결과를 이 함수로 올림한다.

---

## 1-7. 검증 절차

1. Gateway 선행 작업(G1~G3) 완료, Gateway 기동, `curl <gateway>/readyz` 가 ready.
2. local 프로파일에서 `llm.gateway.enabled=true`.
3. LLM 을 쓰는 배치를 도메인별로 하나씩 수동 실행한다.

   | 도메인 | 배치 / 서비스 | 경로 |
   |---|---|---|
   | news | 분류/재분류, `NewsContentAnalyzer` | `generateJSON` + `num_ctx` |
   | report | `ReportContentAnalyzer` | `generateJSON` + `num_ctx` |
   | ai | `AiSummaryGenerator` (ollama 경로), `MarketBriefingService` | `generateJSON`(8192) / `generate` |
   | dart | `PerformanceDisclosureHandler` | 실적 추출 |
   | event | `EconomicEventItemWriter` | `generate` |
   | stock | `StockAlertTasklet` | `generate` |

4. 각 실행마다 확인:
   - 배치 결과(DB 적재값)가 플래그 off 때와 같은 형태인가 (JSON 파싱 실패 0건)
   - Gateway 로그에 호출 수만큼 `chat_completed` 가 찍히는가
   - Grafana `llm-model` 대시보드의 **length 비율**이 0 인가
5. `llm.gateway.enabled=false` 로 되돌려 기존 경로가 여전히 동작하는지 확인 (롤백 검증).
6. dev → prod 순으로 반복.

---

## 완료 체크리스트

- [x] Gateway G1~G3 완료, G4/G5 임시값
- [x] `LlmGatewayProperties` (WebClient 는 `LlmGatewayClient` 가 직접 구성 — 기존 `OllamaModelClient` 와 같은 방식)
- [x] `LlmGatewayClient` (응답·에러 매핑, `finish_reason=length` 경고) + 단위 테스트 9건
- [x] `OllamaModelClient` 내부 분기 (public 시그니처 불변)
- [x] 프로파일별 `llm.gateway.*` 설정 (기본 off)
- [x] E2E: 실제 `classifyArticle()` → Gateway → exaone3.5:7.8b, 스키마 준수 JSON 확인 (`OllamaModelClientGatewayE2ETest`)
- [ ] 1-7 검증 (도메인 6개 + 롤백) — **배치를 실제로 돌려야 해서 미실시**
- [ ] Gateway 문서 `phase-01-implementation.md` DoD 3 ("Spring 연결 확인") 갱신
