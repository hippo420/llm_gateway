# Phase 10 학습 — 자동 대응 (Auto Remediation)

> 설계서: [`../docs/phases/phase-10-auto-remediation.md`](../docs/phases/phase-10-auto-remediation.md)
> 선행: **진입 조건을 전부 만족했을 때만** 시작한다 (1절)

새로운 Python 기술은 거의 없다. Phase 9 의 워크플로에서 **사람을 빼는 것**이 전부다.
그래서 이 Phase 의 학습 내용은 **안전장치를 어떻게 코드로 강제하는가**에 집중된다.

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | 자동화 등급(L0~L3) 관리 + 자동 승격/강등 | — | 중 |
| 2 | Kill switch (전역 플래그의 올바른 구현) | Feature toggle | 중 |
| 3 | 변경 예산 / blast radius 제한 | Rate limiter | 중 |
| 4 | 점진 적용 (분할 실행) | — | 중 |
| 5 | 정책 충돌 해소 (우선순위 + 상호배제) | — | **상** |
| 6 | **불변 감사 로그** (append-only, fsync) | 감사 테이블 | **상** |
| 7 | 승격 판정 통계 (Wilson score) | — | 중 |
| 8 | 카오스 테스트 | — | **상** |

---

## 1. 진입 조건 — 코드보다 먼저 확인한다

아래를 **전부** 만족하지 않으면 이 Phase 를 시작하지 않는다.

- [ ] Phase 2 지표가 최소 1개월 안정적으로 수집됨
- [ ] Phase 3 규칙의 오탐률이 파악됨
- [ ] Phase 9 승인 이력이 **30건 이상**
- [ ] 그중 **사람의 판단과 정책 제안이 일치한 비율**이 측정됨
- [ ] Rollback 이 실제로 동작함이 검증됨

> 일치율이 낮은 정책은 **자동화 대상에서 제외**한다. 전부 자동화하는 것이 목표가 아니다.

일치율 계산:

```python
def agreement_rate(history: list[PolicyRecord]) -> float:
    decided = [h for h in history if h.status in (APPROVED, REJECTED)]
    if len(decided) < 30:
        return 0.0                    # 표본 부족 = 자동화 불가
    return sum(1 for h in decided if h.status == APPROVED) / len(decided)
```

**"승인 비율" 이 곧 "제안이 옳았던 비율" 이다.** 사람이 절반을 거부했다면 그 정책은 절반이 틀렸다.

---

## 2. 자동화 등급

| 등급 | 동작 | 대상 |
|---|---|---|
| **L0 Observe** | 진단만 기록 | 새로 만든 규칙 |
| **L1 Suggest** | 제안 + 알림 (= Phase 9) | 검증 중인 정책 |
| **L2 Auto (제한적)** | 자동 적용 + 즉시 알림 + 쉬운 rollback | 일치율 높고 되돌리기 쉬운 것 |
| **L3 Auto (전면)** | 자동 적용 | 장기간 무사고인 것만 |

```python
class AutomationLevel(int, Enum):     # int 상속 → 비교 연산이 자연스럽다
    OBSERVE = 0
    SUGGEST = 1
    AUTO_LIMITED = 2
    AUTO_FULL = 3


if policy.level >= AutomationLevel.AUTO_LIMITED:
    await self._apply_automatically(policy, diagnosis)
else:
    await self._create_recommendation(policy, diagnosis)     # Phase 9 경로
```

`int, Enum` 다중 상속 덕분에 `>=` 비교가 된다 (`str, Enum` 과 같은 관용구).

**Phase 9 코드를 그대로 두고 분기 하나만 추가한다.** L1 경로는 손대지 않는다.

### 2.1 승격은 데이터로, 강등은 자동으로

```python
def evaluate_level(policy_id: str, history: list[PolicyRecord]) -> AutomationLevel:
    recent = history[-20:]

    rollback_rate = sum(1 for h in recent if h.rolled_back) / max(len(recent), 1)
    if rollback_rate > 0.2:
        return AutomationLevel.SUGGEST          # 강등: 자동 적용 후 rollback 이 잦다

    if len(history) >= 30 and agreement_rate(history) > 0.9 and rollback_rate == 0:
        return AutomationLevel.AUTO_LIMITED     # 승격
    return current_level
```

