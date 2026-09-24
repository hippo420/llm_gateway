# Phase 4 학습 — 동적 설정 (Dynamic Configuration)

> 설계서: [`../docs/phases/phase-04-dynamic-configuration.md`](../docs/phases/phase-04-dynamic-configuration.md)
> 명세: [`../docs/specs/config-spec.md`](../docs/specs/config-spec.md)
> 선행: Phase 1 의 `ConfigSource` / `ModelRegistry`

Spring Cloud Config + Actuator refresh 를 직접 만드는 Phase 다.
Python 지식으로는 **Redis 비동기 클라이언트**와 **원자적 교체**가 새롭다.

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | 계층형 설정 병합 + 우선순위 | Spring `PropertySource` 순서 | 중 |
| 2 | Pydantic 로 설정 스키마 검증 | `@ConfigurationProperties` + `@Validated` | 하 |
| 3 | **원자적 스냅샷 교체** (불변 + 참조 대입) | `volatile` 참조 / `AtomicReference` | **상** |
| 4 | `redis.asyncio` — 조회 + pub/sub | Lettuce / Redisson | 중 |
| 5 | 파일 mtime 감시 | `WatchService` | 하 |
| 6 | FastAPI 인증 의존성 | Spring Security 필터 | 중 |
| 7 | 민감정보 마스킹 | `SecretStr` / 로그 마스킹 | 하 |

---

## 1. Source of Truth 3계층

```text
우선순위:  요청 파라미터  >  Redis override  >  YAML base
```

| 계층 | 저장소 | 성격 |
|---|---|---|
| Base | `config/gateway.yaml` (Git) | 정본. 리뷰/이력 대상 |
| Override | Redis `gateway:config:override` | 운영 중 **임시** 변경. TTL 가능 |
| Runtime | 요청 파라미터 | 단건 |

**Redis override 는 항상 임시다.** 영구 변경은 YAML 에 반영하고 커밋한다.
이 원칙을 코드로 강제하려면 override 에 TTL 을 두고, `GET /admin/config` 에 만료 시각을 함께 노출한다.

---

## 2. 계층 병합 구현

### 2.1 dict 깊은 병합

Python 의 `{**a, **b}` 는 **얕은 병합**이다. 중첩 dict 는 통째로 덮인다.

```python
base = {"timeout": {"connect": 5, "read": 60}}
override = {"timeout": {"read": 120}}

{**base, **override}
# → {"timeout": {"read": 120}}   ← connect 가 사라졌다!
```

설정 병합에는 재귀 병합이 필요하다.

```python
def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)                      # 원본을 건드리지 않는다
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
```

`dict(base)` 로 복사하는 것이 중요하다. 안 하면 원본 YAML 스냅샷이 오염된다.
리스트 병합 정책(교체 vs 추가)은 **교체로 통일**한다 — 추가는 되돌리기가 어렵다.

### 2.2 ConfigSource 계층

```python
class ConfigSource(ABC):
    @abstractmethod
    async def load_raw(self) -> dict[str, Any]: ...


class LayeredConfigSource(ConfigSource):
    def __init__(self, base: ConfigSource, override: ConfigSource | None) -> None: ...

    async def load_raw(self) -> dict[str, Any]:
        raw = await self._base.load_raw()
        if self._override is not None:
            raw = deep_merge(raw, await self._override.load_raw())
        return raw
```

**Redis 가 죽어도 base 로 서비스가 계속되어야 한다.**

```python
try:
    override = await self._override.load_raw()
except RedisError:
    log.warning("redis override unavailable, using base only")
    override = {}
```

---

## 3. 검증 — 잘못된 설정으로 서비스가 죽으면 안 된다

```python
class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")     # ← 오타를 잡는 핵심 설정

    id: str
    adapter: str
    endpoint: AnyHttpUrl
    upstream_model: str
    enabled: bool = True
    weight: int = Field(default=100, ge=0, le=100)
    timeout: TimeoutConfig = TimeoutConfig()
    options: dict[str, Any] = Field(default_factory=dict)
```

