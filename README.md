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

**Phase 6 코드 구현 완료.** 제한적 Retry/Fallback, 요청 전체 시간 제한, 배포별 Circuit Breaker를 적용했다. 실제 서버 장애 실측과 Phase 3 baseline 검증은 남아 있다.

- `pytest` — **실제 Ollama 없이 돈다** (R1~R6 진단 재현 테스트 포함)
- `GET /metrics` 로 L1/L2·설정·라우팅·복원력 지표 노출, `docker compose up -d` 로 Prometheus + Grafana(대시보드 8종)
- `GET /admin/diagnosis`로 규칙 기반 진단 조회. baseline 설정 후 `GATEWAY_DIAGNOSIS_ENABLED=true`
- YAML 감시, Redis 임시 override/TTL/pub-sub, 검증 후 무중단 설정 교체
- 세션 해시 기반 Weighted 분배, 선택 이유/대체 후보 보존, Redis weight/disable 즉시 반영
- HealthAware 옵션: 배포별 오류율/P95로 후보 제외, `/admin/routing/status`에서 관측 상태 조회
- 첫 토큰 수신 전 제한적 재시도/폴백, 실제 처리 배포의 응답 헤더, 취소 시 upstream 정리
- SSE 전송 중 연결 종료도 감시해 upstream 정지/느린 클라이언트 상황에서 즉시 취소
- `/admin/config`에서 유효 설정 조회. 모든 `/admin` API는 `GATEWAY_API_KEY` 설정 및 Bearer 인증 필수
- 구현 기록 / 실측 데이터 / 설계와 갈라진 지점:
  [Phase 1](docs/phases/phase-01-implementation.md) · [Phase 2](docs/phases/phase-02-implementation.md) ·
  [Phase 3](docs/phases/phase-03-implementation.md) · [Phase 4](docs/phases/phase-04-implementation.md) ·
  [Phase 5](docs/phases/phase-05-implementation.md) · [Phase 6](docs/phases/phase-06-implementation.md)

다음 구현은 **Phase 7 A/B 테스트**다.

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

5-c **HealthAware**도 구현되어 있다. `routing.strategy: health_aware`와
`routing.health_aware.thresholds`에 활성 배포별 실측 임계값을 함께 설정해야 한다.
최근 시도별 오류율 또는 성공 시도의 P95 지연시간이 기준을 넘으면 후보에서 제외하고,
남은 후보에 Weighted 선택을 적용한다. 표본이 부족하거나 만료되면 미확인 상태로 재진입한다.
임계값 없이 자동 활성화하지 않으며 기본 설정은 `static`이다.

`GET /admin/routing/status`(Bearer 인증)는 현재 worker의 표본 수, 오류율, P95 및 제외 이유를
반환한다. 관측 자료는 프로세스별 메모리에 있으며 재시작하면 초기화된다. 여러 worker 사이의
health 상태와 sticky 선택은 다를 수 있다.
[설정 예제와 운영 조건](docs/phases/phase-05-health-aware.md)을 참고한다.

### 복원력 (Phase 6)

`gateway.yaml`의 `resilience`에서 정책을 설정한다. 기본값은 최초 포함 최대 2회 시도,
fallback/breaker 비활성이다. fallback을 켜면 Router의 대체 후보를 사용하고, `max_chain: 2`는
최초 배포를 포함한 최대 두 배포를 뜻한다. 첫 응답 토큰 수신 후에는 두 API 모드 모두 재시도하지 않는다.

`timeout.total`은 재시도 대기와 폴백을 포함한 요청 전체 상한이며, `read`는 chunk 사이의
무응답 제한이다. 브레이커는 프로세스별로 관리하고 복구 확인 요청 하나만 허용한다.
자세한 설정·장애 동작·검증 범위는 [Phase 6 구현 기록](docs/phases/phase-06-implementation.md)을 참고한다.

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
├── routing/                 ModelRouter, Static/Weighted/HealthAware, RoutingDecision
├── resilience/              Retry/Fallback/Timeout/Circuit Breaker
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
