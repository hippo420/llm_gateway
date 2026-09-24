# Phase 9 학습 — 동적 정책 (Human-in-the-loop)

> 설계서: [`../docs/phases/phase-09-dynamic-policy.md`](../docs/phases/phase-09-dynamic-policy.md)
> 선행: Phase 3(Rule), Phase 4(Config override), Phase 2(지표)

새로운 기술 스택이 거의 없는 Phase 다. **기존 조각을 이어 붙이는 설계**가 전부다.
Python 으로는 **상태를 갖는 워크플로**와 **영속화**가 새롭다.

```text
Metrics → Diagnosis → Recommendation → Human Approval → Apply → Validate → Metrics
```

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | Phase 3 Rule 자료구조 **재사용** (중복 구현 금지) | — | 중 |
| 2 | Action 과 **역연산** 설계 | Command 패턴 + undo | **상** |
| 3 | 상태 전이 워크플로 (Enum + 검증) | 상태 머신 | 중 |
| 4 | 영속화 (SQLite / `aiosqlite`) | JPA | 중 |
| 5 | Guard — flapping 방지 | Rate limiter | **상** |
| 6 | 검증 루프 + 자동 rollback (지연 실행) | `@Scheduled` + 상태 | 중 |
| 7 | 알림 연동 (Slack webhook) | RestTemplate | 하 |
| 8 | 동시성 제어 (`asyncio.Lock`) | `synchronized` | 중 |

---

## 1. Phase 3 자료구조를 그대로 쓴다

```python
@dataclass(frozen=True)
class Policy:
    id: str
    trigger: Rule                     # ← Phase 3 의 Rule 그대로
    actions: tuple[Action, ...]
    guard: Guard
    validation: Validation
```

**설계서 체크리스트 2번: "Phase 3 Rule 재사용 확인 (중복 구현 금지)".**

Phase 3 에서 규칙을 `if` 문이 아니라 데이터로 만든 이유가 여기서 회수된다.
만약 Phase 3 에서 `if ttft > 1.5 and queue > 5:` 로 짰다면, 지금 전부 다시 짜야 한다.

`trigger` 가 `Rule` 타입이므로 진단 엔진의 평가 결과(`RuleResult`)를 그대로 정책 매칭에 쓸 수 있다.

---

## 2. Action — 역연산이 있는 것만 자동화 대상

```python
class ActionType(str, Enum):
    ADJUST_WEIGHT = "adjust_weight"
    DISABLE_DEPLOYMENT = "disable_deployment"
    SET_TIMEOUT = "set_timeout"
    LIMIT_CONCURRENCY = "limit_concurrency"
    SWITCH_FALLBACK = "switch_fallback"


@dataclass(frozen=True)
class Action:
    type: ActionType
    target: str                       # deployment_id
    delta: int | None = None
    value: Any | None = None
```

### 2.1 핵심 규칙

> **모든 action 은 역연산이 정의되어 있어야 한다.**
> 되돌릴 수 없는 action 은 자동화 대상이 아니다.

역연산을 만드는 가장 안전한 방법은 **delta 를 뒤집는 것이 아니라 이전 값을 저장하는 것**이다.

```python
@dataclass(frozen=True)
class AppliedAction:
    action: Action
    before: dict[str, Any]            # 적용 직전의 실제 값
    after: dict[str, Any]
```

```python
# X: delta 를 뒤집는 방식 — 그 사이 다른 변경이 있으면 어긋난다
weight += 30    →   weight -= 30

# O: 이전 값 복원
before = {"weight": 100}    →    rollback: weight = 100
```

Phase 4 의 `ConfigChangeRecord.before` 를 만들어둔 이유가 이것이다.

### 2.2 적용은 Phase 4 override 로

```python
async def apply(self, action: Action) -> AppliedAction:
    before = self._config.effective_deployment(action.target)
    patch = self._to_patch(action, before)
    await self._config.put_override(action.target, patch, actor="policy", reason=...)
    after = self._config.effective_deployment(action.target)
    return AppliedAction(action=action, before=before, after=after)
```

**새로운 설정 적용 경로를 만들지 않는다.** Phase 4 의 override API 를 그대로 쓴다.
경로가 둘이 되면 "지금 뭐가 적용 중인지" 를 알 수 없게 된다.

---

## 3. Guard — 없는 정책은 만들지 않는다

> 자동 조정의 가장 큰 위험은 **정책끼리 서로 밀어내며 진동(flapping)** 하는 것이다.

```python
@dataclass(frozen=True)
class Guard:
    max_change_per_hour: int = 2
    cooldown_sec: int = 900
    min_weight: int = 10              # 완전히 0으로 만들지 않는다
    max_weight: int = 100
    require_approval: bool = True
    max_concurrent_policies: int = 1
```

