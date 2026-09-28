# Phase 9 — 사람 승인 기반 동적 정책

## 구현 범위

Phase 3에서 확정된 진단을 `trigger_rule_id`로 참조해 제안을 만든다. 임계값을 복제하지 않는다.
제안에는 진단 근거, 변경 전후 설정, 기대 효과, 만료 시각이 포함된다. 생성 자체는 라우팅을
바꾸지 않는다. 승인과 사유를 받은 뒤 Phase 4 Redis override로 적용한다.

지원 action은 `adjust_weight`, `disable_deployment`, `set_timeout`이다. Weight 변경은 같은
논리 모델 안에서 합계를 보존하며 static routing에서는 거부한다. 마지막 가용 배포 제거,
가중치 상·하한 위반, 활성 실험 중 변경도 거부한다. 각 배포에는 한 action만 지정한다.
`limit_concurrency`와 `switch_fallback`은 현재 Phase 4 override/서빙 계약에 해당 제어가 없어
지원하지 않으며 설정 검증에서 거부한다. 이름만 받아 아무 효과 없이 성공시키지 않는다.

정책·검증 설정은 `src/llm_gateway/policy/models.py`, 상태 전이는 `engine.py`에 있다.
검증 조건도 Phase 3 `Condition`/`Rule`의 true/false/unknown 의미를 그대로 사용한다.

## 기동과 설정

1. `docker compose --profile config up -d redis`로 영속 Redis를 준비한다.
2. `config/diagnosis.yaml`에 실측 baseline과 대상 배포를 등록한다.
3. `config/policies.yaml`의 주석 예제를 실제 ID와 측정 기준으로 작성한다.
4. 아래 환경변수를 설정한 뒤 Gateway를 재시작한다.

```dotenv
GATEWAY_DIAGNOSIS_ENABLED=true
GATEWAY_REDIS_URL=redis://localhost:6379/0
GATEWAY_API_KEY=<관리용 비밀 키>
GATEWAY_POLICY_ENABLED=true
GATEWAY_POLICY_CONFIG_PATH=config/policies.yaml
```

기본은 비활성화, 정책 목록은 비어 있다. 현재 운영 routing이나 실제 weight는 이번 구현에서
바꾸지 않았다. 정책 설정과 진단 baseline은 시작 시 읽으므로 변경 후 재시작한다.
정책 기동에는 진단·Redis·관리 키가 모두 필요하고, 감사 저장소가 손상됐거나 읽을 수 없으면
기동을 거부한다. 여러 인스턴스는 동일 YAML·정책·진단 설정을 배포해야 한다.

Compose Redis에 AOF와 `redis-data` 볼륨을 추가했다. 감사 이력에는 TTL이 없고 override에만
TTL이 있다. AOF `everysec`는 비정상 종료 시 최근 약 1초 손실 가능성이 있으므로 엄격한
운영 감사 요구에는 Redis 내구성·백업 정책을 별도 설정한다. `down -v`는 볼륨을 삭제한다.
기존 Redis 컨테이너 재생성 전에 필요한 기존 임시 override를 별도로 보존한다.

## 운영 API

모두 `Authorization: Bearer <GATEWAY_API_KEY>`가 필요하다. 결정 요청 body는
`{"reason":"근거를 확인한 후 작성하는 사유"}`이며 공백 사유는 거부한다.
승인 주체는 기존 관리 API와 동일하게 API key 해시로 기록한다. 공유 키를 쓴 사람들 사이의
개별 신원은 식별하지 못한다. 키 자체는 감사 이력과 로그에 기록하지 않는다.

| Method / 경로 | 동작 |
|---|---|
| GET `/admin/recommendations` | 승인 대기 목록과 `approval_required=true` |
| POST `/admin/recommendations/{id}/approve` | 근거·설정·guard 재검증 후 적용 |
| POST `/admin/recommendations/{id}/reject` | 거부자·거부 사유 기록 |
| GET `/admin/policy-history?offset=0&limit=100` | 전체 상태 이력, 최대 100건 페이지 |
| POST `/admin/policy-history/{id}/rollback` | 소유 중인 override만 되돌림 |
| POST `/admin/policy-history/{id}/resolve` | 운영자가 검토한 롤백 충돌 해제; 설정 변경 없음 |

상태: `pending → observing → validated / rolled_back / rollback_conflict`.
대기 제안은 `rejected` 또는 `expired`가 될 수 있다. 충돌 해제는 `resolved`이며 롤백 성공을
의미하지 않는다. 승인/거부 대상 없음은 404, 상태/guard/근거 충돌은 409다. 잘못된 body는
Gateway 기존 계약대로 400이다. Redis 장애/정책 비활성화는 설정 오류 응답이다.

## 원자성·충돌·시간 제한

`gateway:policy:state`와 기존 override key를 함께 WATCH/MULTI/EXEC로 갱신한다.
저장 전 유효 설정을 검증하고 commit 후 현재 프로세스의 registry를 교체한다. 다른 인스턴스는
기존 pub/sub·poll로 반영한다. 중복 승인, 이미 바뀐 YAML·override·TTL, 만료된 제안,
사라졌거나 오래된 진단은 적용하지 않는다. Redis 연결이 commit 직후 끊기면 결과가 불명확할
수 있으므로 재승인 전에 이력을 확인한다. 같은 ID를 재승인해도 두 번 적용되지 않는다.

