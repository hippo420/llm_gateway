# Phase 10 — 제한적 자동 대응

코드와 합성 장애 검증을 구현했다. 실제 운영의 진입 조건을 충족했다는 뜻은 아니다.
기본 `auto_remediation.enabled: false`이며 운영 Redis나 서빙 설정은 활성화하지 않았다.
Phase 8의 실제 서비스 품질·사람 채점 검증과 Phase 9 실제 승인 이력 수집도 여전히 남아 있다.

## 제어 흐름과 등급

Phase 9의 제안 → 원자적 설정 적용 → 관찰 → 검증/조건부 롤백을 재사용한다.
자동 적용은 별도 `auto_applied` 이벤트와 `automation_level`로 기록하고 `approved_by` 및
`approved_at`을 채우지 않는다. 자동 처리를 사람 승인 일치율에 포함하지 않는다.

| 등급 | 동작 |
|---|---|
| L0 | 기존 진단만 수집, 정책 제안/적용 없음 |
| L1 | 제안 후 사람 승인; 기본 등급 |
| L2 | 명시적 심사 허가와 전역 스위치가 있을 때 제한적으로 자동 적용 |
| L3 | L2 성공 10회 이상, 최초 성공 이후 30일 이상, 최근 30일 실패 없음, 유효 심사 근거를 만족하면 자동 승격 |

정책 `automation.level`은 허용 상한이다. L2 심사 허가를 먼저 받아야 하며 YAML에 L3만
적는 것으로 자동 적용되지 않는다. L3도 변경 예산·단일 배포·관찰·롤백·중단 스위치를
그대로 유지한다. 제한 없는 전면 제어는 구현하지 않았다. 자동 강등은 최근 30일 자동 적용의
롤백/롤백 충돌이 기본 2건이면 L1로 수행한다. 충돌을 resolve해도 실패 이력이 사라지지 않는다.
실패한 진행 건은 한 번의 롤백만으로도 후속 단계가 중단된다. 재시도에는 운영자 재심사가 필요하다.

## 활성화 심사

기존 `GATEWAY_POLICY_ENABLED=true` 및 진단·Redis·관리 API key가 필요하다.
`config/policies.yaml`의 전역 `auto_remediation` 설정과 정책별 `automation` 설정은
시작 시 읽으므로 수정 후 재시작한다. **설정 파일 수정만으로 실행 중 스위치가 바뀌지는 않는다.**

심사 API는 같은 정책/action과 같은 기본 Registry 설정에서 **검증까지 성공한 사람 승인
최소 30건**을 Redis 이력으로 확인한다. 일치율은 사람 승인 / (사람 승인 + 거부)이며 기본
0.95 이상이어야 한다. 정책 action이나 기본 설정이 달라지면 이전 이력으로 허가하지 않는다.

운영자는 다음 증빙도 제출한다. 날짜와 허용 범위는 코드가 검증하지만, 실제 한 달 수집의
안정성·오탐률 측정 방법·실환경 롤백 성공은 `evidence_reference` 자료에 대한 **운영자 확인**이다.
이 API가 한 달의 Prometheus 자료를 직접 조사하거나 증빙 문서 내용을 검증하지는 않는다.

- `metrics_stable_since`: 최소 30일 전, 지표 안정 수집 시작 시각
- `false_positive_rate`: 측정 오탐률, 기본 0.05 이하
- `rollback_verified_at`: 최근 30일 내 실제 롤백 검증 시각
- `evidence_reference`: 검증 보고서 위치/식별자, 공백 불가

허가는 정책 설정·기본 Registry·진단 설정에 묶인다. 허가 이후 첫 적용 전에 operator override가
변경되어도 재심사가 필요하다. 적용 직전에도 제안 당시 설정/TTL/진단/품질 gate와 허가·중단
스위치를 Redis 트랜잭션 안에서 다시 확인한다. 같은 스위치를 여러 인스턴스가 공유한다.

## API

모두 기존 관리용 Bearer key 인증 및 공백이 아닌 `reason`이 필요하다.
운영자 식별은 Phase 9와 동일한 API key 해시이므로 공유 키 사용자를 서로 구별하지는 못한다.

