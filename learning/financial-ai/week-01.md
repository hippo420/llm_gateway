# Week 01 — CUDA / PyTorch / GPU

## Gateway 연결

Gateway 관리자 API에서 GPU 상태와 이 실습의 최근 측정 결과를 조회한다.
`GATEWAY_API_KEY` 설정 후 `Authorization: Bearer <key>`를 전달한다.

| API | 반환 |
|---|---|
| `GET /admin/gpu` | Gateway 호스트의 GPU 이름·드라이버·전체/사용 메모리(MiB)·사용률·온도 |
| `GET /admin/gpu/benchmark/latest` | 최근 완료된 실습의 run ID·완료 여부·dtype/크기별 지연시간과 메모리 |

`nvidia-smi`가 없거나 GPU가 미노출이면 상태 API는 `status: unavailable`을 반환한다.
외부 Ollama 서버의 GPU 상태는 이 API에서 확인할 수 없다. 기존 GPU exporter를 사용한다.
벤치마크는 CLI에서 실행하고 Gateway에서는 결과만 조회한다. 중단된 실행과 환경 점검은
최근 벤치마크 선택에서 제외한다. CPU 실행 결과도 반환하되 CUDA 완료 여부를 보존한다.
Gateway 런타임은 PyTorch를 import하거나 GPU 모델을 직접 적재하지 않는다.

```powershell
$headers = @{ Authorization = "Bearer $env:GATEWAY_API_KEY" }
Invoke-RestMethod http://localhost:8080/admin/gpu -Headers $headers
Invoke-RestMethod http://localhost:8080/admin/gpu/benchmark/latest -Headers $headers
```

결과 디렉터리를 바꾸면 `GATEWAY_GPU_BENCHMARK_RESULTS_PATH`를 설정한다.
컨테이너에서는 결과 디렉터리를 읽기 전용으로 마운트하고 이 설정을 컨테이너 경로로
지정한다. 호스트 GPU를 조회하려면 컨테이너에도 NVIDIA 장치와 `nvidia-smi`가 노출되어야 한다.

## 목표와 구현

Gateway와 분리된 `labs/financial_ai/gpu/benchmark.py`에서 환경 점검과 정방 행렬곱을 수행한다.
CPU와 CUDA, float32/float16/bfloat16, 크기 256/1024/2048을 비교한다.
기본값은 warm-up 5회와 측정 30회다. 30회 미만은 거절한다.

## 실행

저장소 루트에서 PowerShell:

```powershell
.venv/Scripts/python.exe -m venv .venv-ml
.venv-ml/Scripts/python.exe -m pip install -r labs/financial_ai/requirements-ml.txt
.venv-ml/Scripts/python.exe -m labs.financial_ai.gpu.benchmark --check-only
.venv-ml/Scripts/python.exe -m labs.financial_ai.gpu.benchmark
```