### 3.1 시간당 변경 횟수 — `deque` 로 sliding window

```python
class ChangeBudget:
    def __init__(self, limit: int, window_sec: float = 3600) -> None:
        self._events: deque[float] = deque()
        self._limit = limit
        self._window = window_sec

    def allow(self, now: float) -> bool:
        while self._events and now - self._events[0] > self._window:
            self._events.popleft()          # 윈도우 밖의 오래된 기록 제거
        return len(self._events) < self._limit

    def record(self, now: float) -> None:
        self._events.append(now)
```

`deque.popleft()` 는 O(1) 이다 (`list.pop(0)` 은 O(n)).
Phase 3 에서 쓴 `deque(maxlen=N)` 과 달리 여기서는 **시간 기준**이라 maxlen 을 쓸 수 없다.

### 3.2 `min_weight` 가 중요한 이유

```text
정책 A: qwen-7b 에러율 높음 → weight -30
정책 A 재발화 → weight -30
정책 A 재발화 → weight -30
→ weight 10 → 0 → 모든 트래픽 소멸
```

**모든 deployment 의 weight 가 0 이 되는 상태를 만들 수 없어야 한다.**
개별 정책의 `min_weight` 만으로는 부족하고, 적용 직전에 **전역 검증**이 필요하다.

```python
def _validate_global(self, after: RegistrySnapshot) -> None:
    for model, deployments in after.models.items():
        if sum(d.weight for d in deployments if d.enabled) <= 0:
            raise PolicyGuardError(f"all weights would be zero for {model}")
```

이건 Phase 10 의 "하한선" 안전장치의 전신이다. 여기서 만들어두면 그대로 승계된다.

---

## 4. Recommendation 워크플로

### 4.1 상태 전이

```python
class RecommendationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


_ALLOWED: dict[RecommendationStatus, frozenset[RecommendationStatus]] = {
    PENDING: frozenset({APPROVED, REJECTED, EXPIRED}),
    APPROVED: frozenset({APPLIED}),
    APPLIED: frozenset({VALIDATING}),
    VALIDATING: frozenset({SUCCEEDED, ROLLED_BACK}),
    ...
}


def transition(current: RecommendationStatus, to: RecommendationStatus) -> None:
    if to not in _ALLOWED[current]:
        raise InvalidTransitionError(f"{current} -> {to}")
```

**전이 표를 데이터로 두면 잘못된 상태 변경이 런타임에 즉시 잡힌다.**
`if` 문으로 흩어두면 "이미 rejected 인데 approve 가 들어왔을 때" 같은 케이스를 놓친다.

### 4.2 `EXPIRED` 를 잊지 말 것

승인을 기다리다 시간이 지난 제안은 **적용하면 안 된다.**
2시간 전 지표로 만든 제안을 지금 적용하는 것은 위험하다.

```python
if recommendation.created_at < now - timedelta(minutes=30):
    transition(status, EXPIRED)
```

### 4.3 동시성 — `asyncio.Lock`

승인 API 는 사람이 두 번 누를 수 있고, 검증 루프가 동시에 상태를 바꿀 수 있다.

```python
class PolicyStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def approve(self, rec_id: str, actor: str) -> None:
        async with self._lock:                     # 상태 전이 전체를 보호
            rec = await self._get(rec_id)
            transition(rec.status, APPROVED)
            await self._save(rec.with_status(APPROVED, approved_by=actor))
```

common.md 3.5 참고: `await` 를 사이에 낀 read-modify-write 는 원자적이지 않다.
**상태 전이가 정확히 그 패턴이다.** Lock 이 필요한 몇 안 되는 곳이다.

---

## 5. 영속화

### 5.1 왜 메모리로는 안 되는가

- 재기동하면 승인 대기 목록이 사라진다
- **거부 사유가 Phase 10 의 학습 재료다** (설계서 5장) — 잃어버리면 안 된다
- DoD 4번: "최소 10건의 승인/거부 이력이 쌓였다"

### 5.2 SQLite

```python
import aiosqlite          # pip install aiosqlite  (sqlite3 의 async 버전)

async with aiosqlite.connect(db_path) as db:
    await db.execute(
        "INSERT INTO recommendations (id, policy_id, status, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (rec.id, rec.policy_id, rec.status.value, rec.model_dump_json(), rec.created_at.isoformat()),
    )
    await db.commit()
```