| 경로 | 동작 |
|---|---|
| GET `/admin/automation` | 설정 활성 여부, 실제 전역 스위치, 정책별 심사 허가 조회 |
| PUT `/admin/automation` | `{"enabled":false,"reason":"incident"}`로 즉시 새 자동 적용 중단 |
| POST `/admin/automation/{policy_id}/admit` | `reason`과 `evidence`로 심사·L2 허가/새 진행 건 등록 |
| POST `/admin/automation/{policy_id}/demote` | L1 강등 및 해당 대기 제안 만료 |
| GET `/admin/automation-journal?limit=100` | 최신 추가 전용 감사 이벤트, 최대 1,000개 |

심사 요청 형식(값은 실제 확인 결과로 작성):

```json
{
  "reason": "운영 이력 및 롤백 보고서 검토 완료",
  "evidence": {
    "metrics_stable_since": "2026-08-01T00:00:00+09:00",
    "false_positive_rate": 0.01,
    "rollback_verified_at": "2026-09-28T12:00:00+09:00",
    "evidence_reference": "운영 검증 보고서 식별자"
  }
}
```

심사 허가 후에도 전역 스위치는 별도 `PUT`으로 켜야 한다. YAML 설정이 false이면 API로 켤 수
없다. 스위치를 끄면 대기 중인 자동 제안도 만료한다. 이미 commit한 변경을 소급 취소하지는
않으며, 진행 중인 관찰·실패 롤백과 사람의 수동 롤백은 계속 가능하다. 즉시 되돌려야 한다면
기존 `POST /admin/policy-history/{id}/rollback`에 사유를 보낸다.

## 변경 범위·점진 적용·경쟁 방지

자동 action은 진단 대상 **한 배포**에 대한 `disable_deployment` 또는 새 `reduce_weight`만
허용한다. 기존 두 배포 간 `adjust_weight` 이동은 Phase 9 사람 승인으로 사용한다.
자동 timeout·concurrency 제한·fallback 체인 재배열·품질에 따른 모델 교체는 지원하지 않는다.
`reduce_weight`는 weighted/health_aware routing에서만 사용할 수 있다.

```yaml
actions:
  - {type: reduce_weight, target: model@primary, delta: -30}
automation:
  level: L2
  priority: 10
  max_steps: 3
  require_breaker_open: false
```

한 단계의 상대 weight 감소량은 최대 10이며 이전 단계 검증을 통과해야 다음 단계로 간다.
예를 들어 60/40에서 대상 weight가 50 → 40 → 30으로 감소한다. 라우터가 상대 가중치를
정규화하므로 **실제 트래픽 비율이 매번 10%p 줄어든다는 뜻은 아니다.** TTL이 만료되거나
운영자가 이전 단계 override/TTL을 바꾸면 다음 단계는 실행하지 않는다. 기본 최대 3단계,
설정한 총 감소량 또는 단계 상한에 도달하면 중단하며 새 진행에는 재심사가 필요하다.
중간 단계 실패 시 마지막 변경만 되돌려 직전 검증된 단계로 복원한다.

- 전역 상호배제: 자동 변경은 한 번에 하나만 관찰한다. 미해결 롤백 충돌도 추가 적용을 막는다.
- 우선순위: 높은 `priority` 우선, 동점은 policy ID 순서. 실제 commit에서도 상호배제 재검사.
- 시간당/최근 24시간 예산: 기본 각각 2회/6회. 자동 변경의 수동·자동 롤백도 집계하지만
  예산이 소진됐다는 이유로 롤백을 막지는 않는다.
- Phase 9의 cooldown, 최소/최대 weight, 마지막 가용 후보 보호, 활성 실험 충돌 방지 유지.
- 검증 창/최소 표본/결측 처리와 운영자 설정을 덮어쓰지 않는 조건부 롤백도 유지한다.

`require_breaker_open: true`는 Phase 6 breaker가 실제 OPEN이고 cooldown이 끝나지 않았을 때만
허용한다. breaker를 대신하거나 새 실패 카운터를 만들지 않는다. 기존 진단도 동시에 유효해야
한다. breaker는 기존처럼 프로세스 로컬 상태다. disable 정책은 남아 있는 정상 배포를 검증
대상으로 설정해야 한다. 비활성화된 배포의 요청 수로 성공 판정하려 하면 표본 부족으로 롤백된다.

## 감사와 알림

