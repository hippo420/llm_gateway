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

**Phase 10 제한적 자동 대응 코드 구현 완료.** 운영 증빙 심사, 전역 중단 스위치, 단일 배포 점진 적용, 변경 예산, 관찰·롤백 및 자동 강등을 추가했다. 기본은 비활성화다. 실제 서비스 품질·사람 채점, 실제 승인 이력과 자동 대응 운영 검증은 남아 있다.

- `pytest` — **실제 Ollama 없이 돈다** (R1~R6 진단 재현 테스트 포함)
- `GET /metrics` 로 L1/L2·설정·라우팅·복원력·실험·품질 지표 노출, `docker compose up -d` 로 Prometheus + Grafana(대시보드 10종)
- `GET /admin/diagnosis`로 규칙 기반 진단 조회. baseline 설정 후 `GATEWAY_DIAGNOSIS_ENABLED=true`
- YAML 감시, Redis 임시 override/TTL/pub-sub, 검증 후 무중단 설정 교체
- 세션 해시 기반 Weighted 분배, 선택 이유/대체 후보 보존, Redis weight/disable 즉시 반영
- HealthAware 옵션: 배포별 오류율/P95로 후보 제외, `/admin/routing/status`에서 관측 상태 조회
- 첫 토큰 수신 전 제한적 재시도/폴백, 실제 처리 배포의 응답 헤더, 취소 시 upstream 정리
- SSE 전송 중 연결 종료도 감시해 upstream 정지/느린 클라이언트 상황에서 즉시 취소
- 실험 우선 할당, warm-up/fallback 분리 집계, guardrail 위반 후 control 전환
- `GET /admin/experiments` 실험 보고서, `scripts/benchmark.py` 고정 배포 순차 벤치마크
- `scripts/evaluate.py` 품질 배치/사람 평가 검증, `GET /admin/evaluations` 영속 보고서 조회
- `/admin/config`에서 유효 설정 조회. 모든 `/admin` API는 `GATEWAY_API_KEY` 설정 및 Bearer 인증 필수
- 구현 기록 / 실측 데이터 / 설계와 갈라진 지점:
  [Phase 1](docs/phases/phase-01-implementation.md) · [Phase 2](docs/phases/phase-02-implementation.md) ·
  [Phase 3](docs/phases/phase-03-implementation.md) · [Phase 4](docs/phases/phase-04-implementation.md) ·
  [Phase 5](docs/phases/phase-05-implementation.md) · [Phase 6](docs/phases/phase-06-implementation.md) ·
  [Phase 7](docs/phases/phase-07-implementation.md) · [Phase 8](docs/phases/phase-08-implementation.md)

Phase 9 실행 및 한계: [구현·검증 기록](operations/results/2026-09-28-phase09-validation.md).
Phase 10: [구현·활성화 조건·한계](operations/results/2026-09-29-phase10-validation.md).
**운영 자동화는 아직 켜지 않는다.** Phase 10 심사에는 같은 정책·기본 설정에서 검증된 사람 승인 최소 30건, 일치율, 한 달의 지표 및 실환경 롤백 증빙이 필요하다. 운영 서빙 선택은 성능/자원/품질 실측 후 결정한다.

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

## Docker 서비스 (포트 35000)

Gateway는 컨테이너에서, Ollama는 WSL의 11434 포트에서 실행한다.
Ollama 전용 어댑터(`ollama`)로 `http://172.22.104.79:11434`에 연결하도록 설정한다.
Docker 전용 모델 설정은 `config/gateway.docker.yaml`이다.

| 요청의 model | 용도 | Ollama 모델 이름 |
| --- | --- | --- |
| `summary` | 단순 요약 | `qwen3:4b` |
| `analysis` | 분석 | `exaone3.5:7.8b` |
| `embeddings` | 임베딩 (1024차원) | `bge-m3:latest` |

