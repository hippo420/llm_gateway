# Phase 8 학습 — 품질 평가 (Quality Evaluation)

> 설계서: [`../docs/phases/phase-08-quality-evaluation.md`](../docs/phases/phase-08-quality-evaluation.md)
> 선행: Phase 7(비교 틀), Phase 2(성능 지표)

여기서 처음으로 **Python 데이터 생태계**(numpy, 임베딩)를 만난다.
동시에 이 Phase 는 코드보다 **평가 설계**가 어렵다. 자동 채점을 믿을 수 있는지가 관건이다.

---

## 0. 이 Phase 에서 새로 필요한 지식

| # | 주제 | Java 대응 | 난이도 |
|---|---|---|---|
| 1 | JSONL 파일 처리 + 제너레이터 | 스트림 파일 읽기 | 하 |
| 2 | numpy 기초 (벡터, 내적, 노름) | — | 중 |
| 3 | 코사인 유사도 / 임베딩 | — | 중 |
| 4 | LLM-as-Judge 프롬프트 + **JSON 응답 안정화** | — | **상** |
| 5 | 상관계수 (Pearson / Spearman) | — | 중 |
| 6 | 배치 실행 (`gather` + `Semaphore`) | `ExecutorService` | 하 |
| 7 | CPU 작업을 이벤트 루프 밖으로 (`to_thread`) | 별도 스레드풀 | 중 |
| 8 | 결과 영속화 (JSONL / SQLite) | JPA / 파일 | 중 |
| 9 | 재현성 (seed, 프롬프트 버전 고정) | — | 중 |

---

## 1. 문제 정의 — "최고의 모델" 을 찾는 게 아니다

```text
Model A:  Latency 2.1s | TPS 50 | Quality 82
Model B:  Latency 4.8s | TPS 31 | Quality 94
```

어느 쪽이 옳은가? **요청 종류에 따라 다르다.**

이 Phase 의 산출물은 순위표가 아니라 **`request_type × model` 적합도 표**다.

| request_type | 권장 deployment | 근거 |
|---|---|---|
| `simple_qa` | qwen-7b@ollama | 품질 차이 3점, 지연 절반 |
| `report_analysis` | qwen-14b@vllm | Faithfulness 0.79 → 0.91 |
| `news_summary` | ... | ... |

이 표가 Phase 9 라우팅 정책의 입력이 된다 (DoD 5).

---

## 2. RAG 평가 항목과 책임 소재

| 항목 | 의미 | **문제가 있으면 누구 책임인가** |
|---|---|---|
| Faithfulness | 답변이 제공된 context 에 근거하는가 | 모델 |
| Answer Relevance | 질문에 답했는가 | 모델 |
| **Context Relevance** | 검색된 context 가 질문에 적절한가 | **Spring 쪽 RAG 파이프라인 (리트리버)** |
| Citation Accuracy | 인용 출처가 실제 근거인가 | 모델 |
| Hallucination | 사실이 아닌 생성 | 모델 (**금융 도메인에서 가장 치명적**) |

**Context Relevance 를 모델 평가에 섞으면 안 된다.**
리트리버가 엉뚱한 문서를 가져왔는데 모델만 계속 바꾸다 끝난다.
평가 리포트에서 이 항목을 별도 섹션으로 분리한다.

---

## 3. 벤치마크 데이터셋

### 3.1 JSONL — 한 줄에 JSON 하나

```text
datasets/
├── simple-qa.jsonl          30~50건
├── report-analysis.jsonl    30건
└── news-summary.jsonl       30건
```

```json
{"id":"rpt-001","request_type":"report_analysis","question":"...","context":["..."],"reference_answer":"...","must_include":["영업이익","2025"],"must_not_include":[]}
```

### 3.2 읽기 — 제너레이터로

```python
def load_dataset(path: Path) -> Iterator[EvalItem]:
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                yield EvalItem.model_validate_json(line)
            except ValidationError as exc:
                raise ValueError(f"{path}:{line_no} invalid") from exc
```

| 요소 | 설명 |
|---|---|
| `yield` (동기 제너레이터) | 파일 전체를 메모리에 올리지 않는다. Java 의 `Stream<T>` |
| `enumerate(f, start=1)` | 인덱스와 함께 순회. **줄 번호를 에러에 넣으면 디버깅이 쉬워진다** |
| `encoding="utf-8"` | Windows 필수 (Phase 1 과 같은 이유) |
| `model_validate_json` | Pydantic 이 JSON 문자열을 직접 검증/파싱 |

### 3.3 데이터셋은 **실제 서비스 질의에서 추출**한다

만들어낸 질문으로 평가하면 실제 성능과 무관한 결과가 나온다.
Spring 로그에서 실제 질의를 뽑아 익명화해 쓴다.

**고정 데이터셋이 없으면 비교 자체가 불가능하다.** 이 Phase 는 여기서 시작한다.

