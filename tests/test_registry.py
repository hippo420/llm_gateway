"""Model Registry / Config loader 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_gateway.core.errors import (
    AdapterNotRegisteredError,
    ConfigNotFoundError,
    DuplicateDeploymentIdError,
    ModelNotFoundError,
    NoAvailableDeploymentError,
)
from llm_gateway.registry.loader import YamlConfigSource

SAMPLE = """
version: 1

defaults:
  timeout:
    connect: 5
    read: 30
    total: 180
  options:
    temperature: 0.2
    top_p: 0.9
    max_tokens: 2048

models:
  qwen-7b:
    description: "기본 모델"
    deployments:
      - id: qwen-7b@ollama
        adapter: ollama
        endpoint: http://localhost:11434
        upstream_model: qwen2.5:7b
        enabled: true
        weight: 100
        timeout:
          read: 60
        options:
          temperature: 0.5
        extra:
          keep_alive: 30m
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "gateway.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestYamlConfigSource:
    def test_loads_sample_config(self, tmp_path):
        """config/gateway.yaml 형식을 그대로 파싱할 수 있어야 한다."""
        snapshot = YamlConfigSource(_write(tmp_path, SAMPLE)).load()

        assert snapshot.version == 1
        assert list(snapshot.models) == ["qwen-7b"]

        dep = snapshot.models["qwen-7b"].deployments[0]
        assert dep.id == "qwen-7b@ollama"
        # YAML 에 없는 필드다. 부모 키에서 채워 넣어야 한다.
        assert dep.logical_model == "qwen-7b"
        assert dep.upstream_model == "qwen2.5:7b"
        assert dep.extra == {"keep_alive": "30m"}

    def test_repository_config_is_loadable(self):
        """저장소에 커밋된 정본 설정이 항상 로드 가능해야 한다."""
        snapshot = YamlConfigSource(Path("config/gateway.yaml")).load()

        assert "qwen-7b" in snapshot.models

    def test_missing_file_raises_gw1001(self, tmp_path):
        """파일이 없으면 ConfigNotFoundError."""
        with pytest.raises(ConfigNotFoundError) as exc:
            YamlConfigSource(tmp_path / "nope.yaml").load()

        assert exc.value.code == "GW-1001"

    def test_duplicate_deployment_id_raises_gw1004(self, tmp_path):
        """deployment id 는 전역 고유해야 한다."""
        text = SAMPLE + """
  qwen-7b-copy:
    deployments:
      - id: qwen-7b@ollama
        adapter: ollama
        endpoint: http://localhost:11434
        upstream_model: qwen2.5:7b
"""
        with pytest.raises(DuplicateDeploymentIdError) as exc:
            YamlConfigSource(_write(tmp_path, text)).load()

        assert exc.value.code == "GW-1004"

    def test_unknown_adapter_raises_gw1003(self, tmp_path):
        """factory 에 없는 adapter 이름은 로드 자체를 거부한다."""
        text = SAMPLE.replace("adapter: ollama", "adapter: tensorrt")

        with pytest.raises(AdapterNotRegisteredError) as exc:
            YamlConfigSource(_write(tmp_path, text)).load()

        assert exc.value.code == "GW-1003"

    def test_unknown_adapter_allowed_while_disabled(self, tmp_path):
        """아직 구현되지 않은 adapter 라도 enabled=false 면 기동을 막지 않는다.

        정본 설정에는 다음 Phase 용 deployment 가 미리 적혀 있다.
        (config/gateway.yaml 의 qwen-14b@vllm)
        """
        text = SAMPLE.replace("adapter: ollama", "adapter: tensorrt").replace(
            "enabled: true", "enabled: false"
        )

        snapshot = YamlConfigSource(_write(tmp_path, text)).load()

        assert snapshot.models["qwen-7b"].deployments[0].enabled is False

    def test_defaults_merged_into_deployment(self, tmp_path):
        """defaults.timeout/options 가 deployment 에 병합돼야 한다.

        deployment 가 일부만 override 하면 나머지는 defaults 값이 남는다.
        """
        dep = YamlConfigSource(_write(tmp_path, SAMPLE)).load().models["qwen-7b"].deployments[0]

        assert dep.timeout.read == 60  # deployment override
        assert dep.timeout.connect == 5  # defaults 유지
        assert dep.timeout.total == 180  # defaults 유지
        assert dep.options.temperature == 0.5  # deployment override
        assert dep.options.top_p == 0.9  # defaults 유지
        assert dep.options.max_tokens == 2048  # defaults 유지

    def test_invalid_endpoint_rejected(self, tmp_path):
        """스킴 없는 endpoint 는 GW-1002."""
        text = SAMPLE.replace("http://localhost:11434", "localhost:11434")

        with pytest.raises(Exception) as exc:
            YamlConfigSource(_write(tmp_path, text)).load()

        assert exc.value.code == "GW-1002"

    def test_is_stale_detects_change(self, tmp_path):
        """Phase 4 의 watcher 가 쓰는 신호."""
        path = _write(tmp_path, SAMPLE)
        source = YamlConfigSource(path)
        source.load()

        assert source.is_stale() is False

        path.write_text(SAMPLE.replace("weight: 100", "weight: 50"), encoding="utf-8")
        # mtime 해상도가 낮은 파일시스템을 위해 값을 직접 흔든다.
        import os

        os.utime(path, (0, 0))
        assert source.is_stale() is True


class TestModelRegistry:
    def test_resolve_returns_enabled_deployment(self, registry):
        assert registry.resolve("qwen-7b").id == "qwen-7b@fake"

    def test_unknown_model_raises_gw4001(self, registry):
        with pytest.raises(ModelNotFoundError) as exc:
            registry.resolve("qwen-70b")

        assert exc.value.code == "GW-4001"
        assert exc.value.http_status == 404

    def test_all_disabled_raises_gw4004(self, registry):
        """enabled 후보가 하나도 없으면 NoAvailableDeploymentError."""
        with pytest.raises(NoAvailableDeploymentError) as exc:
            registry.candidates("qwen-off")

        assert exc.value.code == "GW-4004"
        assert exc.value.http_status == 503

    def test_candidates_returns_all_enabled(self, registry):
        """Phase 5 가 이 목록 위에서 고른다. 복수 반환이 계약이다."""
        candidates = registry.candidates("qwen-7b")

        assert isinstance(candidates, list)
        assert [d.id for d in candidates] == ["qwen-7b@fake"]

    def test_list_models_includes_disabled(self, registry):
        """GET /v1/models 는 왜 라우팅되지 않는지도 보여줘야 한다."""
        names = [entry.name for entry in registry.list_models()]

        assert set(names) == {"qwen-7b", "qwen-off"}

    def test_all_deployments_only_enabled(self, registry):
        """readyz 는 살아있어야 하는 것만 확인한다."""
        assert [d.id for d in registry.all_deployments()] == ["qwen-7b@fake"]
