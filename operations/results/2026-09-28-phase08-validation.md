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