---

## 4. 평가 계층 — 싼 것부터

| 계층 | 방법 | 비용 | 용도 |
|---|---|---|---|
| L1 | 규칙 (포함어, 형식, 길이, 숫자 존재) | 매우 낮음 | 매 실행 |
| L2 | 임베딩 유사도 (reference 대비) | 낮음 | 매 실행 |
| L3 | LLM-as-Judge | 높음 | 주요 비교 시 |
| L4 | 사람 평가 | 매우 높음 | **judge 검증용** 샘플 |

**L1 부터 만든다.** LLM judge 보다 싸고 안정적이고, 결과가 재현된다.

### 4.1 L1 규칙 스코어러

```python
class RuleScorer:
    def score(self, item: EvalItem, answer: str) -> ScoreResult:
        checks: dict[str, bool] = {}

        for token in item.must_include:
            checks[f"include:{token}"] = token in answer
        for token in item.must_not_include:
            checks[f"exclude:{token}"] = token not in answer

        checks["nonempty"] = bool(answer.strip())
        checks["has_number"] = bool(re.search(r"\d", answer)) if item.expects_number else True

        passed = sum(checks.values())                  # True == 1 로 계산된다
        return ScoreResult(score=passed / len(checks), detail=checks)
```

- `token in answer` = `String.contains()`
- `sum(checks.values())` — Python 에서 `bool` 은 `int` 의 서브클래스라 `True + True == 2` 다.
  Java 개발자에게는 낯설지만 관용적인 표현이다.
- 정규식은 `re` 모듈. `r"..."` 는 raw string (백슬래시를 그대로).

**금융 도메인 팁:** `must_not_include` 에 "추정", "아마도" 같은 표현이나
context 에 없는 연도/숫자를 넣으면 hallucination 을 싸게 잡을 수 있다.

---

## 5. L2 — 임베딩 유사도

### 5.1 numpy 최소 지식

```powershell
pip install numpy
```

```python
import numpy as np

a = np.array([0.1, 0.2, 0.3], dtype=np.float32)   # 벡터
a.shape          # (3,)   차원
a @ b            # 내적 (dot product)
np.linalg.norm(a)  # L2 노름 (크기)
```

| numpy | Java 로 치면 |
|---|---|
| `np.array([...])` | `float[]` (단, 연산이 벡터화됨) |
| `a @ b` | `for` 루프로 곱해 더한 값 |
| `a.shape` | `arr.length` (다차원이면 튜플) |
| `dtype` | 원소 타입. `float32` 로 두면 메모리 절반 |

**루프를 돌지 않는 것이 numpy 의 요점이다.** `sum(x*y for x,y in zip(a,b))` 보다 수십 배 빠르다.

### 5.2 코사인 유사도

```python
def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-12:
        return 0.0                       # 영벡터 방어
    return float(a @ b / denom)
```

값 범위는 -1 ~ 1 이고, 임베딩에서는 보통 0 ~ 1 이다.

**의미:** "두 문장이 얼마나 비슷한 방향을 가리키는가".
길이(크기)를 무시하므로 문장 길이 차이에 덜 민감하다.

### 5.3 임베딩 모델

이 프로젝트는 **Spring 쪽에서 이미 bge-m3 를 쓰고 있다** (설계서 5장 체크리스트).
같은 모델을 재사용해야 점수가 일관된다.

| 방법 | 장단점 |
|---|---|
| Ollama 임베딩 API (`/api/embeddings`) | 이미 있는 인프라 재사용. **권장** |
| `sentence-transformers` 로 직접 로드 | GPU 메모리를 또 먹는다. 12GB 환경에서 위험 |

```python
async def embed(self, text: str) -> np.ndarray:
    resp = await self._client.post("/api/embeddings", json={"model": "bge-m3", "prompt": text})
    return np.array(resp.json()["embedding"], dtype=np.float32)
```

**임베딩 계산이 CPU 를 오래 먹는 경우** (로컬 모델 사용 시) 이벤트 루프를 막는다:

```python
vec = await asyncio.to_thread(model.encode, text)     # 별도 스레드로
```

---

## 6. L3 — LLM-as-Judge

### 6.1 3가지 철칙 (설계서 3.3)

1. **judge 모델은 평가 대상과 다른 모델**을 쓴다 (self-preference bias — 모델은 자기 출력을 후하게 준다)
2. **judge 프롬프트/버전을 고정하고 기록**한다. 바뀌면 과거 점수와 비교 불가
3. 점수는 절대값이 아니라 **동일 judge 하에서의 상대 비교**로만 쓴다

### 6.2 JSON 응답을 안정적으로 받기 — 실무의 절반

