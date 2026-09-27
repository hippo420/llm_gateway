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

**Phase 5 필수 범위(Static/Weighted 라우터) 코드 구현 완료.** 실제 환경의 분배/성능 실측 및 Phase 3 baseline 검증은 남아 있다.

- `pytest` — **실제 Ollama 없이 돈다** (R1~R6 진단 재현 테스트 포함)
- `GET /metrics` 로 L1/L2·설정·라우팅 지표 노출, `docker compose up -d` 로 Prometheus + Grafana(대시보드 7종)
- `GET /admin/diagnosis`로 규칙 기반 진단 조회. baseline 설정 후 `GATEWAY_DIAGNOSIS_ENABLED=true`
- YAML 감시, Redis 임시 override/TTL/pub-sub, 검증 후 무중단 설정 교체
- 세션 해시 기반 Weighted 분배, 선택 이유/대체 후보 보존, Redis weight/disable 즉시 반영
- `/admin/config`에서 유효 설정 조회. 모든 `/admin` API는 `GATEWAY_API_KEY` 설정 및 Bearer 인증 필수
- 구현 기록 / 실측 데이터 / 설계와 갈라진 지점:
  [Phase 1](docs/phases/phase-01-implementation.md) · [Phase 2](docs/phases/phase-02-implementation.md) ·
  [Phase 3](docs/phases/phase-03-implementation.md) · [Phase 4](docs/phases/phase-04-implementation.md) ·
  [Phase 5](docs/phases/phase-05-implementation.md)

다음 구현은 **Phase 6 복원력(Retry/Fallback/Circuit Breaker)**이다.

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
- [spring/README.md](spring/README.md) — **Spring 앱(mony_batch) 쪽 작업** 단계별 가이드

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

### 관측 스택 (Phase 2)

```bash
docker compose up -d                  # Prometheus :9090 / Grafana :3000 (admin/admin)
docker compose --profile gpu up -d    # + GPU exporter
curl localhost:8080/metrics
```

포트 충돌 / Windows 포트 예약 / WSL GPU 주의사항: [observability-stack.md](docs/operations/observability-stack.md)

### 테스트

```bash
pytest
ruff check src tests
mypy
```

테스트는 **실제 Ollama 없이** 전부 통과해야 한다. (`tests/conftest.py` 의 `FakeAdapter`)

### 동적 설정 (Phase 4)

YAML 변경은 기본 5초 주기로 감지한다. 검증 실패 시 기존 설정을 유지하고,
이미 처리 중인 요청은 이전 endpoint/timeout으로 완료한다.

```bash
docker compose --profile config up -d redis
```

`.env`에 `GATEWAY_REDIS_URL=redis://localhost:6379/0` 및 `GATEWAY_API_KEY`를 설정하고
Gateway를 시작한다. Redis 없이도 YAML 감시/조회/수동 reload는 사용할 수 있다.
임시 변경은 `PUT /admin/deployments/{id}`로 적용하며, `reason`은 필수,
`ttl_sec`는 기본 3600초(최대 86400초)다. 상세 예제와 장애 동작은
[Phase 4 구현 기록](docs/phases/phase-04-implementation.md)을 참고한다.

### 모델 라우팅 (Phase 5)

`config/gateway.yaml`에서 `routing.strategy: weighted`로 설정하고, **같은 논리 모델**의
deployment에 `weight: 80` / `weight: 20`을 지정한다. 기본 `static`은 기존처럼 첫 enabled
deployment를 선택한다. Weighted는 enabled이며 weight가 양수인 배포만 사용한다.

동일 `X-Session-Id`는 동일 모델·후보·가중치에서 같은 배포로 간다. 세션 헤더가 없으면
`X-User-Bucket`, 이후 request_id를 사용한다. weight/enable 변경 시 세션 배정은 바뀔 수 있다.
Grafana `LLM Gateway / Routing`에서 분배와 deployment별 TTFT/TPS를 비교한다.
전체 예제와 검증 범위는 [Phase 5 구현 기록](docs/phases/phase-05-implementation.md)을 참고한다.

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
├── routing/                 ModelRouter, Static/Weighted, RoutingDecision
├── adapters/                LLMAdapter ABC + Ollama 구현
├── service/                 ChatService (오케스트레이션)
└── observability/           Prometheus metrics (Phase 2)
observability/               prometheus.yml, Grafana provisioning + 대시보드 JSON
docker-compose.yml           관측 스택
```

요청 흐름:

```text
RequestIdMiddleware → AccessLogMiddleware → chat route
   → ChatService.prepare()   검증 + ModelRouter 선택 (스트림 시작 전)
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