WSL에서 `ollama list`로 모델 설치를 확인하고 Ollama 서버를 실행한다.
외부 접근에는 `OLLAMA_HOST=0.0.0.0:11434` 바인딩과 Windows에서 WSL로의 접근 경로가 필요하다.
현재 WSL 주소는 `172.22.104.79`이며 `wsl hostname -I`로 확인할 수 있다.
WSL 재시작으로 IP가 바뀌면 `config/gateway.docker.yaml`의 세 endpoint를 함께 변경한다.
Docker에서 WSL 주소에 접근할 수 있어야 하며, 현재 연결 확인에서는 타임아웃이 발생했다.
Windows 호스트의 `192.168.0.4:11434`는 LM Studio이므로 Ollama 주소로 사용하지 않는다.
`config/evaluation.yaml`은 호스트/WSL에서 실행하는 평가 스크립트용으로 `127.0.0.1:11434`를 사용한다.

```bash
curl http://172.22.104.79:11434/api/tags
```

반환된 `models[].name`을 설정의 각 `upstream_model`에 그대로 넣는다.
endpoint에는 `/v1`이나 `/api`를 넣지 않는다. 어댑터가 `/api/chat`, `/api/embed`, `/api/tags`를 덧붙인다.
임베딩 요청은 Gateway의 `/v1/embeddings`에 `model: "embeddings"`로 보낸다.

```bash
docker compose up -d --build gateway
curl http://localhost:35000/healthz
curl http://localhost:35000/readyz
curl http://localhost:35000/v1/models
curl http://localhost:35000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"summary","messages":[{"role":"user","content":"다음 문장을 요약해줘: LLM Gateway는 요청을 받아 호스트의 Ollama로 전달한다."}],"stream":false}'
```

분석 요청은 `model`을 `analysis`로 지정한다. 내용에 따라 자동으로 모델을 분류하지 않는다.
`GATEWAY_API_KEY`를 `.env`에 설정하면 요청에 `Authorization: Bearer <키>` 헤더를 추가한다.
관리 API는 키 설정이 필수다. `readyz`는 서버 연결과 설정한 모델 이름이 Ollama의 `/api/tags`에 있는지 확인한다.
실제 모델 로딩 및 추론 성공 여부는 채팅 요청으로 확인한다.

```bash
docker compose up -d --build           # Gateway + 관측 스택
docker compose logs -f gateway
docker compose stop gateway
```

Redis 사용 시 `docker compose --profile config up -d redis` 후
`.env`에 `GATEWAY_REDIS_URL=redis://redis:6379/0`을 설정하고 Gateway를 재생성한다.
Spring AI의 `base-url`은 `http://<Docker 호스트>:35000`, 논리 모델은 `summary` 또는 `analysis`다.
평가 결과는 `gateway-evaluations` 볼륨에 유지된다.

코드 변경 후 재빌드·검증·롤백 절차: [코드 변경 배포 가이드](operations/deploy-code-changes.md)

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
docker compose up -d                  # Gateway :35000 / Prometheus :9090 / Grafana :3000 (admin/admin)
docker compose --profile gpu up -d    # + GPU exporter
curl localhost:35000/metrics
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

### A/B 실험 (Phase 7)

`config/gateway.yaml`의 `experiments`에 동일 논리 모델에 속한 배포들을 등록하면 일반
Router보다 실험 할당을 우선한다. 기본 설정은 빈 목록으로 실험 비활성이다.
실험의 `bucket_key`는 `session_id`(`X-Session-Id`), `user_id`(`X-User-Bucket`),
`request_id` 중 하나다. 키가 없거나 유효하지 않으면 request_id를 사용한다.
`X-Gateway-Experiment`/`X-Gateway-Variant`는 최초 할당, `X-Gateway-Deployment`는 실제 배포다.

Guardrail은 최소 표본을 채운 뒤 오류율/TTFT P95 기준 초과 시 신규 요청을 control로 전환한다.
상태와 보고서는 worker별 메모리이며 재시작 시 초기화된다. 중단 알림은 구조화 로그와
Prometheus alert rule로 제공한다. 외부 알림 수신 채널은 별도 설정해야 한다.

한 GPU에서는 준비된 배포를 각각 시간 분할로 측정한다. 아래 smoke 데이터셋은 기능 확인용이다.

```powershell
.venv/Scripts/python.exe scripts/benchmark.py --deployment qwen-7b@ollama --dataset datasets/benchmark-smoke.jsonl --concurrency 1,4,8 --repeat 10 --warmup 2 --out operations/results/ollama-smoke.json
```