LLM 은 요청한 형식을 자주 어긴다. ` ```json ` 으로 감싸거나 설명을 덧붙인다.

```python
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_judge_output(text: str) -> JudgeScore | None:
    match = _JSON_BLOCK.search(text)      # 첫 { 부터 마지막 } 까지
    if match is None:
        return None
    try:
        return JudgeScore.model_validate_json(match.group())
    except ValidationError:
        return None                        # 파싱 실패는 "점수 없음". 0 점이 아니다
```

| 대응책 | 내용 |
|---|---|
| 정규식으로 JSON 블록 추출 | ` ``` ` 래핑, 앞뒤 설명 제거 |
| Pydantic 으로 검증 | 필드 누락/타입 오류를 잡는다 |
| **파싱 실패 = `None`** | 0 점으로 처리하면 평균이 왜곡된다 |
| `temperature=0` | 재현성 확보 |
| 실패 시 1회 재시도 | 그 이상은 비용 낭비 |

`re.DOTALL` 은 `.` 이 개행에도 매칭되게 한다 (Java 의 `Pattern.DOTALL`).

### 6.3 프롬프트 버전 관리

```python
JUDGE_PROMPT_V3 = """..."""

JUDGE_VERSION = "judge-v3"
```

결과에 반드시 함께 저장한다:

```json
"judge": { "model": "gemini-2.x", "prompt_version": "judge-v3" }
```

**프롬프트를 고치면 버전을 올린다.** 같은 버전으로 다른 프롬프트를 쓰면 데이터가 오염된다.

---

## 7. L4 — 사람 평가로 judge 를 검증

> **L4 로 L3 를 검증하지 않으면 judge 를 믿을 수 없다.** (설계서 3.2)

최소 20건을 사람이 채점하고 judge 점수와의 상관을 본다.

```python
def spearman(xs: list[float], ys: list[float]) -> float:
    """순위 상관. 값의 크기가 아니라 순서가 일치하는지를 본다."""
    rx, ry = _rank(xs), _rank(ys)
    return pearson(rx, ry)
```

`statistics.correlation(xs, ys)` (Python 3.10+) 로 Pearson 을 바로 구할 수 있고,
`statistics.correlation(xs, ys, method="ranked")` (3.12+) 로 Spearman 도 가능하다.
없으면 `scipy.stats.spearmanr` 를 쓰거나 위처럼 직접 구현한다.

| 상관계수 | 해석 |
|---|---|
| > 0.7 | judge 를 쓸 만하다 |
| 0.4 ~ 0.7 | 참고용. 큰 차이에만 사용 |
| < 0.4 | **judge 를 폐기한다** |

DoD 3번이 "상관이 확인되었다 **(또는 낮아서 judge 를 폐기했다)**" 인 이유다.
폐기도 정당한 결론이다.

**Spearman 을 쓰는 이유:** judge 가 전반적으로 후하거나 짜도(1~2점 오프셋),
순서만 맞으면 비교에는 쓸 수 있다.

---

## 8. Runner — 배치 실행

```python
async def run(
    self, dataset: list[EvalItem], deployment_id: str, concurrency: int = 4
) -> list[EvalResult]:
    sem = asyncio.Semaphore(concurrency)

    async def one(item: EvalItem) -> EvalResult:
        async with sem:
            answer, perf = await self._ask(item, deployment_id)
            scores = await self._score_all(item, answer)
            return EvalResult(item_id=item.id, answer=answer, scores=scores, performance=perf)

    return await asyncio.gather(*[one(i) for i in dataset], return_exceptions=False)
```

Phase 7 의 벤치마크 스크립트와 같은 패턴이다.

**`return_exceptions` 선택:**

| 값 | 동작 |
|---|---|
| `False` (기본) | 하나라도 실패하면 전체 중단. **나머지 태스크는 취소되지 않고 남는다** (주의) |
| `True` | 예외를 결과 리스트에 담아 반환. 부분 실패를 허용 |

평가는 **일부 실패해도 나머지 결과가 유용**하므로 `True` 를 쓰고, 실패 건수를 리포트에 남기는 쪽이 낫다.

### 성능 지표를 함께 수집한다

```python
@dataclass
class EvalResult:
    item_id: str
    answer: str
    scores: dict[str, float | None]
    performance: ChatTimings              # ← 반드시 함께
```

DoD 2번: **"품질 점수와 성능 지표가 한 화면에서 비교된다."**
품질만 따로 저장하면 나중에 합칠 수 없다.

---

## 9. 결과 저장

```json
{
  "run_id": "eval-20260825-01",
  "deployment_id": "qwen-7b@vllm",
  "dataset": "report-analysis",
  "quality": { "faithfulness": 0.91, "relevance": 0.88, "citation": 0.79, "overall": 86 },
  "performance": { "ttft_p95": 0.8, "output_tps": 58, "latency_p95": 4.1 },
  "judge": { "model": "gemini-2.x", "prompt_version": "judge-v3" }
}
```