`extra="forbid"` 를 쓰는 이유: `endpont:` 같은 오타가 **조용히 무시되면 안 된다.**
Phase 1 의 요청 스키마는 `extra="ignore"` 였다 — 외부에서 오는 것은 관대하게,
**내 설정 파일은 엄격하게.** 방향이 반대다.

```python
try:
    config = GatewayConfig.model_validate(raw)
except ValidationError as exc:
    log.error("config validation failed", extra={"errors": exc.errors()})
    CONFIG_RELOAD.labels(result="invalid").inc()
    return None            # 기존 스냅샷 유지
```

**기동 시와 reload 시의 처리가 다르다:**

| 시점 | 검증 실패 시 |
|---|---|
| 기동 (lifespan) | **프로세스 기동 중단** (Phase 1 결정) |
| reload | **기존 스냅샷 유지** + 에러 로그 + metric |

---

## 4. 원자적 스냅샷 교체 (이 Phase 의 핵심)

### 4.1 왜 부분 갱신이 위험한가

```python
# X: 다른 코루틴이 중간 상태를 볼 수 있다
self._models.clear()
self._models.update(new_models)      # ← 이 사이에 요청이 들어오면 빈 registry
```

```python
# O: 완성된 객체를 만들고 참조만 바꾼다
new_snapshot = RegistrySnapshot(models=..., version=...)
self._snapshot = new_snapshot        # 참조 대입 = 원자적
```

### 4.2 왜 이게 안전한가

common.md 3.5 참고: 이벤트 루프는 단일 스레드고, `await` 가 없는 구간은 원자적이다.
**참조 대입 한 줄에는 `await` 가 없으므로 중간 상태가 관측되지 않는다.**

Java 의 `AtomicReference.set()` 또는 `volatile` 필드에 해당하지만,
Python 에서는 별도 도구 없이 그냥 대입하면 된다.

### 4.3 읽는 쪽도 한 번만 읽어야 한다

```python
def resolve(self, model: str) -> ModelDeployment:
    snapshot = self._snapshot            # ← 지역 변수로 한 번만 읽는다
    deployments = snapshot.models[model]
    return snapshot.defaults.apply(deployments[0])   # 같은 snapshot 을 계속 사용
```

`self._snapshot` 을 여러 번 읽으면 그 사이에 교체되어 **서로 다른 버전이 섞인다.**
Java 에서 `volatile` 필드를 지역 변수에 복사해 쓰는 것과 같은 관용구다.

### 4.4 스냅샷은 불변으로

```python
@dataclass(frozen=True)
class RegistrySnapshot:
    version: int
    models: Mapping[str, tuple[ModelDeployment, ...]]   # dict/list 가 아니라 불변 타입
    loaded_at: datetime
```

`frozen=True` 는 **얕은 불변**이다. 안의 dict 는 여전히 바뀔 수 있다.
그래서 `tuple` 을 쓰고, dict 는 `MappingProxyType` 으로 감싸거나 "수정 금지" 를 규약으로 둔다.

```python
from types import MappingProxyType
models = MappingProxyType(dict(built))     # 읽기 전용 뷰 (Collections.unmodifiableMap)
```

---

## 5. Redis (redis-py asyncio)

```powershell
pip install "redis>=5.0"
```

### 5.1 기본 사용

```python
import redis.asyncio as redis            # 동기 API 가 아니라 asyncio 버전

client = redis.from_url(
    settings.redis_url,
    decode_responses=True,               # bytes 대신 str 로 받는다
)

raw = await client.get("gateway:config:override")
data = json.loads(raw) if raw else {}
```

| 함정 | 내용 |
|---|---|
| `import redis` (동기) | 이벤트 루프를 블로킹한다. **`redis.asyncio` 를 쓴다** |
| `decode_responses=False` 기본 | 모든 값이 `bytes` 로 온다. `b"..."` 에 당황하게 됨 |
| 연결 실패 처리 | `redis.exceptions.RedisError` 를 잡아 base 로 폴백 |
| 종료 | lifespan 에서 `await client.aclose()` |

### 5.2 Pub/Sub — 변경 즉시 반영

