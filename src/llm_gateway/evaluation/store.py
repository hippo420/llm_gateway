"""Per-run artifacts; summary.json is the atomic commit point consumed by API/metrics."""

import json
import os
import re
import uuid
from pathlib import Path

from .dataset import Dataset
from .models import Run, Summary
from .report import build_summary, markdown


class EvaluationStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", run_id):
            raise ValueError("invalid run ID")
        path = (self.root / run_id).resolve()
        if path.parent != self.root:
            raise ValueError("run path escapes results directory")
        return path

    @staticmethod
    def _write(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("x", encoding="utf-8") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)

    def save(self, run: Run, datasets: list[Dataset]) -> Summary:
        path = self.path(run.run_id)
        path.mkdir(parents=True, exist_ok=False)
        cases = {(d.info.name, c.id): c for d in datasets for c in d.cases}
        hashes = {d.name: d.sha256 for d in run.datasets}
        ratings = []
        for sample in run.samples:
            if "overall" not in sample.scores:
                continue
            case = cases[(sample.dataset, sample.case_id)]
            ratings.append(
                {
                    "run_id": run.run_id,
                    "dataset": sample.dataset,
                    "dataset_sha256": hashes[sample.dataset],
                    "case_id": sample.case_id,
                    "deployment_id": sample.deployment_id,
                    "answer_sha256": sample.answer_sha256,
                    "human_score": None,
                    "reviewer": "",
                    "notes": "",
                    "answer": sample.answer,
                    "question": case.question if sample.answer is not None else None,
                    "context": case.context if sample.answer is not None else None,
                }
            )
        self._write(
            path / "human-review.jsonl",
            "".join(json.dumps(rating, ensure_ascii=False) + "\n" for rating in ratings),
        )
        return self.update(run)

    def update(self, run: Run) -> Summary:
        path = self.path(run.run_id)
        summary = build_summary(run)
        self._write(path / "results.json", run.model_dump_json(indent=2))
        self._write(path / "report.md", markdown(summary))
        self._write(path / "policy-input.json", json.dumps(summary.policy_input, indent=2))
        self._write(path / "summary.json", summary.model_dump_json(indent=2))
        return summary

    def load_run(self, run_id: str) -> Run:
        path = self.path(run_id) / "results.json"
        if path.resolve().parent != self.path(run_id):
            raise ValueError("artifact escapes run directory")
        if path.stat().st_size > 50_000_000:
            raise ValueError("run artifact exceeds 50 MB")
        run = Run.model_validate_json(path.read_text(encoding="utf-8"))
        if run.run_id != run_id:
            raise ValueError("run ID mismatch")
        return run

    def load_summary(self, run_id: str) -> Summary:
        path = self.path(run_id) / "summary.json"
        if path.resolve().parent != self.path(run_id):
            raise ValueError("artifact escapes run directory")
        if path.stat().st_size > 2_000_000:
            raise ValueError("summary exceeds 2 MB")
        summary = Summary.model_validate_json(path.read_text(encoding="utf-8"))
        if summary.run_id != run_id:
            raise ValueError("run ID mismatch")
        return summary

    def summaries(self, limit: int = 100) -> list[Summary]:
        if not self.root.exists():
            return []
        candidates = []
        for path in self.root.glob("*/summary.json"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        paths = [path for _, path in sorted(candidates, reverse=True)]
        results = []
        for path in paths[:limit]:
            try:
                results.append(self.load_summary(path.parent.name))
            except (OSError, ValueError):
                continue
        return sorted(results, key=lambda r: r.created_at, reverse=True)