> **강등도 자동으로 되어야 한다** (설계서 2장).
> 승격만 자동이면 나빠진 정책이 계속 자동 적용된다.
> 강등 기준을 승격 기준보다 **느슨하게(= 쉽게 강등되게)** 두는 것이 안전하다.

### 2.2 승격 판정에 표본이 부족할 때

성공률 100% 라도 표본이 5건이면 신뢰할 수 없다. **Wilson score 하한**을 쓰면 표본 수가 반영된다.

```python
import math

def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    if total == 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    center = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return (center - margin) / denom
```

```text
5/5   성공 →  하한 0.57   (승격 불가)
30/30 성공 →  하한 0.89   (승격 가능)
```

**"작은 표본의 100%" 를 걸러주는 것이 요점이다.** 별도 라이브러리 없이 `math` 만으로 된다.

---

## 3. 안전장치 — 코드로 강제한다

설계서 4장의 8가지. **"주의해서 쓴다" 가 아니라 통과할 수 없게 만든다.**

### 3.1 Kill switch

```python
async def maybe_apply(self, policy: Policy, diagnosis: Diagnosis) -> None:
    if not self._config.auto_remediation_enabled:      # ← 모든 자동 경로의 첫 줄
        log.info("auto remediation disabled by kill switch")
        return
    ...
```

| 요구사항 | 구현 |
|---|---|
| **하나로 전체 중단** | `auto_remediation.enabled: false` (Phase 4 설정) |
| **재기동 없이 즉시** | Redis override + pub/sub (Phase 4) |
| **모든 진입점에서 검사** | 검사 지점이 여러 곳이면 언젠가 하나를 빠뜨린다 → **진입점을 하나로 만든다** |

```python
# 자동 적용은 이 함수 하나만 통과한다
async def _apply_automatically(self, ...) -> None:
    self._assert_safe_to_apply(policy)      # kill switch + 예산 + blast radius + 하한선
    ...
```

**검사를 한 함수에 모으는 것이 핵심 설계다.** 흩뿌리면 새 경로를 추가할 때 빠진다.

### 3.2 Blast radius — 한 번에 하나의 deployment

```python
targets = {a.target for a in policy.actions}
if len(targets) > 1 and policy.level < AutomationLevel.AUTO_FULL:
    raise PolicyGuardError("L2 policies may change only one deployment at a time")
```

> weight 이동 정책은 본질적으로 2개를 건드린다 (`-30` / `+30`).
> 이 경우는 "하나의 모델 내 재분배" 로 예외 처리하되, **모델 경계를 넘지 않게** 강제한다.

### 3.3 변경 예산

Phase 9 의 `ChangeBudget` (deque sliding window) 을 재사용하되, **전역 예산**을 추가한다.

```python
self._policy_budget = ChangeBudget(limit=guard.max_change_per_hour)   # 정책별
self._global_budget = ChangeBudget(limit=6, window_sec=3600)          # 전체 시스템
self._daily_budget = ChangeBudget(limit=20, window_sec=86400)
```

정책별 예산만 있으면 정책 5개가 각자 2회씩 = 시간당 10회 변경이 된다.

### 3.4 하한선

```python
def _assert_not_all_zero(self, after: RegistrySnapshot) -> None:
    for model, deployments in after.models.items():
        if sum(d.weight for d in deployments if d.enabled) <= 0:
            raise PolicyGuardError(f"all weights would be zero: {model}")
```

Phase 9 에서 만든 전역 검증을 그대로 쓴다. **자동 경로에서는 필수다.**

### 3.5 점진 적용

```text
weight 30% 이동  →  10% × 3회 (각 회차 사이에 observe)
```

```python
def split_action(action: Action, steps: int = 3) -> list[Action]:
    if action.delta is None:
        return [action]
    step = action.delta // steps
    remainder = action.delta - step * (steps - 1)
    return [replace(action, delta=step) for _ in range(steps - 1)] + [replace(action, delta=remainder)]
```

