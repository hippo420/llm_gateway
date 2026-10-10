"""Local GPU inspection and offline lab artifacts; no torch runtime dependency."""

from __future__ import annotations

import csv
import io
import json
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def gpu_status() -> dict:
    result = {"scope": "gateway_host", "source": "nvidia-smi",
              "observed_at": datetime.now(UTC).isoformat(), "devices": []}
    try:
        process = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,name,driver_version,memory.total,"
             "memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if process.returncode:
            return {**result, "status": "unavailable", "reason": "nvidia_smi_failed"}
        devices = []
        for row in csv.reader(io.StringIO(process.stdout), skipinitialspace=True):
            if len(row) != 8:
                raise ValueError("unexpected GPU fields")
            devices.append({
                "index": int(row[0]), "uuid": row[1], "name": row[2], "driver": row[3],
                "memory_total_mib": number(row[4]), "memory_used_mib": number(row[5]),
                "utilization_percent": number(row[6]), "temperature_c": number(row[7]),
            })
        return {**result, "status": "ok" if devices else "unavailable", "devices": devices}
    except FileNotFoundError:
        return {**result, "status": "unavailable", "reason": "nvidia_smi_not_found"}
    except subprocess.TimeoutExpired:
        return {**result, "status": "unavailable", "reason": "timeout"}
    except (OSError, ValueError, csv.Error):
        return {**result, "status": "unavailable", "reason": "gpu_query_error"}


def number(value: str) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except ValueError:
        return None  # NVIDIA may report N/A or [Not Supported].


def latest_benchmark(root: Path) -> dict:
    """Only complete artifact bundles; interrupted runs are ignored."""
    if not root.is_dir():
        return {"status": "not_found", "run_id": None}
    for run in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not run.is_dir() or run.is_symlink():
            continue
        paths = [run / name for name in ("manifest.json", "metrics.json", "samples.jsonl",
                                        "config.yaml", "report.md")]
        if not all(p.is_file() and not p.is_symlink() for p in paths):
            continue
        try:
            if paths[0].stat().st_size > 2_000_000 or paths[1].stat().st_size > 2_000_000:
                continue
            manifest = json.loads(paths[0].read_text(encoding="utf-8"))
            metrics = json.loads(paths[1].read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not isinstance(metrics, list):
                continue
            if not manifest.get("finished_at") or not metrics:
                continue  # Environment-only checks are not benchmark runs.
            return {"status": "ok", "run_id": run.name,
                    "finished_at": manifest["finished_at"],
                    "week_01_complete": manifest.get("week_01_complete", False),
                    "source_sha256": manifest.get("source_sha256"),
                    "metrics": metrics}
        except (OSError, ValueError, UnicodeError):
            continue
    return {"status": "not_found", "run_id": None}