| 저장 방식 | 언제 |
|---|---|
| JSONL 파일 (`evaluation/runs/*.jsonl`) | 시작점. git 으로 이력 관리 가능 |
| SQLite (`sqlite3` 표준 라이브러리) | 쿼리가 필요해지면 |

```python
import sqlite3          # 표준 라이브러리. 별도 설치 불필요

conn = sqlite3.connect("evaluation/results.db")
conn.execute("INSERT INTO runs (run_id, deployment_id, payload) VALUES (?, ?, ?)",
             (run_id, dep_id, json.dumps(payload, ensure_ascii=False)))
conn.commit()
```

> `sqlite3` 는 **동기 라이브러리**다. 배치 스크립트에서는 문제없지만,
> FastAPI 요청 경로에서 쓰면 이벤트 루프를 막는다. `aiosqlite` 를 쓰거나 `to_thread` 로 감싼다.

---

## 10. 메트릭 노출

```python
QUALITY = Gauge("llm_gateway_quality_score", "", ["deployment_id", "dataset"])
QUALITY.labels(deployment_id=dep, dataset=ds).set(overall)
```

**Gauge 인 이유:** 배치로 갱신되는 현재 값이지 누적이 아니다.
평가 실행 시에만 갱신되므로 Grafana 에서 `last_over_time(...[1d])` 로 본다.

---

## 11. 재현성

| 항목 | 고정 방법 |
|---|---|
| 생성 파라미터 | `temperature=0`, `seed` 고정 (`AdapterChatRequest.seed` 필드가 이미 있다) |
| judge 프롬프트 | 버전 문자열로 관리 |
| 데이터셋 | git 커밋. 수정 시 버전 올림 |
| 임베딩 모델 | 모델명 + 버전 기록 |

**seed 를 줘도 완전히 재현되지는 않는다** (GPU 부동소수점 비결정성, 배치 크기 영향).
"거의 재현" 을 목표로 하고, 차이가 나면 **여러 번 실행해 평균**을 쓴다.

---

## 12. 실습 과제

1. `simple-qa.jsonl` 10건을 실제 서비스 질의에서 뽑아 작성
2. `load_dataset()` + 잘못된 줄에서 줄 번호가 나오는지 확인
3. **L1 규칙 스코어러만으로 두 deployment 비교** — 여기까지가 절반이다
4. Ollama 임베딩 API 로 L2 스코어러 → 코사인 유사도
5. runner 로 30건 배치 실행 (Semaphore 4) + 성능 지표 동시 수집
6. 결과를 JSONL 로 저장 → 두 deployment 비교 리포트 출력
7. L3 judge 추가 (다른 모델로) + JSON 파싱 실패율 측정
8. **사람이 20건 채점** → Spearman 상관 계산 (DoD 3)
9. `request_type × deployment` 적합도 표 작성 (DoD 4)
10. 그 표를 Phase 9 정책 입력 형태로 정리 (DoD 5)

---

## 13. 함정 요약

| 함정 | 결과 | 대응 |
|---|---|---|
| L3 부터 시작 | 비용 폭증 + 결과 불안정 | L1 → L2 → L3 순서 |
| judge 로 자기 모델 평가 | self-preference bias | 다른 모델 사용 |
| judge 프롬프트 무버전 변경 | 과거 점수와 비교 불가 | 버전 문자열 |
| 파싱 실패를 0점 처리 | 평균 왜곡 | `None` + 실패율 별도 보고 |
| Context Relevance 를 모델 탓 | 리트리버 문제를 영원히 못 고침 | 항목 분리 |
| 만들어낸 질문으로 평가 | 실제 성능과 무관 | 실제 질의 추출 |
| 품질만 저장 | 성능과 대조 불가 | `EvalResult` 에 성능 포함 |
| 로컬 임베딩 모델 로드 | GPU 12GB 압박 | Ollama 임베딩 API 재사용 |
| CPU 작업을 이벤트 루프에서 | 서버 정지 | `asyncio.to_thread` |
| `sqlite3` 를 요청 경로에서 | 블로킹 | 배치에서만, 또는 `aiosqlite` |
| Semaphore 없는 배치 | judge API rate limit | `asyncio.Semaphore` |
| 영벡터 코사인 | `ZeroDivisionError` / NaN | denom guard |

---

## 14. 이 Phase 의 결론 형태

> 이 Phase 의 산출물은 "최고의 모델" 이 아니라 **`request_type × model` 적합도 표**다.

그리고 그 표는 Phase 9 에서 이런 정책이 된다:

```text
IF request_type == "report_analysis" AND quality_score(qwen-7b) < 80
THEN route to qwen-14b
```

품질 점수가 라우팅의 입력이 되는 순간, 이 프로젝트의 "성능과 품질을 함께 최적화" 라는 목표가 성립한다.