| 항목 | 내용 |
|---|---|
| `aiosqlite` | 이벤트 루프를 막지 않는다. **동기 `sqlite3` 를 요청 경로에서 쓰지 않는다** |
| `?` 플레이스홀더 | SQL 인젝션 방지. **f-string 으로 SQL 을 만들지 않는다** |
| `payload` 컬럼에 JSON | 스키마 변경 없이 필드 추가 가능. 검색이 필요한 필드만 별도 컬럼 |
| `datetime` 저장 | ISO 문자열 (UTC). SQLite 에 datetime 타입이 없다 |
| `commit()` | 명시적 호출 필요 |

> ORM(SQLAlchemy, SQLModel)도 있지만 이 규모에서는 과하다. 원시 SQL 로 충분하다.

### 5.3 스키마

Phase 10 과 공통 필드다 (설계서 8장).

```sql
CREATE TABLE IF NOT EXISTS policy_history (
    id                TEXT PRIMARY KEY,
    policy_id         TEXT NOT NULL,
    trigger           TEXT,
    trigger_evidence  TEXT,          -- JSON
    before_config     TEXT,          -- JSON
    after_config      TEXT,          -- JSON
    status            TEXT NOT NULL,
    approved_by       TEXT,
    approved_at       TEXT,
    rejected_reason   TEXT,          -- ← Phase 10 의 학습 재료
    applied_at        TEXT,
    validation_result TEXT,          -- JSON
    rolled_back       INTEGER DEFAULT 0
);
```

**`rejected_reason` 이 이 Phase 의 진짜 산출물이다.**
"이 상황에서 사람은 왜 승인하지 않았는가" 를 모으지 않으면 Phase 10 에서 자동화할 근거가 없다.

---

## 6. 검증 루프 + 자동 rollback

```python
@dataclass(frozen=True)
class Validation:
    observe_sec: int = 600
    success_criteria: tuple[str, ...] = ()      # PromQL 표현식 또는 조건 문자열
    rollback_on_failure: bool = True
```

### 6.1 지연 실행 — `sleep` 이 아니라 상태로

```python
# X: 태스크 안에서 10분 sleep
await asyncio.sleep(600)
await self._validate(rec)          # 이 사이에 재기동하면 검증이 영영 안 된다
```

```python
# O: 상태와 시각을 저장하고, 주기 루프가 처리한다
rec = rec.with_status(VALIDATING, validate_after=now + timedelta(seconds=observe_sec))
...
# 별도 루프 (Phase 3 패턴)
async def validation_loop(store, engine):
    while True:
        for rec in await store.due_for_validation(datetime.now(UTC)):
            await engine.validate_and_finalize(rec)
        await asyncio.sleep(30)
```

**재기동에 견디는 유일한 방법이 이것이다.** 중요한 지연 작업을 `asyncio.sleep` 에 맡기지 않는다.

### 6.2 성공 기준 평가

```python
async def _check_criteria(self, criteria: tuple[str, ...]) -> bool:
    for expr in criteria:                      # 예: "error_rate < 0.03"
        signal, op, threshold = _parse(expr)
        value = await self._prom.query(_QUERIES[signal])
        if value is None:
            return False                        # 판단 불가 = 실패로 본다 (보수적)
        if not _OPS[op](value, threshold):
            return False
    return True
```

Phase 3 의 `Condition` 평가 로직과 같은 구조다. **재사용한다.**

**판단 불가를 "실패" 로 처리하는 것**이 Phase 3 과 다른 점이다.
진단에서는 오탐을 막으려 `insufficient_signal` 로 뒀지만,
검증에서는 "개선됐는지 확인 못 함 = 되돌린다" 가 안전하다.

### 6.3 Rollback

```python
async def rollback(self, rec: Recommendation) -> None:
    for applied in reversed(rec.applied_actions):        # 역순으로 되돌린다
        await self._config.put_override(applied.action.target, applied.before, actor="rollback")
    await self._store.mark_rolled_back(rec.id)
    await self._notify(f"자동 rollback: {rec.policy_id}")
```

`reversed(...)` 로 역순 적용. 여러 action 이 서로 의존할 때 순서가 중요하다.

---

## 7. 알림

```python
async def notify_slack(self, webhook_url: str, rec: Recommendation) -> None:
    payload = {
        "text": f"[{rec.policy_id}] 정책 제안",
        "blocks": [...],
    }
    try:
        resp = await self._client.post(webhook_url, json=payload, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError:
        log.exception("notification failed")     # 알림 실패가 정책 흐름을 막으면 안 된다
```

**알림 실패를 삼키는 것**이 여기서는 옳다. 알림이 안 갔다고 제안 생성이 실패하면 안 된다.
단, 실패는 반드시 로그와 metric 에 남긴다.

webhook URL 은 `.env` 에서 읽고 **로그에 찍지 않는다** (URL 자체가 비밀).

---

## 8. Admin API