```python
async def subscribe_config_changes(client, on_change) -> None:
    pubsub = client.pubsub()
    await pubsub.subscribe("gateway:config:changed")
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":       # subscribe 확인 메시지 등은 건너뛴다
                continue
            await on_change()
    finally:
        await pubsub.aclose()
```

- `pubsub.listen()` 은 async generator 다 — Phase 1 에서 배운 그 패턴.
- **`message["type"]` 검사가 필수다.** 구독 확인 메시지(`subscribe`)가 먼저 온다.
- Pub/Sub 은 **전달 보장이 없다.** 메시지를 놓칠 수 있으므로 **mtime polling 을 함께 둔다.**
  (설계서가 두 트리거를 모두 나열한 이유)

### 5.3 백그라운드 태스크로

Phase 3 의 루프 패턴과 동일하다. lifespan 에서 `create_task` → shutdown 에서 `cancel`.
pub/sub 구독이 끊기면 재연결하는 루프를 감싼다.

```python
while True:
    try:
        await subscribe_config_changes(client, on_change)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("pubsub disconnected, retrying in 5s")
        await asyncio.sleep(5)
```

---

## 6. 파일 감시 (mtime polling)

```python
async def watch_file(path: Path, on_change, interval: float = 5.0) -> None:
    last_mtime = path.stat().st_mtime
    while True:
        await asyncio.sleep(interval)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue                       # 편집기가 잠깐 파일을 지우는 경우가 있다
        if mtime != last_mtime:
            last_mtime = mtime
            await on_change()
```

| 항목 | 내용 |
|---|---|
| `Path.stat().st_mtime` | 수정 시각 (float 초) |
| `!=` 비교 | `>` 가 아니다. 되돌리기(git checkout)도 감지해야 한다 |
| 파일이 잠시 사라짐 | 에디터의 원자적 저장 패턴. `FileNotFoundError` 무시 |
| **부분 저장 파일** | 저장 도중 읽으면 YAML 파싱 실패 → **검증 실패 처리로 자연스럽게 흡수된다** |

> `watchfiles` 라이브러리(OS 네이티브 이벤트)도 있지만, 5초 폴링으로 DoD(5초 내 반영)를 만족한다.
> **의존성을 늘리지 않는다.**

`settings.config_reload_sec` 이 0 이면 감시를 시작하지 않는다 (이미 스켈레톤에 준비됨).

---

## 7. Admin API + 인증

### 7.1 API 키 검증 의존성

```python
_api_key_header = APIKeyHeader(name="X-Gateway-Key", auto_error=False)

async def require_admin(
    key: str | None = Security(_api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.auth_enabled:
        return                                  # 키가 비어 있으면 인증 비활성 (개발용)
    if key is None or not secrets.compare_digest(key, settings.api_key):
        raise UnauthorizedError("invalid admin key")
```

**`secrets.compare_digest` 를 쓰는 이유:** 일반 `==` 는 타이밍 공격에 취약하다.
Java 의 `MessageDigest.isEqual` 에 해당한다.

### 7.2 라우터 전체에 적용

```python
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
```

`dependencies=[...]` 는 반환값을 쓰지 않는 의존성이다 — 순수하게 **가드** 역할.
Spring Security 의 `.antMatchers("/admin/**").authenticated()` 에 해당한다.

### 7.3 엔드포인트

```http
GET    /admin/config                     # effective config (마스킹 적용)
GET    /admin/config/sources             # 계층별 원본
POST   /admin/config/reload              # 강제 reload
PUT    /admin/deployments/{id}           # weight/enabled override
DELETE /admin/deployments/{id}/override
```

**`GET /admin/config` 가 가장 중요하다.**
"지금 실제로 뭐가 적용 중인지" 를 모르면 운영이 불가능하다 (설계서 원칙 2번).

### 7.4 마스킹

```python
class RedactedStr(str):
    ...

def mask(value: str) -> str:
    return value[:2] + "*" * 6 if value else ""
```

API 키, Redis URL 의 비밀번호 등은 응답에서 가린다.
Pydantic 의 `SecretStr` 을 쓰면 `repr` / `model_dump()` 에서 자동으로 `**********` 이 된다.