원자료 JSON과 Markdown 보고서를 함께 저장한다. 이 명령은 실제 모델을 호출하며, 벤치마크는
retry/fallback 없이 지정 배포를 측정한다. 현재 vLLM adapter는 미구현이므로 실제 vLLM 비교에는
adapter와 서버 준비가 필요하다. 설정 예제와 한계는 [Phase 7 기록](docs/phases/phase-07-implementation.md)을 참고한다.

### 품질 평가 (Phase 8)

`config/evaluation.yaml`로 별도 배치를 실행한다. 기본은 **합성 데이터 3종 × 30건과 L1 규칙 검사**다.
L2는 준비된 bge-m3 서버의 endpoint와 `similarity.enabled: true`를 설정하고,
L3는 `gateway.yaml`에 등록된 별도 모델의 ID를 `judge.deployment_id`에 지정하면 활성화된다.
설정만으로 모델을 다운로드하지 않는다. 평가 대상과 judge의 upstream 모델명이 같으면 실행을 거부한다.

```powershell
.venv/Scripts/python.exe scripts/evaluate.py run --deployment qwen-7b@ollama --run-id eval-001 --save-answers
```

이 명령은 실제 모델을 호출한다. 비교 배포는 `--deployment`를 반복해 지정한다. 각 배포의
답변 생성을 먼저 측정한 뒤 임베딩, judge 순서로 채점한다. retry/fallback/breaker와 A/B 할당은
배치에서 비활성이다. 결과는 `operations/evaluations/eval-001/`에 저장된다.

- `report.md` / `summary.json`: 성능·품질과 요청 종류별 적합도
- `results.json`: 개별 표본, 실패 사유, 데이터셋/설정/프롬프트 해시
- `human-review.jsonl`: 사람 평가 입력 양식. `--save-answers`를 켠 경우 로컬 답변·자료 포함
- `policy-input.json`: Phase 9 입력. 운영 라우팅을 직접 변경하지 않는 후보 자료

Judge를 켜고 실행한 결과에서 **고유 질의 최소 20건**을 사람이 채점한다. 검토할 행을 별도
JSONL로 복사하고 `human_score`(0~1), `reviewer`를 작성한다. 미채점 행은 제외하고 해시를 보존한다.

```powershell
.venv/Scripts/python.exe scripts/evaluate.py calibrate --run-id eval-001 --ratings operations/evaluations/eval-001/human-rated.jsonl
```

Pearson/Spearman/MAE를 계산하며 기본 검증 기준은 Spearman ≥ 0.5, MAE ≤ 0.2다.
합성 데이터 또는 사람 검증 미통과 결과에서는 권장 모델을 비워 둔다. 상관 검증은 해당 run에만 적용된다.
`--save-answers` 없이도 점수와 답변 해시는 저장되지만, 사람 검토용 답변은 별도로 보관해야 한다.

Gateway와 CLI가 같은 결과 디렉터리를 보도록 `GATEWAY_EVALUATION_RESULTS_PATH`와 `--out-dir`을 맞춘다.
인증된 `GET /admin/evaluations` 및 `GET /admin/evaluations/{run_id}`에서 요약을 확인할 수 있다.
Grafana `LLM Gateway / Quality`는 배치 점수·TTFT·TPS·검증 여부·데이터 출처·경과 시간을 함께 보여준다.
미측정 점수는 0으로 채우지 않는다. 전체 절차와 한계는
[Phase 8 기록](docs/phases/phase-08-implementation.md), 데이터 스키마는 [datasets/README.md](datasets/README.md)를 참고한다.

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
├── experiment/              실험 스키마, 할당, guardrail, 보고서, 벤치마크
├── evaluation/              배치 품질 평가, judge, 사람 상관 검증, 파일 보고서
├── resilience/              Retry/Fallback/Timeout/Circuit Breaker
├── adapters/                LLMAdapter ABC + Ollama 구현
├── service/                 ChatService (오케스트레이션)
└── observability/           Prometheus metrics (Phase 2)
observability/               prometheus.yml, Grafana provisioning + 대시보드 JSON
docker-compose.yml           Gateway + 관측 스택
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
