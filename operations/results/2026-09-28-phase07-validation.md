# Benchmark / Before-After 기록 — Phase 7 기능 검증

이 기록은 합성 테스트 결과다. 실제 모델 성능 벤치마크가 아니다.

## 실행 조건

| 항목 | 값 |
|---|---|
| 일시 | 2026-09-28 |
| 목적 | A/B 할당, guardrail 및 시간 분할 측정 도구 검증 |
| 데이터 | 테스트용 세션 키 10,000개, FakeAdapter 고정 응답 |
| 설정 | control/treatment 50/50, 최소 표본·warmup·오류 주입 |
| 실행 환경 | Windows 로컬 Python 가상환경 |
| 실제 GPU 생성 | 실행하지 않음 |

## 결과

| 검증 | 결과 |
|---|---|
| 10,000개 세션 분배 | control 5,015건(50.15%), treatment 4,985건(49.85%) |
| 동일 세션/순서 변경 | 동일 할당 유지 |
| fallback 성공 | 최초 variant에 fallback outcome 기록, 깨끗한 성공 지표에서 제외 |
| error/TTFT guardrail | 최소 표본 후 control 전환, 표본 만료 후에도 중단 유지 |
| benchmark warmup | 측정 표본에서 제외, 실패 시 측정 시작 안 함 |
| 전체 회귀 테스트 | 338개 통과 |

할당 원자료: [2026-09-28-phase07-validation.json](2026-09-28-phase07-validation.json).
세션 입력 패턴/범위와 가중치/집계값을 담았으며 실제 모델 성능 값은 null이다.
현재 저장소 정책에서 results JSON은 gitignore 대상이므로 공유 시 함께 보관한다.

### 조건 A — Ollama

TTFT/TPS/Latency/Error Rate/처리량/GPU Memory/Util: 미측정.
읽기 전용 `/api/tags` 응답 200만 확인했다.

### 조건 B — vLLM

TTFT/TPS/Latency/Error Rate/처리량/GPU Memory/Util: 미측정.
`:8000/v1/models`는 404였으며 현재 vLLM adapter는 미구현이다.

## 품질

Faithfulness/Answer Relevance/Citation Accuracy/Overall Quality: 미측정(Phase 8).

## 해석

합성 결과는 제어 흐름의 검증이다. 실제 서빙 간 성능 차이나 품질 우위를 의미하지 않는다.
Prometheus 연결 시간 초과로 운영 대시보드의 실제 수집도 확인하지 못했다.

## 결론 / 결정

서빙 채택 결정은 보류한다. 실험은 기본 비활성이다. 실제 비교는 대표 데이터셋과
GPU 자원 분리 또는 시간 분할, 사전 종료 조건을 정한 뒤 수행한다.

## 관찰된 문제

실서버 비교에 필요한 vLLM adapter/서버, 자원 계측 및 품질 평가가 아직 없다.

## 재현 방법

```powershell
.venv/Scripts/python.exe -m pytest tests/test_experiments.py tests/test_benchmark.py -q
.venv/Scripts/python.exe scripts/benchmark.py --help
```

실서버 준비 후 `scripts/benchmark.py`로 JSON 원자료와 Markdown 보고서를 함께 생성한다.