```python
api_key: SecretStr = SecretStr("")
...
settings.api_key.get_secret_value()      # 실제 값이 필요할 때만 명시적으로
```

---

## 8. 감사 로그의 시작

override 변경 시 **누가 / 언제 / 왜** 를 기록한다. Phase 9~10 감사 로그의 전신이다.

```python
@dataclass(frozen=True)
class ConfigChangeRecord:
    changed_at: datetime
    actor: str                    # API 키 식별자 또는 "system"
    target: str                   # deployment_id
    before: dict[str, Any]
    after: dict[str, Any]
    reason: str | None
```

지금은 로그 파일로 충분하다. Phase 9 에서 영속 저장소로 승격된다.
**`before` 를 기록해두는 것이 핵심이다.** 없으면 되돌릴 수 없다.

---

## 9. 메트릭

```python
CONFIG_RELOAD = Counter("llm_gateway_config_reload_total", "", ["result"])   # success|invalid|error
CONFIG_VERSION = Gauge("llm_gateway_config_version", "현재 적용된 설정 버전")
```

`config_version` 은 Gauge 다 — 증가만 하는 게 아니라 값 자체가 의미다.
Grafana 에서 "설정이 바뀐 시점" 과 지표 변화를 겹쳐 보면 원인 분석이 쉬워진다.

---

## 10. 실습 과제

1. `deep_merge()` 함수 + 단위 테스트 (중첩 dict, 리스트, None 케이스)
2. `GatewayConfig` Pydantic 모델 작성 → 일부러 오타 넣고 `extra="forbid"` 가 잡는지 확인
3. 원자적 교체 구현 → **테스트: reload 중에 100개 요청을 동시에 던져 전부 성공하는지**

```python
results = await asyncio.gather(*[call() for _ in range(100)], return_exceptions=True)
assert not any(isinstance(r, Exception) for r in results)
```

4. mtime watcher → 파일 저장 후 5초 내 반영 확인 (DoD 1)
5. **깨진 YAML 을 저장하고 서비스가 계속 도는지 확인** (DoD 3) — 이게 가장 중요한 테스트
6. Redis 기동 → override 로 deployment disable → 즉시 트래픽 차단 확인 (DoD 2)
7. Redis 를 죽여도 서비스가 base 로 계속 도는지 확인
8. `GET /admin/config` 로 effective config 확인 (DoD 4)

---

## 11. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| `{**a, **b}` 로 중첩 설정 병합 | 하위 키 소실 | `deep_merge` |
| 병합 시 원본 dict 수정 | 스냅샷 오염 | `dict(base)` 로 복사 |
| 부분 갱신 (`clear()` + `update()`) | 중간 상태 노출 | 새 스냅샷 + 참조 대입 |
| `self._snapshot` 을 여러 번 읽음 | 버전 혼합 | 지역 변수로 한 번만 |
| 동기 `redis` 사용 | 이벤트 루프 블로킹 | `redis.asyncio` |
| `decode_responses` 미설정 | 모든 값이 bytes | `decode_responses=True` |
| pub/sub 만 의존 | 메시지 유실 시 반영 안 됨 | mtime polling 병행 |
| pub/sub 메시지 type 미검사 | 구독 확인 메시지로 reload | `type != "message"` 건너뛰기 |
| Redis 장애 시 기동/서비스 중단 | 가용성 하락 | base 폴백 |
| reload 실패 시 빈 설정 적용 | 전체 장애 | 기존 스냅샷 유지 |
| API 키 `==` 비교 | 타이밍 공격 | `secrets.compare_digest` |
| override 를 영구 설정처럼 사용 | 재기동 시 유실, 이력 없음 | TTL + YAML 커밋 원칙 |

---

## 12. 다음 Phase 진입 조건

**Registry 가 논리 모델당 복수 deployment 를 반환할 수 있어야 한다.**

```python
def candidates(self, model: str) -> list[ModelDeployment]:
    """enabled 인 deployment 를 weight 순으로. Phase 5 Router 의 입력."""
```

Phase 5 는 이 목록 위에서 고른다. `resolve()`(하나 반환)만 있으면 Phase 5 에서 registry 를 다시 짜야 한다.