Linux/WSL에서는 Python으로 venv를 생성하고 `.venv-ml/bin/python`을 사용한다.
PyTorch 2.10.0 CUDA 12.8 wheel을 고정했다.
[공식 설치 명령](https://pytorch.org/get-started/previous-versions/#v2100)을 기준으로 한다.
실제 설치된 모든 패키지 버전은 manifest에 기록한다. CUDA 설치가 성공한 환경에서
`python -m pip freeze`로 전이 의존성까지 보관한다. 현재 CPU 검증 환경은
`labs/financial_ai/requirements-ml-cpu.lock.txt`에 고정한다.

CPU만 실행할 때는 `--device cpu`, 특정 장치는 `--gpu-index 0`으로 선택한다.
CPU 실행·환경 점검만으로는 CUDA 실측 완료로 표시하지 않는다.
CUDA를 요청했는데 미가용이면 실패 결과를 저장하고 exit code 1을 반환한다.

```powershell
.venv-ml/Scripts/python.exe -m labs.financial_ai.gpu.benchmark --device cpu --sizes 256
.venv-ml/Scripts/python.exe -m labs.financial_ai.gpu.benchmark --device both --sizes 256 1024 2048 --warmup 5 --repeats 30
```

CPU 전용 환경을 재현할 때는 CUDA requirements 대신 다음을 설치한다.

```powershell
.venv-ml/Scripts/python.exe -m pip install -r labs/financial_ai/requirements-ml-cpu.lock.txt
```

설치 중 C 드라이브 임시 공간이 부족하면 작업 폴더에 임시 디렉터리를 지정한다.

```powershell
New-Item -ItemType Directory -Force .venv-ml/tmp | Out-Null
$env:TEMP = "$PWD/.venv-ml/tmp"
$env:TMP = $env:TEMP
.venv-ml/Scripts/python.exe -m pip install --no-cache-dir -r labs/financial_ai/requirements-ml.txt
```

## 측정 해석

- 입력과 출력 텐서를 먼저 할당하고 `torch.mm(..., out=...)`을 측정한다.
- CUDA는 매 측정 전후 `synchronize`한다. 수치는 커널 제출과 동기화 비용을 포함한
  wall time이며 순수 CUDA Event 시간과 직접 비교하지 않는다.
- warm-up, 할당, 입력 전송, 결과 전송, 정확성 검증은 지연시간에서 제외한다.
- TF32를 끄고 float32 precision을 highest로 설정한다. CPU 스레드는 기본 4개다.
- 입력에서 8×8 출력 타일을 CPU float64로 재계산하여 dtype별 오차 한계로 검증한다.
  전체 출력의 finite 여부도 검사한다. 모든 원소의 정확성을 증명하는 검사는 아니다.
- allocated/reserved와 각각의 peak는 PyTorch allocator 수치다.
  nvidia-smi의 전체 장치 메모리·드라이버는 manifest의 실행 전 스냅샷이다.
  allocator 메모리와 장치 사용량을 더하거나 같은 수치로 해석하지 않는다.
- 다른 GPU 작업을 종료한 뒤 실행한다. CLI가 Ollama를 자동 종료하거나 GPU를 독점하지 않는다.
  첫 실행과 후속 실행, 전력·온도·백그라운드 부하에 따라 값이 달라질 수 있다.

## 결과 계약과 검증

`operations/results/financial-ai/week-01/<UTC-run-id>/` 아래에 다음을 저장한다.
원자료는 생성 즉시 flush하며 warm-up은 포함하지 않는다. 기존 실행은 덮어쓰지 않는다.

| 파일 | 내용 |
|---|---|
| manifest.json | 실행 시각, commit와 dirty 상태, 소스/설정 SHA256, Python·패키지·GPU·CUDA, 성공/실패 수 |
| config.yaml | JSON 문법의 유효한 YAML 설정, seed·스레드·크기·dtype·반복 수 |
| samples.jsonl | case·iteration별 latency_ms |
| metrics.json | case별 count·mean·median·P95·min/max, 정확성, GPU 메모리, 실패/OOM |
| report.md | 측정 방법, 완료 여부, 요약 표와 오류 |

결과는 Git에서 제외한다. 공유할 때 해당 run 디렉터리 전체를 보관한다.
외부 데이터·모델·프롬프트를 사용하지 않아 해당 해시는 적용하지 않는다.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_gpu_lab.py -q --basetemp .venv-ml/test-tmp -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check labs/financial_ai/gpu tests/test_gpu_lab.py
```

## 실행 기록

2026-10-10: Windows에서 RTX 4070 Ti 12GB, NVIDIA driver 617.42 확인.
초기 Gateway Python 3.14.6 환경에는 torch가 없었다.
CUDA wheel 다운로드(2.9GB) 및 설치를 시도했으나 C 임시 공간, 이어 D 압축 해제 공간
부족으로 실패했다. 불완전한 torch 설치만 제거하고 CPU wheel로 검증했다.
CUDA 드라이버의 장치 인식과 PyTorch CUDA 연산 성공은 별개다.
현재 상태는 **구현 완료, CUDA 실측 미완료**다. 충분한 디스크 공간 확보 후
CUDA requirements로 재설치하고 기본 `--device both` 실행을 완료해야 한다.

검증 실행:

- 명령: `.venv-ml/Scripts/python.exe -m labs.financial_ai.gpu.benchmark --device cpu --sizes 256 512`
- 결과: `operations/results/financial-ai/week-01/20261010T072629.496441Z/`
- float32/float16/bfloat16 × 256/512의 6개 case, 각 warm-up 5회 + 측정 30회,
  총 원자료 180개, 정확성 검사 모두 통과. GPU 메모리는 CPU 실행에 적용하지 않는다.
- N=512 median: float32 0.850ms, float16 159.041ms, bfloat16 0.475ms.
  이 CPU에서 float16이 느렸다. dtype의 메모리 크기만으로 성능을 예측할 수 없다.
- 기본 크기 전체 CPU 실행은 float16 대형 행렬의 긴 실행 시간으로 중단했다.
  해당 부분 원자료는 완료 결과로 사용하지 않는다.
- CPU wheel 환경에서 CUDA 요청은 unavailable과 exit code 1로 종료하는지 확인했다.
- CLI 테스트 2개와 ruff 통과. 전체 pytest는 449 passed / 5 failed:
  기존 `config/gateway.yaml`이 models를 누락하고 미허용 gateway 키를 포함하여
  registry 1개와 diagnosis lifespan 4개가 실패했다. 이번 작업에서 해당 설정은 수정하지 않았다.
- torch import 시 NumPy 미설치 경고가 있지만 본 실습은 NumPy API를 사용하지 않는다.

재현 시 manifest의 commit뿐 아니라 dirty 상태와 source_sha256도 확인한다.
아직 커밋하지 않은 구현이므로 manifest commit은 변경 이전 HEAD이며 소스 해시가
실제 실행 파일을 식별한다.

다음 주 진입 조건: CPU/CUDA의 모든 설정에서 30개 이상 원자료와 정확성 검증이
성공하고 환경·메모리·보고서를 보존할 것. 분류 학습은 이 분리 환경을 확장한다.