`dataclasses.replace()` 는 **불변 객체의 일부만 바꾼 새 객체**를 만든다 (Java record 의 wither).
`frozen=True` dataclass 를 다룰 때 계속 쓰게 된다.

나머지를 마지막 스텝에 몰아주는 것이 정수 나눗셈 오차를 흡수하는 방법이다.

### 3.6 정책 충돌 해소

동시에 여러 정책이 매칭될 수 있다.

```python
@dataclass(frozen=True)
class Policy:
    ...
    priority: int = 100                       # 낮을수록 우선
    mutex_group: str | None = None            # 같은 그룹은 동시 적용 불가


def resolve_conflicts(matched: list[Policy], active: set[str]) -> list[Policy]:
    matched.sort(key=lambda p: (p.priority, p.id))     # 결정론적 정렬 (동점 시 id)
    selected: list[Policy] = []
    used_groups = {p.mutex_group for p in matched if p.id in active}

    for policy in matched:
        if policy.mutex_group and policy.mutex_group in used_groups:
            continue
        selected.append(policy)
        used_groups.add(policy.mutex_group)
        break                                  # L2 에서는 한 번에 하나만
    return selected
```

**정렬 키에 `p.id` 를 넣는 이유:** 우선순위가 같을 때 순서가 매번 달라지면 재현이 안 된다.
동점 처리를 명시하지 않으면 dict 순회 순서에 의존하게 된다.

### 3.7 관찰 후 확정 / 알림

Phase 9 의 검증 루프를 그대로 쓴다. **자동 변경도 사람에게 반드시 알린다 (사후 통보).**

---

## 4. 불변 감사 로그

> 이 로그는 **삭제되지 않는 저장소**에 남긴다. 자동 시스템의 유일한 설명 책임 수단이다.

### 4.1 기록 내용 (설계서 5장)

```json
{
  "policy_id": "SHIFT_TRAFFIC_ON_ERROR",
  "trigger": "error_rate > 0.05 for 5m",
  "trigger_evidence": { "error_rate": 0.081, "window": "5m" },
  "before_config": { "qwen-7b@ollama": 100, "qwen-7b@vllm": 0 },
  "after_config":  { "qwen-7b@ollama": 70,  "qwen-7b@vllm": 30 },
  "timestamp": "2026-08-25T10:20:00Z",
  "reason": "Ollama endpoint error rate 급증",
  "automation_level": "L2",
  "result": "success",
  "validation": { "observed_sec": 600, "error_rate_after": 0.012 },
  "rolled_back": false
}
```

### 4.2 append-only 구현

```python
class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def append(self, record: AuditRecord) -> None:
        line = record.model_dump_json() + "\n"
        async with self._lock:                       # 줄 섞임 방지
            await asyncio.to_thread(self._write_sync, line)

    def _write_sync(self, line: str) -> None:
        with open(self._path, "a", encoding="utf-8") as f:   # "a" = append 모드
            f.write(line)
            f.flush()
            os.fsync(f.fileno())                     # OS 버퍼까지 디스크로 강제
```

| 요소 | 이유 |
|---|---|
| `"a"` 모드 | 기존 내용을 절대 덮어쓰지 않는다 |
| `asyncio.Lock` | 동시 쓰기 시 줄이 섞이는 것을 막는다 |
| `to_thread` | 파일 I/O + fsync 는 블로킹이다 |
| `flush()` + `os.fsync()` | **프로세스가 죽어도 기록이 남는다.** 감사 로그의 핵심 |
| JSONL | 한 줄 = 한 레코드. 손상 시에도 나머지를 읽을 수 있다 |

> `fsync` 는 느리다(수 ms). 자동 변경은 시간당 몇 건이므로 문제없다.
> **일반 로그에는 쓰지 않는다.**

### 4.3 삭제 방지

- 파일 권한을 append-only 로 (Linux: `chattr +a`, Windows: ACL)
- 또는 외부 저장소로 전송 (S3 object lock, 로그 수집기)
- **최소한 코드 어디에도 이 파일을 지우거나 자르는 경로를 두지 않는다**

