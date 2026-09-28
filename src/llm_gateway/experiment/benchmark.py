"""Fixed-deployment, sequential-condition benchmarks; no retry, routing or prompt storage."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ..adapters.factory import AdapterFactory
from ..core.context import RequestContext
from ..core.errors import GatewayError
from ..registry.loader import YamlConfigSource
from ..registry.models import ModelDeployment, ModelRegistry
from ..resilience.config import ResilienceConfig, RetryConfig
from ..routing.decision import RoutingDecision
from ..schemas.chat import ChatCompletionRequest
from ..service.chat_service import ChatService
from .report import Observation, summarize


async def measure(
    service: ChatService,
    deployment: ModelDeployment,
    requests: list[ChatCompletionRequest],
    *,
    concurrency: int,
    repeat: int,
    warmup: int,
) -> dict:
    if not requests or concurrency < 1 or repeat < 1 or warmup < 0:
        raise ValueError(
            "nonempty dataset, positive concurrency/repeat and nonnegative warmup required"
        )

    async def call(index: int) -> dict:
        ctx = RequestContext(request_id=f"benchmark-{index}")
        ctx.routing_decision = RoutingDecision(deployment, "benchmark", "static")
        ctx.resilience_config = ResilienceConfig(retry=RetryConfig(max_attempts=1))
        started = time.perf_counter()
        try:
            result = await service.complete(requests[index % len(requests)], ctx, deployment)
        except GatewayError as exc:
            return {
                "index": index,
                "outcome": "error",
                "error_code": exc.code,
                "elapsed_sec": time.perf_counter() - started,
            }
        return {
            "index": index,
            "outcome": "success",
            "timings": asdict(result.timings),
            "usage": result.response.usage.model_dump() if result.response.usage else None,
        }

    warmup_results = [await call(index) for index in range(warmup)]
    if any(item["outcome"] != "success" for item in warmup_results):
        raise ValueError("warm-up failed; measured run was not started")
    work = iter(range(len(requests) * repeat))
    samples: list[dict] = []

    async def worker() -> None:
        for index in work:
            samples.append(await call(index))

    started = time.perf_counter()
    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(requests) * repeat)):
            group.create_task(worker())
    elapsed = time.perf_counter() - started
    observations = []
    for sample in samples:
        timing = sample.get("timings", {})
        usage = sample.get("usage") or {}
        generation = timing.get("generation_sec")
        output = usage.get("completion_tokens")
        observations.append(
            Observation(
                0,
                sample["outcome"],
                timing.get("ttft_sec"),
                timing.get("total_sec"),
                output / generation
                if output is not None and generation and generation >= 0.001
                else None,
                usage.get("prompt_tokens"),
                output,
            )
        )
    return {
        "deployment_id": deployment.id,
        "adapter": deployment.adapter,
        "upstream_model": deployment.upstream_model,
        "options": deployment.options.model_dump(),
        "concurrency": concurrency,
        "repeat": repeat,
        "warmup": warmup,
        "elapsed_sec": elapsed,
        "throughput_req_min": len(samples) / elapsed * 60,
        "summary": summarize(observations, 20),
        "gpu_memory_peak_gb": None,
        "gpu_util_mean": None,
        "quality": None,
        "samples": sorted(samples, key=lambda sample: sample["index"]),
    }


def markdown(result: dict) -> str:
    lines = [
        "# Benchmark / Before-After 기록",
        "",
        "## 실행 조건",
        "",
        f"- 일시: {result['started_at']}",
        f"- 데이터셋 SHA-256: {result['dataset_sha256']}",
        "- 실행 방식: deployment별 순차 실행, retry/fallback 없음, warm-up 제외",
        "- GPU: 외부에서 자원 격리/상주 모델 확인 필요",
        "",
        "## 결과",
        "",
        "| 조건 | 동시성 | 요청 수 | TTFT P50 | TTFT P95 | TPS 평균 |"
        " Latency P95 | Latency P99 | 오류율 | req/min |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in result["runs"]:
        report = run["summary"]
        values = [
            run["deployment_id"],
            run["concurrency"],
            report["samples"],
            report["ttft_p50_sec"],
            report["ttft_p95_sec"],
            report["output_tps_mean"],
            report["latency_p95_sec"],
            report["latency_p99_sec"],
            report["error_rate"],
            run["throughput_req_min"],
        ]
        lines.append(
            "| "
            + " | ".join(
                "미측정" if v is None else f"{v:.4f}" if isinstance(v, float) else str(v)
                for v in values
            )
            + " |"
        )
    lines += [
        "",
        "## 품질 / 자원",
        "",
        "GPU 메모리·사용률 및 품질 점수: 미측정.",
        "",
        "## 해석",
        "",
        "표본 수, 요청 종류/길이 편향과 실행 순서 영향을 검토해야 한다.",
        "",
        "## 결론 / 결정",
        "",
        "자동 채택 판단 없음. 실제 자원/품질 검증 후 결정한다.",
        "",
        "## 관찰된 문제",
        "",
        "원자료 samples의 error_code와 누락 지표를 확인한다.",
        "",
        "## 재현 방법",
        "",
        "JSON의 invocation 필드에 실행 인자와 조건이 기록되어 있다.",
        "",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> dict:
    data = args.dataset.read_bytes()
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("dataset must contain JSON objects")
    snapshot = YamlConfigSource(args.config).load()
    registry = ModelRegistry(snapshot)
    deployments = {d.id: d for m in snapshot.models.values() for d in m.deployments}
    selected = [deployments[name] for name in args.deployment]
    if any(not d.enabled for d in selected):
        raise ValueError("benchmark deployment must be enabled")
    # Validate every condition before generating anything.
    conditions = []
    for deployment in selected:
        requests = [
            ChatCompletionRequest.model_validate({**row, "model": deployment.logical_model})
            for row in rows
        ]
        for request in requests:
            if request.unsupported_fields():
                raise ValueError("dataset includes unsupported request fields")
        conditions.append((deployment, requests))
    result: dict = {
        "started_at": datetime.now(UTC).isoformat(),
        "dataset_sha256": hashlib.sha256(data).hexdigest(),
        "invocation": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "runs": [],
    }
    factory = AdapterFactory()
    service = ChatService(registry, factory)
    try:
        for deployment, requests in conditions:
            for concurrency in args.concurrency:
                result["runs"].append(
                    await measure(
                        service,
                        deployment,
                        requests,
                        concurrency=concurrency,
                        repeat=args.repeat,
                        warmup=args.warmup,
                    )
                )
    finally:
        await factory.close_all()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/gateway.yaml"))
    parser.add_argument("--deployment", action="append", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--concurrency", default="1,4,8")
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        args.concurrency = [int(value) for value in args.concurrency.split(",")]
        if min(args.concurrency) < 1 or args.repeat < 1 or args.warmup < 0:
            raise ValueError("invalid benchmark counts")
        if args.out.suffix != ".json":
            raise ValueError("--out must end with .json")
        if args.out.exists() or args.out.with_suffix(".md").exists():
            raise ValueError("output already exists; use a new run filename")
        result = asyncio.run(run(args))
    except (ValueError, KeyError, OSError, GatewayError) as exc:
        parser.exit(
            2, f"Benchmark failed: {type(exc).__name__}. Check configuration and dataset.\n"
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    args.out.with_suffix(".md").write_text(markdown(result), encoding="utf-8")
    print(f"Saved {args.out} and {args.out.with_suffix('.md')}")


if __name__ == "__main__":
    main()
