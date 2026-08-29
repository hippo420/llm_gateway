# LLM Gateway

Spring Boot + Spring AI 서비스와 실제 LLM Serving Framework(Ollama / vLLM / 외부 API) 사이에 놓이는
**Control Plane**.

> Spring 애플리케이션은 실제 Serving Framework 나 모델 서버의 물리적 위치를 알 필요가 없어야 한다.

```text
Spring Boot → Spring AI → [ LLM Gateway ] → Ollama / vLLM / External → GPU
                                │
                                └─► Metrics / Logs / Traces → Prometheus / Grafana
```

---

## 현재 상태

**Phase 1 구현 완료.** (Spring 앱 연결 확인만 미실시)

- `pytest` 49개 통과 — **실제 Ollama 없이 돈다**
- 남아 있는 `NotImplementedError` 는 Phase 2(metrics) / Phase 4(Redis config) 자리표시자뿐
- 구현 기록 / 실측 데이터 / 설계와 갈라진 지점:
  [`docs/phases/phase-01-implementation.md`](docs/phases/phase-01-implementation.md)

다음 작업은 Phase 2 (계측). `ChatService` 의 `# Phase 2:` 주석 지점에
metric 기록을 추가하면 된다 — 값은 이미 계산되어 있다.

---

## 문서

설계 문서가 정본이다. 코드를 고치기 전에 문서를 먼저 본다.

- [docs/README.md](docs/README.md) — 문서 색인 / Phase 로드맵
- [docs/phases/phase-01-implementation.md](docs/phases/phase-01-implementation.md) — Phase 1 구현 기록
- [docs/00-architecture.md](docs/00-architecture.md) — 아키텍처, 책임 분리, 개발 규약
- [docs/specs/api-spec.md](docs/specs/api-spec.md) — HTTP API
- [docs/specs/adapter-interface.md](docs/specs/adapter-interface.md) — Adapter 계약
- [docs/specs/config-spec.md](docs/specs/config-spec.md) — 설정
- [docs/specs/metrics-spec.md](docs/specs/metrics-spec.md) — Metric 이름/Label
- [docs/specs/error-codes.md](docs/specs/error-codes.md) — 에러 코드

---

## 개발 환경

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # WSL / Linux

pip install -r requirements-dev.txt
pip install -e .

cp .env.example .env
```

### 실행

```bash
uvicorn llm_gateway.main:app --host 0.0.0.0 --port 8080 --reload
# 또는
python -m llm_gateway.main
```

### 테스트

```bash
pytest
ruff check src tests
mypy
```

테스트는 **실제 Ollama 없이** 전부 통과해야 한다. (`tests/conftest.py` 의 `FakeAdapter`)

---

## 구조

```text
config/gateway.yaml          논리 모델 ↔ deployment 매핑 (정본)
src/llm_gateway/
├── main.py                  app factory / lifespan / 에러 핸들러
├── settings.py              환경변수
├── api/                     라우터, DI, 엔드포인트
├── schemas/                 OpenAI 호환 DTO (Spring AI 와의 계약)
├── core/                    context / errors / logging / timing
├── middleware/              request_id, access log
├── registry/                Model Registry, Config loader
├── adapters/                LLMAdapter ABC + Ollama 구현
├── service/                 ChatService (오케스트레이션)
└── observability/           Metrics (Phase 2)
```

요청 흐름:

```text
RequestIdMiddleware → AccessLogMiddleware → chat route
   → ChatService.prepare()   검증 + deployment 선택 (스트림 시작 전)
   → ChatService.complete()  내부 streaming 집계 → TTFT 확보
   → OllamaAdapter           NDJSON 파싱 / 나노초 → 초 / 예외 → GatewayError
   → 응답 정규화 + chat_completed 로그
```

---

## Spring Boot 연결

```yaml
spring:
  ai:
    openai:
      base-url: http://localhost:8080
      api-key: ${GATEWAY_API_KEY:dummy}
      chat:
        options:
          model: qwen-7b        # 논리 모델명
```

Spring 로그와 Gateway 로그를 이어붙이려면 MDC 요청 ID 를 `X-Request-Id` 헤더로 전달한다.

---

## 지켜야 할 것

1. 서비스/라우터 계층에 `if adapter == "ollama"` 같은 분기를 두지 않는다.
2. endpoint / timeout / 기본 파라미터를 코드에 하드코딩하지 않는다.
3. Metric label 에 `request_id`, `user_id`, 프롬프트를 넣지 않는다.
4. 모르는 값을 `0` 으로 채우지 않는다. `None` 은 "모른다"는 뜻이다.
5. 자동화보다 관측을 먼저 완성한다.

전체 규약: [docs/00-architecture.md](docs/00-architecture.md) "7. 개발 규약"