기본 guard: 동시에 관찰 중인 정책 1개, 전체 변경 시간당 4회, 정책별 시간당 2회,
cooldown 900초, 최소 weight 10. 롤백도 변경 횟수에 포함하지만 롤백 자체는 횟수 제한으로
막지 않는다. 거부 후 즉시 같은 제안을 재생성하는 것도 cooldown으로 제한한다.
`max_pending`은 기본 20개다. 감사 이력은 10,000건 또는 50MB 한도에 도달하면 쓰기를
거부한다. 자동 삭제/아카이빙 기능은 없으므로 한도 이전에 운영 백업·보존 절차가 필요하다.

검증은 최소 300초 뒤, 변경 이후 시점의 5분 기본 Prometheus window로 수행한다.
`request_rate × 300`으로 추정한 최소 표본 수와 모든 성공 조건을 만족해야 한다.
진단 주기 2회만큼 최신 scrape를 기다린 뒤에도 결측·저표본·실패이면 롤백한다.
검증 대상에 custom query override가 있으면 window 길이를 보장할 수 없어 기동을 거부한다.
Override TTL은 관찰·scrape 대기·정책 주기를 모두 덮어야 하며 기존 operator TTL을 연장하지
않는다. 검증 성공 후에도 override는 원래 TTL에 만료된다. 영구 변경은 YAML에 반영한다.

롤백은 적용 직전 override와 남은 TTL을 복원한다. 이미 만료된 이전 override는 되살리지
않는다. 적용 후 YAML이나 대상 override/TTL을 운영자가 바꿨으면 덮어쓰지 않고
`rollback_conflict`로 남긴다. 운영자가 현재 설정을 직접 검토한 뒤 rollback 재시도 또는
resolve를 선택한다. 만료된 lease를 대체한 새 override도 보호한다.

## Phase 8 품질 근거

성능/장애 완화 정책은 사람 승인으로 사용할 수 있다. 품질을 근거로 트래픽을 늘릴 때는
정책의 `quality`에 아래를 명시한다.

```yaml
quality:
  run_id: actual-service-evaluation-run
  request_type: simple_qa
  max_age_sec: 86400
```

이 gate는 service 자료, 사람 검증 통과, 적격 destination, 평가 당시 배포 설정 해시,
자료 신선도를 제안과 승인 시점 모두 확인한다. 현재 합성 pilot/review 결과는 통과하지 못한다.
이 기능이 Phase 8 사람 채점을 대신하거나 request_type별 라우팅 기능을 추가하지는 않는다.

## 알림과 관측

`policy_recommendation` 및 `policy_transition` 구조화 로그, 인증된 대기 목록 API,
Prometheus alert rules, Grafana **LLM Gateway / Policies** 대시보드를 추가했다.
알림은 pending(1분), 롤백 충돌(즉시), 감사 저장소 장애(1분) 조건이다.
Prometheus `/alerts`에서 상태를 확인할 수 있다. Slack/메일 발송 수신처·Alertmanager는
연결하지 않았으며 메시지를 전송하지 않았다. 외부 통지는 운영 채널 연결 후 가능하다.

| Metric | 의미 |
|---|---|
| `llm_gateway_policy_pending{policy_id}` | 승인 대기 수 |
| `llm_gateway_policy_active{policy_id}` | 관찰 중 수 |
| `llm_gateway_policy_rollback_conflicts{policy_id}` | 미해결 충돌 수 |
| `llm_gateway_recommendation_total{policy_id,decision}` | 승인/거부/만료 누계 |
| `llm_gateway_policy_rollback_total{policy_id}` | 완료된 롤백 누계 |
| `llm_gateway_policy_store_up` | 감사 저장소 읽기 가능 여부 |

이력에서 계산하므로 프로세스 재시작에 누계가 초기화되지 않는다. 제거된 정책은 `retired`로
모아 label 수를 제한한다. 요청 ID·제안 ID·사용자·사유는 label에 넣지 않는다.
Redis 공유 이력의 counter를 여러 Gateway replica에 대해 합산하지 않는다.
Redis 장애 때도 다른 `/metrics`는 제공하고 store_up=0을 노출한다.

## 검증 및 운영 완료 조건

2026-09-28 최종 실행 결과:

| 검사 | 결과 |
|---|---|
| 전체 pytest | 415 passed (10.80초) |
| Phase 9 정책 테스트 | 41 passed |
| Ruff (`src tests scripts`) | 통과 |
| mypy | 92 source files, 오류 없음 |
| `docker compose config --quiet` | 통과 (기동/배포 없음) |
| 정책 기본 설정 및 관측 YAML 파싱 | 통과 |
| Grafana JSON / UID 중복 | 11개 정상 |
| `git diff --check` | 통과 |

```powershell
.venv/Scripts/python.exe -m pytest -q --basetemp=.phase04-validation/phase09-final -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m mypy
docker compose config --quiet
```

이번 검증은 promtool을 통한 PromQL 실행이나 실제 Grafana 화면 렌더링을 포함하지 않는다.

자동 검증은 실제 Ollama 없이 FakeAdapter/fakeredis로 실행한다. 승인 전 불변, 중복 승인,
설정·근거 변경, 거부 사유, 가중치/timeout/disable 복원, TTL, 표본·결측·관찰 window,
flapping guard, 품질 gate, 인증·API·metrics, 저장 실패/손상과 롤백 충돌을 재현한다.
테스트의 승인 5건·거부 5건은 합성 판단이며 실제 사람 승인 이력으로 세지 않는다.

운영 완료까지 남은 항목은 실측 baseline으로 장애 재현, 실제 Redis/Prometheus/Grafana에서
승인 후 트래픽 이동과 실패 롤백 관측, 통지 채널 수신 확인, 실제 승인/거부 최소 10건과
잘못된 제안의 거부 이유 분석이다. 이 조건을 충족하기 전 Phase 10 자동 승인에는 진입하지 않는다.
