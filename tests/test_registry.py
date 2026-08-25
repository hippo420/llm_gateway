"""Model Registry / Config loader 테스트."""

from __future__ import annotations

import pytest


class TestYamlConfigSource:
    @pytest.mark.skip(reason="TODO: Phase 1 구현 후 활성화")
    def test_loads_sample_config(self, tmp_path):
        """config/gateway.yaml 형식을 그대로 파싱할 수 있어야 한다."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_missing_file_raises_gw1001(self, tmp_path):
        """파일이 없으면 ConfigNotFoundError."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_duplicate_deployment_id_raises_gw1004(self, tmp_path):
        """deployment id 는 전역 고유해야 한다."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_unknown_adapter_raises_gw1003(self, tmp_path):
        """factory 에 없는 adapter 이름은 로드 자체를 거부한다."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_defaults_merged_into_deployment(self, tmp_path):
        """defaults.timeout/options 가 deployment 에 병합돼야 한다.

        deployment 가 일부만 override 하면 나머지는 defaults 값이 남는다.
        """
        raise NotImplementedError


class TestModelRegistry:
    @pytest.mark.skip(reason="TODO")
    def test_resolve_returns_enabled_deployment(self, registry):
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_unknown_model_raises_gw4001(self, registry):
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_all_disabled_raises_gw4004(self, registry):
        """enabled 후보가 하나도 없으면 NoAvailableDeploymentError."""
        raise NotImplementedError

    @pytest.mark.skip(reason="TODO")
    def test_candidates_returns_all_enabled(self, registry):
        """Phase 5 가 이 목록 위에서 고른다. 복수 반환이 계약이다."""
        raise NotImplementedError
