# Jenkins Docker 배포

Jenkins에서 Pipeline script from SCM을 선택하고 Script Path를 `Jenkinsfile`로 지정한다.

- Linux 에이전트에 `docker` label, Docker CLI, Docker Compose v2 (`up --wait` 지원)가 필요하다. Docker daemon 접근 권한을 제공한다.
- 에이전트가 연결한 Docker daemon에 배포한다. 배포 호스트의 에이전트에만 `docker` label을 지정한다.
- `network_dev`가 필요하다. 없으면 최초 한 번 `docker network create network_dev`를 실행한다.
- 호스트 포트는 35000이며 같은 네트워크에서는 `http://gateway:35000`으로 접근한다.
- 선택적으로 Jenkins Secret file credential에 환경변수 파일을 등록하고 빌드 파라미터 `ENV_CREDENTIALS_ID`에 ID를 입력한다. 파일에는 `GATEWAY_API_KEY=...`, `GATEWAY_REDIS_URL=redis://redis:6379/0` 등을 지정한다. Redis는 별도로 실행하고 network_dev에 연결한다. 생략하면 키와 Redis 없이 실행한다.

Gateway만 빌드·배포하며 설정은 이미지에 포함한다. `config/gateway.docker.yaml`의 endpoint는 배포 호스트에서 접근 가능해야 한다. 설정 변경 후에는 재빌드한다.

기존 Gateway가 포트 35000을 사용하면 먼저 중지한다. 평가 결과는 새 Compose 프로젝트의 named volume에 유지하며 기존 Compose 프로젝트의 볼륨과 별개다. 기존 데이터가 필요하면 사전에 이관한다.

빌드 번호별 이미지로 배포하고 healthz 및 readyz를 확인한다. upstream 연결이나 모델 확인 실패도 빌드 실패로 처리한다. 실패 시 새 컨테이너가 실행 중일 수 있으며 자동 롤백은 하지 않는다. 이전 정상 커밋으로 재빌드·배포하여 복구한다. 이미지는 자동 삭제하지 않는다.

동일 대상에는 이 Jenkins job 하나만 사용한다. 컨테이너 교체 시 짧은 중단이 발생한다.
