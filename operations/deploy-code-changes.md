# 코드 변경 배포 가이드

이 저장소의 Docker Compose 배포에서 Python 코드 변경을 Gateway에 반영하는 절차다.
`Dockerfile`이 `src/`를 이미지에 복사하므로 **코드 변경에는 이미지 재빌드가 필요하다**.
설정 디렉터리는 컨테이너에 읽기 전용으로 마운트되어 코드 이미지와 별도로 관리된다.

## 사전 확인

배포 호스트에서 저장소 최신 변경을 준비하고, Docker Engine과 Docker Compose가 실행 중인지 확인한다.
`.env`를 사용하는 경우 배포에 필요한 `GATEWAY_API_KEY`, Redis URL 등 환경변수가 설정되어 있는지 확인한다.
Gateway가 연결할 upstream(예: LM Studio)과 설정된 모델이 사용 가능한 상태여야 한다.

```bash
docker compose ps
docker compose config --quiet
```

가능하면 로컬에서 테스트와 정적 검사를 먼저 실행한다.

```bash
pytest
ruff check src tests
mypy
```

## 코드 변경 배포

저장소 루트에서 Gateway 서비스만 빌드하고 재생성한다. Prometheus, Grafana, Redis 등 다른 Compose 서비스는 이 명령으로 재시작하지 않는다.

```bash
docker compose up -d --build gateway
```

배포가 끝나면 컨테이너 상태와 시작 로그를 확인한다.

```bash
docker compose ps gateway
docker compose logs --tail=100 gateway
curl --fail http://localhost:35000/healthz
curl --fail http://localhost:35000/readyz
```

`healthz`는 프로세스 상태를, `readyz`는 등록된 upstream 연결 상태를 확인한다. `readyz`가 실패하면 로그와 upstream 주소, 모델 ID, 네트워크 연결을 확인하고 정상 응답을 확인하기 전까지 배포 완료로 처리하지 않는다.

API 키 인증을 켠 환경에서는 아래처럼 인증 헤더를 붙여 모델 목록과 실제 요청을 검증한다. 셸에 키를 직접 입력하거나 로그에 출력하지 않는다.

```bash
export GATEWAY_API_KEY='배포 환경의 API 키'
curl --fail http://localhost:35000/v1/models \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

## 임베딩 API 확인

`/v1/embeddings`의 `model`은 Gateway 설정의 논리 모델명이다. 해당 모델의 enabled deployment가 있어야 하고, upstream 모델도 임베딩을 지원해야 한다. 현재 기본 Docker 설정은 요약/분석용 모델만 등록하므로, 임베딩 모델을 사용할 때는 `config/gateway.docker.yaml`의 `models`에 실제 upstream 모델 ID를 가진 deployment를 먼저 추가한다.

예를 들어 LM Studio에 임베딩 모델이 로드되어 있다면 다음 형태로 등록한다. `upstream_model`은 LM Studio의 `GET /v1/models` 응답에 나온 ID를 사용한다.

```yaml
  embeddings:
    description: "텍스트 임베딩"
    deployments:
      - id: bge-m3@lm-studio
        adapter: openai
        endpoint: http://host.docker.internal:1234/v1
        upstream_model: <LM Studio에 표시된 모델 ID>
```

설정 파일은 Compose bind mount 대상이며 YAML 변경 감시가 활성화되어 있으므로 보통 이미지 재빌드는 필요 없다. 설정 reload 로그를 확인한 뒤 요청한다. 코드와 설정을 함께 변경한 경우 위의 코드 변경 배포 명령으로 새 이미지와 설정을 같이 반영한다.

```bash
curl --fail http://localhost:35000/v1/embeddings \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -d '{"model":"embeddings","input":["배포 후 임베딩 확인"]}'
```

인증이 꺼져 있으면 `Authorization` 헤더를 생략한다. 응답의 `data[0].embedding`이 숫자 벡터인지, `X-Gateway-Deployment` 헤더가 선택된 deployment ID인지 확인한다. upstream이 임베딩 API를 지원하지 않거나 모델 ID가 맞지 않으면 요청은 실패한다.

## 환경변수 변경

`.env` 또는 Compose `environment` 값을 바꾼 경우 컨테이너를 재생성해야 한다. 코드도 바뀌었다면 `--build`를 유지한다.

```bash
docker compose up -d --force-recreate gateway
# 코드도 변경한 경우
docker compose up -d --build --force-recreate gateway
```

## 문제 발생 시 롤백

배포 후 오류가 발생하면 먼저 로그와 readiness를 확인한다. 코드 변경이 원인이면 이전 정상 커밋을 기준으로 복구 변경을 준비한 뒤 같은 빌드·검증 절차를 다시 실행한다.

```bash
git revert <문제가 발생한 커밋>
docker compose up -d --build gateway
docker compose logs --tail=100 gateway
curl --fail http://localhost:35000/readyz
```

운영 데이터가 있는 named volume을 보존해야 하므로 일반적인 코드 롤백에 `docker compose down -v`를 사용하지 않는다. 이 저장소에는 원격 레지스트리 push/pull이나 CI 배포 파이프라인이 정의되어 있지 않다. 원격 환경은 승인된 배포 방식으로 이 절차의 빌드 결과를 전달하되, 환경변수와 모델 설정은 해당 환경의 값을 사용한다.