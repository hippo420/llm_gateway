"""Lab contracts that do not require installing torch in the Gateway environment."""

import json
import subprocess
import sys
from pathlib import Path


def test_environment_check_writes_artifacts_without_gpu(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "labs.financial_ai.gpu.benchmark", "--check-only",
         "--output-root", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    run, = tmp_path.iterdir()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["week_01_complete"] is False
    assert "nvidia_smi" in manifest["environment"]
    assert manifest["source_sha256"]
    assert {p.name for p in run.iterdir()} == {
        "manifest.json", "config.yaml", "samples.jsonl", "metrics.json", "report.md",
    }


def test_rejects_insufficient_measurements(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "labs.financial_ai.gpu.benchmark", "--repeats", "29",
         "--output-root", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert not list(Path(tmp_path).iterdir())
