from __future__ import annotations

import json
import subprocess

import pytest

from llm_gateway.observability.gpu import gpu_status, latest_benchmark


def test_gpu_query_parses_device_and_unsupported_fields(monkeypatch):
    def run(args, **kwargs):
        assert kwargs["timeout"] == 3
        assert "--format=csv,noheader,nounits" in args
        return subprocess.CompletedProcess(args, 0,
            '0, GPU-abc, "NVIDIA, Test", 617.42, 12282, 568, N/A, 36\n')
    monkeypatch.setattr(subprocess, "run", run)
    result = gpu_status()
    assert result["status"] == "ok"
    device = result["devices"][0]
    assert device["name"] == "NVIDIA, Test"
    assert device["memory_used_mib"] == 568
    assert device["utilization_percent"] is None


@pytest.mark.parametrize("error,reason", [
    (FileNotFoundError(), "nvidia_smi_not_found"),
    (subprocess.TimeoutExpired("nvidia-smi", 3), "timeout"),
])
def test_gpu_unavailable(monkeypatch, error, reason):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", run)
    assert gpu_status()["reason"] == reason


def test_latest_benchmark_ignores_interrupted_and_check_only_runs(tmp_path):
    complete = tmp_path / "20261010T000001Z"
    complete.mkdir()
    (complete / "manifest.json").write_text(json.dumps({
        "finished_at": "2026-10-10T00:00:01Z", "week_01_complete": False,
    }))
    (complete / "metrics.json").write_text('[{"status":"ok","device":"cpu"}]')
    for name in ["samples.jsonl", "config.yaml", "report.md"]:
        (complete / name).write_text("")
    interrupted = tmp_path / "20261010T000002Z"
    interrupted.mkdir()
    (interrupted / "samples.jsonl").write_text("")
    result = latest_benchmark(tmp_path)
    assert result["run_id"] == complete.name
    assert result["week_01_complete"] is False
    (complete / "metrics.json").write_text("[]")
    assert latest_benchmark(tmp_path)["status"] == "not_found"


async def test_gpu_admin_api_requires_key(client, app, monkeypatch, tmp_path):
    assert (await client.get("/admin/gpu")).status_code == 401
    assert (await client.get("/admin/gpu/benchmark/latest")).status_code == 401
    app.state.settings.api_key = "test-key"
    app.state.settings.gpu_benchmark_results_path = tmp_path
    monkeypatch.setattr("llm_gateway.api.routes.gpu.gpu_status",
                        lambda: {"status": "unavailable", "devices": []})
    assert (await client.get("/admin/gpu")).status_code == 401
    headers = {"Authorization": "Bearer test-key"}
    response = await client.get("/admin/gpu", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    response = await client.get("/admin/gpu/benchmark/latest", headers=headers)
    assert response.json()["status"] == "not_found"