로테이션이 필요하면 **날짜별 새 파일**을 만든다. 기존 파일을 자르지 않는다.

---

## 5. 자동화 후보 (L2 부터)

```text
Error Rate > 5%          → Fallback Model 로 전환
TTFT P95 > 5s            → Traffic 분산
GPU Memory > 95%         → Concurrency 감소
Quality Score < 임계값    → 이전 Model 로 Rollback
Circuit Breaker OPEN     → 해당 deployment weight 0 (일시)
```

> **가장 안전한 시작점은 Circuit Breaker 연동이다.**
> 이미 Phase 6 에서 검증된 동작이고 되돌리기가 자명하다.

```python
async def on_breaker_open(self, deployment_id: str) -> None:
    if not self._config.auto_remediation_enabled:
        return
    await self._apply_automatically(
        policy=BREAKER_POLICY,
        actions=(Action(ActionType.ADJUST_WEIGHT, target=deployment_id, value=0),),
        reason=f"circuit breaker OPEN: {deployment_id}",
    )

async def on_breaker_closed(self, deployment_id: str) -> None:
    await self._restore_previous_weight(deployment_id)     # 역연산이 자명하다
```

**"weight 0" 은 `min_weight` 하한선의 예외**다 — breaker 는 이미 그 deployment 를 못 쓰는 상태이므로.
단, **다른 deployment 가 남아 있는지** 반드시 확인한다.

---

## 6. 카오스 테스트

> 체크리스트 마지막: **"정책이 서로 밀어내는 상황 재현"**

### 6.1 시뮬레이터로 테스트한다

실제 GPU 부하로는 재현이 불가능하다. **지표를 가짜로 주입**한다.

```python
class FakeSignalSource:
    """시나리오대로 SignalSnapshot 을 뱉는다."""
    def __init__(self, scenario: list[SignalSnapshot]) -> None:
        self._scenario = iter(scenario)

    async def snapshot(self) -> SignalSnapshot:
        return next(self._scenario, _STEADY_STATE)


async def test_policies_do_not_oscillate():
    engine = RemediationEngine(signals=FakeSignalSource(_OSCILLATION_SCENARIO), ...)

    for _ in range(100):                      # 100 사이클을 빠르게 돌린다
        await engine.tick()

    weights = [h.after_config for h in audit.records()]
    assert len(audit.records()) <= 6          # 시간당 예산이 지켜졌는가
    assert not _is_oscillating(weights)       # A→B→A→B 반복이 없는가
```

시간을 주입 가능하게 설계해두면 (Phase 6 의 `clock` 파라미터 패턴)
**100 사이클을 1초에 돌릴 수 있다.**

### 6.2 재현할 시나리오

| 시나리오 | 확인할 것 |
|---|---|
| 정책 A 가 weight 를 줄이고 정책 B 가 늘림 | 진동하지 않는가 (mutex/우선순위) |
| 같은 정책 연속 발화 | cooldown + 예산이 막는가 |
| 모든 deployment 가 나쁨 | 전부 0 이 되지 않는가 |
| 자동 적용 직후 재발화 | 관찰 기간 중 추가 변경이 막히는가 |
| Kill switch 를 중간에 끔 | 즉시 멈추는가, 진행 중 검증은 어떻게 되는가 |
| 적용 후 프로세스 재기동 | 검증이 이어지는가 (Phase 9 상태 기반 설계 덕분) |

마지막 두 개가 특히 중요하다. **"진행 중이던 자동 변경" 의 처리 규칙을 정해둔다.**
권장: kill switch 는 **새 변경만 막고**, 진행 중 검증/rollback 은 완료시킨다.
(rollback 을 막으면 되돌릴 수 없는 상태로 남는다)

---

## 7. 폐쇄 루프

```text
        ┌──────────────────────────────┐
        ▼                              │
     Observe → Measure → Diagnose → Decide → Control → Validate
        ▲                                                 │
        └─────────────────────────────────────────────────┘
```

Validate 결과가 다시 Observe 로 들어가고,
정책의 성공/실패 이력이 자동화 등급에 반영되는 구조가 최종 형태다.

