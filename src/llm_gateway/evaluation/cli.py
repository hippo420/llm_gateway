import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from ..adapters.factory import AdapterFactory
from ..core.errors import GatewayError
from ..registry.loader import YamlConfigSource
from ..registry.models import ModelRegistry
from ..resilience.config import ResilienceConfig, RetryConfig
from ..service.chat_service import ChatService
from .calibration import calibrate
from .config import EvaluationConfig
from .dataset import load_dataset
from .runner import evaluate
from .store import EvaluationStore


async def execute(args: argparse.Namespace) -> None:
    config = EvaluationConfig.load(args.evaluation_config)
    datasets = [load_dataset(spec) for spec in config.datasets]
    snapshot = YamlConfigSource(args.config).load()
    # A batch has its own registry; production breakers/experiments must not skip samples.
    registry = ModelRegistry(
        snapshot.model_copy(
            update={
                "resilience": ResilienceConfig(retry=RetryConfig(max_attempts=1)),
                "experiments": {},
            }
        )
    )
    store = EvaluationStore(args.out_dir)
    if store.path(args.run_id).exists():
        raise ValueError("run ID already exists; use a new run ID")
    factory = AdapterFactory()
    try:
        run = await evaluate(
            registry,
            ChatService(registry, factory),
            config,
            datasets,
            args.deployment,
            args.run_id,
            save_answers=args.save_answers,
        )
        store.save(run, datasets)
    finally:
        await factory.close_all()
    print(f"Saved evaluation to {store.path(args.run_id)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline quality evaluation and human calibration")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, default=Path("config/gateway.yaml"))
    run.add_argument("--evaluation-config", type=Path, default=Path("config/evaluation.yaml"))
    run.add_argument("--deployment", action="append", required=True)
    run.add_argument(
        "--run-id", default=datetime.now(UTC).strftime("eval-%Y%m%d-%H%M%S-") + uuid4().hex[:8]
    )
    run.add_argument("--out-dir", type=Path, default=Path("operations/evaluations"))
    run.add_argument(
        "--save-answers", action="store_true", help="Store local answer/context for human review"
    )
    human = commands.add_parser("calibrate")
    human.add_argument("--run-id", required=True)
    human.add_argument("--ratings", type=Path, required=True)
    human.add_argument("--out-dir", type=Path, default=Path("operations/evaluations"))
    args = parser.parse_args()
    try:
        if args.command == "run":
            asyncio.run(execute(args))
        else:
            store = EvaluationStore(args.out_dir)
            result = store.load_run(args.run_id)
            result.calibration = calibrate(result, args.ratings)
            store.update(result)
            print(
                f"Calibration: {result.calibration.status} "
                f"({result.calibration.unique_cases} cases)"
            )
    except (ValueError, KeyError, OSError, GatewayError, yaml.YAMLError) as exc:
        parser.exit(
            2, f"Evaluation failed: {type(exc).__name__}. Check config/data and output paths.\n"
        )


if __name__ == "__main__":
    main()
