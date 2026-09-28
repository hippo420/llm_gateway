import hashlib
import json
from dataclasses import dataclass

from .config import DatasetSpec
from .models import Case, DatasetInfo


@dataclass(frozen=True)
class Dataset:
    info: DatasetInfo
    cases: list[Case]


def load_dataset(spec: DatasetSpec) -> Dataset:
    if spec.path.stat().st_size > 20_000_000:
        raise ValueError("dataset exceeds 20 MB")
    data = spec.path.read_bytes()
    cases = [
        Case.model_validate(json.loads(line))
        for line in data.decode("utf-8-sig").splitlines()
        if line.strip()
    ]
    if not cases or len(cases) > 1000:
        raise ValueError("dataset requires 1 to 1000 cases")
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate case ID")
    if len({case.request_type for case in cases}) != 1:
        raise ValueError("each dataset must contain exactly one request_type")
    return Dataset(
        DatasetInfo(
            name=spec.name,
            sha256=hashlib.sha256(data).hexdigest(),
            provenance=spec.provenance,
            request_type=cases[0].request_type,
            cases=len(cases),
        ),
        cases,
    )
