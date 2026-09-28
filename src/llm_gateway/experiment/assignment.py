import hashlib
import json
from dataclasses import dataclass

from .models import Experiment, Variant


@dataclass(frozen=True)
class Assignment:
    experiment: str
    variant: str
    deployment_id: str
    generation: str
    warmup: bool
    paused: bool


def choose(experiment_id: str, experiment: Experiment, bucket: str) -> Variant:
    variants = sorted((v for v in experiment.variants if v.weight > 0), key=lambda v: v.name)
    payload = json.dumps([experiment_id, bucket], ensure_ascii=False, separators=(",", ":"))
    number = int.from_bytes(hashlib.sha256(payload.encode()).digest(), "big")
    slot = number * sum(v.weight for v in variants) // (1 << 256)
    for variant in variants:
        if slot < variant.weight:
            return variant
        slot -= variant.weight
    raise AssertionError("experiment bucket outside weight range")