코드로는 이렇게 된다:

```python
async def tick(self) -> None:
    snapshot = await self._signals.snapshot()            # Observe/Measure (Phase 2,3)
    diagnoses = self._diagnosis.evaluate(snapshot)       # Diagnose      (Phase 3)
    policies = self._match_policies(diagnoses)           # Decide        (Phase 9)
    policies = resolve_conflicts(policies, self._active)
    for policy in policies:
        if policy.level >= AutomationLevel.AUTO_LIMITED:
            await self._apply_automatically(policy)      # Control       (Phase 4 override)
        else:
            await self._create_recommendation(policy)    # Phase 9 경로
    await self._run_due_validations()                    # Validate      (Phase 9 루프)
    self._update_levels()                                # 등급 승격/강등 → 다음 Observe 로
```

**한 함수 안에서 전 Phase 가 만난다.** 이게 이 프로젝트의 최종 형태다.

---

## 8. 실습 과제

1. **진입 조건 체크리스트를 먼저 검증한다.** 못 채우면 여기서 멈춘다
2. `AutomationLevel` + 정책별 등급 저장 (Phase 9 SQLite 확장)
3. Kill switch — 자동 경로 진입점을 **하나로 통합**하고 첫 줄에 검사
4. `_assert_safe_to_apply()` 에 안전장치 전부 모으기 + 각각의 단위 테스트
5. `wilson_lower_bound()` + 승격/강등 판정 함수
6. `AuditLog` (append + fsync) + 동시 쓰기 테스트
7. **Circuit Breaker 연동만 L2 로 올린다** — 가장 안전한 시작점
8. `FakeSignalSource` 로 카오스 테스트 (진동 시나리오)
9. 실제 장애에서 동작 확인 (DoD 1)
10. Kill switch 로 즉시 중단되는지 확인 (DoD 4)
11. 등급이 데이터에 따라 조정된 사례 만들기 (DoD 5)

---

## 9. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| 진입 조건 미달 상태에서 시작 | 잘못된 제안이 자동 적용 | 체크리스트 강제 |
| kill switch 검사 지점 분산 | 새 경로에서 누락 | 진입점 단일화 |
| 승격만 자동, 강등은 수동 | 나빠진 정책이 계속 자동 | 강등 자동화 |
| 작은 표본으로 승격 | 우연을 실력으로 오인 | Wilson score 하한 |
| 정책별 예산만 존재 | 전체 변경량 폭증 | 전역/일일 예산 추가 |
| 우선순위 동점 처리 미정의 | 실행마다 결과가 다름 | 정렬 키에 `id` 추가 |
| 감사 로그에 `flush`/`fsync` 없음 | 크래시 시 기록 유실 | 명시적 fsync |
| 감사 로그를 로테이션하며 삭제 | 설명 책임 상실 | 날짜별 새 파일, 삭제 금지 |
| 감사 파일 쓰기를 이벤트 루프에서 | 블로킹 | `to_thread` + Lock |
| 여러 정책 동시 적용 | 원인 분석 불가 | mutex + 한 번에 하나 |
| kill switch 가 rollback 도 차단 | 되돌릴 수 없는 상태 | 새 변경만 차단 |
| 전부 자동화하려 함 | 사고 | 일치율 낮은 정책은 제외 |

---

## 10. 이 Phase 의 진짜 목표

> 자동화 그 자체가 아니다.
> **모델/서빙/옵션이 바뀌어도, 데이터로 판단하고 안전하게 되돌릴 수 있는 시스템**을 갖는 것이다.

DoD 6번: **"자동 대응 때문에 상황이 악화된 사례가 0건"** 이거나,
발생 후 재발 방지책이 문서화되어 있을 것.

그리고 마지막 문장:

> **자동화를 켜지 않기로 결정하는 것도 정당한 결론이다. 단, 그 결정은 데이터로 내린다.**

Phase 1~9 를 다 만들고 L2 를 한 번도 켜지 않는 것도 성공이다.
그때 남는 것은 "지표로 판단할 수 있는 시스템" 이고, 그게 애초의 목표였다.