상태·override와 `gateway:policy:state:audit` Redis Stream append를 같은 WATCH/MULTI/EXEC에
넣었다. 이벤트는 적용 전후 설정, 진단 근거, 등급, 시각, 주체, 사유, 검증 결과를 담는다.
정책 상태의 `audit_sequence`, 스트림 마지막 ID와 길이를 확인해 스트림 삭제/중간 삭제/잘림/
잘못된 자료형이면 새 설정 변경을 거부한다. 기록에 TTL·XTRIM·XDEL을 사용하는 애플리케이션
경로는 없다. Phase 9 이전 이력은 유지하고 새 journal의 시작에 이전 상태 SHA-256을 기록한다.
과거 이벤트를 새 스트림에 작성했던 것처럼 소급 생성하지 않는다.

**이것은 애플리케이션의 추가 전용 감사 스트림이며 WORM 저장소는 아니다.** Redis 관리자 권한으로
상태와 스트림을 함께 조작하는 행위를 막거나 내용 전체의 위변조를 입증하지 못한다. 엄격한 불변
보존이 필요하면 외부 WORM/감사 시스템에 지속 반출하고 Redis ACL·백업·보존 정책을 설정해야 한다.
기존 AOF everysec의 최근 약 1초 손실 가능성도 남는다. 저장소를 복구할 때는 상태와 스트림을
일관된 백업에서 함께 복원한다. 무결성 오류를 없애려고 한쪽만 지우면 안 된다.

알림은 즉시 기록되는 `auto_applied` 감사/구조화 로그, 인증된 journal API, 다음 metrics 및
Prometheus 규칙으로 제공한다. Grafana Policies 대시보드에도 자동 적용 수·허가 등급·스위치를
추가했다. 실제 Slack/메일 수신처/Alertmanager는 연결하지 않았고 메시지를 보내지 않았다.
외부 전달 및 수신 확인은 운영에서 별도로 연결해야 한다. Prometheus 알림은 scrape 주기를 따른다.

| Metric | 의미 |
|---|---|
| `llm_gateway_auto_remediation_total{policy_id}` | 영속 이력의 자동 적용 수 |
| `llm_gateway_automation_level{policy_id}` | 설정 상한 안의 허가 등급 |
| `llm_gateway_auto_remediation_enabled` | 설정 + 런타임 전역 스위치 |
| `llm_gateway_policy_loop_errors_total` | controller 평가 루프 실패 수; 프로세스 재시작 시 초기화 |

기존 승인/거부 지표에 자동 적용을 사람 승인으로 더하지 않는다. replica별로 보이는 공유
감사 누계를 합산하지 않는다. 운영 근거가 부족하면 L1을 유지하는 것이 정상 동작이다.

## 검증

2026-09-29 최종 결과:

| 검사 | 결과 |
|---|---|
| 전체 pytest | 442 passed (11.72초) |
| Phase 10 자동 대응 테스트 | 27 passed (전체 실행에 포함) |
| Phase 9 정책 회귀 테스트 | 41 passed (전체 실행에 포함) |
| Ruff (`src tests scripts`) | 통과 |
| mypy | 93 source files, 오류 없음 |
| 기본 설정 | 자동화 false 확인 및 스키마 로딩 통과 |
| Grafana JSON/UID/panel ID | 대시보드 11개 정상 |
| 정책 알림 YAML | 5개 규칙 파싱 정상 |
| `git diff --check` | 통과 |

```powershell
.venv/Scripts/python.exe -m pytest -q --basetemp=.phase04-validation/phase10-final -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check src tests scripts
.venv/Scripts/python.exe -m mypy
```

실제 Redis 장애/복구, promtool/PromQL 실행, 실제 Grafana 렌더링은 이 자동 검증에 포함하지 않는다.

합성 이력과 fakeredis로 심사 거부, 두 중단 스위치, 비동기 경계에서 스위치 재검사, 단일 배포
3단계 감소, 예산, 경쟁 정책의 우선순위, 서로 다른 controller의 중복 적용 방지, 반복 롤백 강등,
L3 승격, 운영자 override 보존, OPEN breaker 연동, 감사 스트림 손실/잘림과 인증을 검증한다.
테스트의 사람 승인 30건과 장기간 자동 이력은 fixture이며 실제 운영 증거로 집계하지 않는다.

실제 장애의 자동 대응·트래픽 이동, 외부 알림 수신, Redis 영속 복구, WORM 보존, 오탐/악화율은
아직 실환경에서 검증하지 않았다. 이 결과로 운영 자동화를 활성화할 수 있다고 판단하지 않는다.