```http
GET  /admin/recommendations              # 대기 중 제안
POST /admin/recommendations/{id}/approve
POST /admin/recommendations/{id}/reject  # {reason}   ← reason 필수
GET  /admin/policy-history               # 적용 이력
POST /admin/policy-history/{id}/rollback # 수동 rollback
```

```python
class RejectBody(BaseModel):
    reason: str = Field(min_length=1)       # 빈 사유를 허용하지 않는다
```

**거부 사유를 필수로 강제한다.** 선택으로 두면 아무도 안 쓴다.
Phase 10 의 학습 재료가 사라진다.

인증은 Phase 4 의 `require_admin` 의존성을 그대로 붙인다.

---

## 9. 정책 예시 전체

```python
Policy(
    id="SHIFT_TRAFFIC_ON_ERROR",
    trigger=Rule(
        id="HIGH_ERROR_RATE",
        severity="critical",
        conditions=(Condition("error_rate", Op.GT, 0.05),),
        hypothesis="특정 deployment 에러율 급증",
        actions=(),
    ),
    actions=(
        Action(ActionType.ADJUST_WEIGHT, target="qwen-7b@ollama", delta=-30),
        Action(ActionType.ADJUST_WEIGHT, target="qwen-7b@vllm", delta=+30),
    ),
    guard=Guard(max_change_per_hour=2, cooldown_sec=900, min_weight=10, require_approval=True),
    validation=Validation(
        observe_sec=600,
        success_criteria=("error_rate < 0.03",),
        rollback_on_failure=True,
    ),
)
```

정책 정의를 YAML 로 옮길 수 있게 Pydantic 모델도 함께 만든다 (Phase 4 방식).
그러면 정책 변경이 **코드 배포 없이 설정 커밋**으로 가능해진다.

---

## 10. 실습 과제

1. `Policy` / `Action` / `Guard` / `Validation` 자료구조 — **Phase 3 `Rule` 을 import 해서** 쓴다
2. 상태 전이 표 + `transition()` + 단위 테스트 (잘못된 전이가 막히는지)
3. SQLite 스키마 + `aiosqlite` 저장/조회
4. Recommendation 생성 — Phase 3 진단 결과를 입력으로
5. 승인 API + `asyncio.Lock` → **동시에 두 번 승인 호출** 테스트
6. `ChangeBudget` (deque sliding window) + 테스트
7. 전역 검증 (`all weights zero` 방지) + 테스트
8. 적용 → Phase 4 override 경로 사용 확인
9. 검증 루프 + 자동 rollback → **일부러 개선되지 않게 만들어 rollback 확인** (DoD 3)
10. Slack 알림 (또는 로그 알림)
11. 장애 재현 → 제안 생성 → 승인 → Grafana 에서 트래픽 이동 확인 (DoD 1, 2)
12. **거부 사유를 포함한 이력 10건 쌓기** (DoD 4) → 그중 "잘못된 제안" 분석 (DoD 5)

---

## 11. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| Phase 3 Rule 을 다시 구현 | 규칙이 두 벌 → 불일치 | import 해서 재사용 |
| delta 를 뒤집어 rollback | 중간 변경과 어긋남 | `before` 값 저장/복원 |
| Guard 없는 정책 | flapping, 트래픽 소멸 | Guard 필수화 |
| 전역 검증 누락 | 모든 weight 0 | 적용 전 스냅샷 검증 |
| `asyncio.sleep` 으로 관찰 대기 | 재기동 시 검증 유실 | 상태 + 주기 루프 |
| Lock 없는 상태 전이 | 중복 승인/적용 | `asyncio.Lock` |
| 거부 사유 선택 입력 | Phase 10 근거 소멸 | `min_length=1` 필수 |
| 메모리에만 저장 | 재기동 시 이력 소실 | SQLite |
| 동기 `sqlite3` 를 API 에서 | 이벤트 루프 블로킹 | `aiosqlite` |
| f-string 으로 SQL | 인젝션 | `?` 플레이스홀더 |
| 만료된 제안 적용 | 낡은 지표로 판단 | `EXPIRED` 상태 |
| 알림 실패가 흐름 차단 | 정책 중단 | 예외를 삼키되 기록 |
| 새 설정 적용 경로 신설 | effective config 불명확 | Phase 4 override 재사용 |

---

## 12. 이 Phase 를 끝내는 조건

DoD 5번: **"그중 몇 건이 '사람이 거부한 잘못된 제안' 인지 분석되어 있다."**

> **5번을 못 채우면 Phase 10 으로 가지 않는다.**

이 루프가 안정적으로 돌지 않는데 자동화하면, 사람이 거부했을 제안이 자동으로 적용된다.
Phase 9 의 승인/거부 이력이 Phase 10 자동화 등급의 유일한 근거다.
