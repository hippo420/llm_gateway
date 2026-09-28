# Phase 8 구현 검증 — 2026-09-28

코드/합성 입력 검증 결과다. 실제 모델의 품질·성능 측정이나 실제 사람 채점 결과가 아니다.

| 검사 | 결과 |
|---|---|
| 전체 pytest | 374 passed (10.82s) |
| Phase 8 테스트 | 36 passed |
| Ruff | 통과 |
| mypy | 87 source files, 오류 없음 |
| 새 Python 파일 format 검사 | 통과 |
| Grafana JSON 파싱/UID 중복 검사 | 대시보드 10개 정상 |
| judge 프롬프트 | package-data 선언 및 런타임 resource 로딩 정상 |
| 합성 데이터 | 3종 각 30건, 기준 답변의 L1 규칙 검사 모두 통과 |

전체 테스트 명령:

```powershell
.venv/Scripts/python.exe -m pytest -q --basetemp=.phase04-validation/phase08-full -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check src tests scripts/evaluate.py scripts/generate_evaluation_datasets.py
.venv/Scripts/python.exe -m mypy
```

검증은 FakeAdapter와 HTTP mock을 사용한다. 배치 CLI의 저장, 운영 breaker/retry/fallback 격리,
생성 후 채점 순서, scorer 실패의 누락 처리, 자기 평가 차단, API 인증/에러 계약,
사람 평가 해시 대조·최소 표본·상관 계산, 추천 보류 및 영속 metrics를 확인했다.
테스트에서 사용한 사람 점수는 상관 계산 검증용 fixture이며 실제 사람 검증을 의미하지 않는다.

실제 서비스 질의 3종, 별도 judge/임베딩 모델을 포함한 실제 배치, 사람 채점 최소 20건,
실제 Grafana 렌더링/운영 환경 검증은 후속이다. 실제 근거가 갖춰지기 전 권장 모델은 보류한다.

## 후속 실모델 점검 (2026-09-28)

로컬 Ollama에 실제 연결해 합성 자료로 평가 경로를 실행했다. 실제 서비스 자료 및 사람 채점
검증과 구분한다. 기존 36개 평가 테스트도 재실행해 통과했다.

- Endpoint: `http://[::1]:11434`, Ollama `0.20.5`.
- 대상: `qwen2.5:7b`, `exaone3.5:7.8b`; 별도 judge: `qwen2.5:14b`; 임베딩: `bge-m3`.
- Pilot: 요청 종류별 합성 질의 1건 × 대상 2개 = 답변 6개. 생성 및 L1/L2/L3 모두 성공.
- Warmup은 대상별 1회, 동시성 1, temperature 0, seed 0, 출력 상한 512 tokens.
- Pilot의 관측 범위: TTFT 0.066~0.123초, 응답 시간 0.526~8.123초, TPS 56.29~59.08.
  각 조합 표본이 1개이므로 일반적인 P95나 모델 우열의 근거로 사용하지 않는다.
- 실행 설정 및 모델 digest: `operations/evaluations/phase08-live-pilot/`.
- 원본 결과: `operations/evaluations/phase08-live-pilot-20260928/`.

`D:/workspace/mony_batch/logs/mony_batch/json`의 로그 44개와 LLM 관련 기록 2,398건을
확인했지만, 평가에 필요한 질문·당시 검색 context·검증된 기준 답변 묶음과 사람 채점 자료는
찾지 못했다. 과거 모델 출력만으로 기준 답변을 대신하지 않았다. 실제 데이터의 저장 위치나
익명화된 export가 필요하다. 기존 합성 데이터의 provenance는 `synthetic`으로 유지한다.

### 사람 검토용 확장 배치

`phase08-live-review-20260928`은 합성 고유 질의 20건(단순 QA 7 / 리포트 7 / 뉴스 6)을
두 모델에 실행했다. 점수를 보기 전에 원본 순서에서 균등 간격으로 선택했으며
`operations/evaluations/phase08-live-review/selection.json`에 선택 규칙과 ID를 기록했다.
설정과 모델 digest도 같은 디렉터리에 보존했다. 실행 전후 모델 digest는 동일했다.

- 생성 응답 40/40, 규칙 채점 40/40, judge 채점 40/40, 임베딩 채점 39/40.
- 리포트 답변 6개는 512토큰 상한으로 `finish_reason=length`였다. EXAONE 4개, Qwen 2개.
  생성 성공이라는 상태가 완전한 답변을 의미하지는 않는다. 출력 상한이 이번 비교 조건이다.
- EXAONE의 `simple-qa/qa-030` 임베딩 채점 1건은 `embedding_failed`로 누락됐다.
  0점으로 대체하지 않았다. 원인이 확정되지 않았으므로 추정해서 기록하지 않는다.
- 각 요청 종류의 표본은 6~7건에 불과하다. 합성 자료, 사람 검증 미실시, 표본 부족으로
  모든 정책 후보의 적격 여부는 false다. 이 결과로 운영 모델을 추천하지 않는다.

상세 성능·품질: [확장 배치 보고서](../evaluations/phase08-live-review-20260928/report.md).
사람 검토: [judge 점수 없는 20건](../evaluations/phase08-live-review-20260928/human-review-20.md).
채점 입력: [human-review-20.jsonl](../evaluations/phase08-live-review-20260928/human-review-20.jsonl).
두 모델에서 각각 10개 답변을 고유 질문별 하나씩 선택했다. `human_score`와 `reviewer`는
모두 비어 있으며 AI가 사람 점수를 대신 작성하지 않았다. 채점 전에는 배치 보고서의 judge
점수를 보지 않는다. 위 Markdown의 근거와 답변을 읽고 JSONL에 실제 검토자가 점수와 이름을
입력한 뒤 아래 명령으로 상관 검증한다.

```powershell
.venv/Scripts/python.exe scripts/evaluate.py calibrate --run-id phase08-live-review-20260928 --ratings operations/evaluations/phase08-live-review-20260928/human-review-20.jsonl
```

점수를 채우기 전에는 이 명령을 실행하지 않는다. 현재 상관 검증은 `not_run`이다.
통과 여부는 실제 점수로 계산하며 표본 부족·상수 점수·낮은 상관은 통과로 간주하지 않는다.
합성 자료의 사람 검증이 통과하더라도 실제 서비스 데이터에 대한 Phase 8 완료 조건은 남는다.

확장 배치 재실행(이미 존재하는 run ID 대신 새 ID 사용):

```powershell
.venv/Scripts/python.exe scripts/evaluate.py run --config operations/evaluations/phase08-live-review/gateway.yaml --evaluation-config operations/evaluations/phase08-live-review/evaluation.yaml --deployment qwen-7b@ollama --deployment exaone-7.8b@ollama --run-id phase08-live-review-rerun --save-answers
```

원문 답변과 검토 파일은 기존 gitignore 대상 `operations/evaluations/` 아래 로컬에만 저장했다.
