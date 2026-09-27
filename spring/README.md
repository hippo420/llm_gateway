# Spring 앱 연동 가이드 (mony_batch)

> LLM Gateway 의 각 Phase 에서 **Spring 쪽이 해야 할 일**을 단계별로 정리한다.
> Gateway 설계서(`docs/`)가 "Gateway 가 무엇을 하는가"라면, 이 폴더는 "Spring 이 무엇을 바꿔야 하는가"다.
>
> 대상: `D:\workspace\mony_batch` (Spring Boot 3.5.16 / Spring AI 1.1.8 / Spring Batch)

---

## 1. 단계 목록

| 단계 | 문서 | 한 줄 요약 | 대응 Gateway Phase | 상태 |
|---|---|---|---|---|
| 1 | [phase-01-gateway-connection.md](./phase-01-gateway-connection.md) | Ollama 직접 호출 → Gateway 경유로 전환 (플래그로 롤백 가능) | Phase 1 | **구현 완료** (플래그 off, 배치별 검증 남음) |
| 2 | [phase-02-correlation-and-metrics.md](./phase-02-correlation-and-metrics.md) | `X-Request-Id` 전파, LLM 호출 요약 로그, Spring 측 지표 | Phase 2 | **구현 완료** (앱 기동 후 scrape 확인 남음) |
| 3 | — | (Spring 작업 거의 없음. Gateway 규칙 엔진) | Phase 3 | 미작성 |
| 4 | — | (Spring 작업 없음. Gateway 동적 설정) | Phase 4 | 미작성 |
| 5 | — | `X-Request-Type` / `X-Session-Id` 로 라우팅 힌트 전달 | Phase 5 | 미작성 |
| 6 | — | Spring 쪽 재시도 제거, Gateway 에러 코드별 처리 | Phase 6 | 미작성 |
| 7~10 | — | 실험 variant 헤더 처리, 품질 평가 데이터셋 제공 등 | Phase 7~10 | 미작성 |

---

## 2. 현재 mony_batch 의 LLM 호출 구조 (2026-09-24 기준 조사)

**설계서의 전제("Spring AI `base-url` 만 바꾸면 붙는다")는 mony_batch 에 그대로 맞지 않는다.**
채팅 호출이 Spring AI 를 거치지 않기 때문이다.

| 용도 | 구현 | 프로토콜 | Gateway 경유 대상? |
|---|---|---|---|
| 채팅/생성 (12개 호출 지점) | `infra/llm/OllamaModelClient` (WebClient 직접) | Ollama 네이티브 `/api/generate`, `/api/chat` | **대상** |
| 임베딩 (bge-m3) | `VectorConfig` 의 `OllamaEmbeddingModel` (Spring AI) | Ollama `/api/embed` | 대상 아님 — Gateway 에 embeddings API 없음 |
| Gemini | `GeminiApiClient`, `OllimaApiClient` | Google API | 대상 아님 — Gateway 에 external adapter 없음 |
| Spring AI ChatModel/ChatClient | **사용처 없음** (`spring.ai.ollama.chat.*` 설정만 존재) | — | — |

`OllamaModelClient` 가 쓰는 Ollama 전용 기능:

| 기능 | 사용처 | OpenAI 호환 API 대응 |
|---|---|---|
| `format: <JSON Schema>` (structured output) | `generateJSON()` — 뉴스 분석/리포트 분석/AI 종목 요약 | `response_format: {type: json_schema}` |
| `options.num_ctx` (호출마다 다른 값) | `AiSummaryGenerator`(8192), `NewsContentAnalyzer`/`ReportContentAnalyzer`(자동 산출) | **표준 없음** |
| `options.temperature` | 0.2 고정 (`chat()` 은 미지정 → Ollama 기본값) | `temperature` |

→ 그래서 1단계는 "설정 변경"이 아니라 **`OllamaModelClient` 의 전송 계층 교체**이고,
Gateway 쪽에 선행 작업(G1~G3, 구현 완료)이 있었다. 자세한 내용: [phase-01 "0. Gateway 선행 작업"](./phase-01-gateway-connection.md#0-gateway-선행-작업-spring-작업-전에-끝나야-함)

---

## 3. 구현 위치 (mony_batch)

| 파일 | 단계 | 역할 |
|---|---|---|
| `common/config/LlmGatewayProperties.java` | 1 | `llm.gateway.*` |
| `infra/llm/gateway/LlmGatewayClient.java` | 1·2 | `/v1/chat/completions` 호출, 에러 매핑, request_id/MDC, `llm_call` 로그, `mony_llm_call_seconds` |
| `infra/llm/gateway/LlmGatewayRequest` / `Result` / `Exception` | 1 | DTO |
| `infra/llm/gateway/LlmContextMetrics.java` | 2 | `mony_llm_context_seconds` |
| `infra/llm/OllamaModelClient.java` | 1 | `generate` / `generateJSON` / `chat` 내부에서 플래그 분기 (시그니처 불변) |
| `batch/ai/processor/AiStockSummaryProcessor.java` | 2 | `stock_context` 수집 시간 측정 |
| `batch/stock/tasklet/StockAlertTasklet.java` | 2 | `news_vector` 검색 시간 측정 |
| `application-{local,dev,prod}.properties` | 1·2 | `llm.gateway.*`, actuator 노출 범위 |
| `logback-spring.xml` | 2 | MDC `REQUEST_ID` (JSON + 콘솔) |
| `build.gradle` | 2 | actuator, micrometer-registry-prometheus |
| 테스트 `infra/llm/gateway/LlmGatewayClientTest` | 1·2 | fake gateway(JDK HttpServer) 9건 |
| 테스트 `infra/llm/OllamaModelClientGatewayE2ETest` | 1 | 실제 Gateway+Ollama. `LLM_GATEWAY_E2E=true` 일 때만 |

---

## 4. 원칙

1. **호출부(12곳)는 건드리지 않는다.** `OllamaModelClient` 의 public 메서드 시그니처를 유지하고 내부 전송만 바꾼다.
2. **플래그 하나로 되돌릴 수 있어야 한다.** `llm.gateway.enabled=false` 면 기존 Ollama 직접 호출로 돌아간다.
3. **Spring 은 serving 을 모른다.** 물리 모델명(`qwen2.5:14b`)이 아니라 **논리 모델명**(`qwen-14b`)으로 부른다.
4. **Spring 에서 LLM 재시도를 새로 만들지 않는다.** 재시도/폴백은 Gateway Phase 6 의 책임이다. 양쪽에서 재시도하면 호출 수가 곱으로 늘어난다.
5. **프롬프트 원문은 metric 에도, 요약 로그에도 넣지 않는다.** (Gateway 와 같은 규칙)
