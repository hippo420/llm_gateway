"""Fixed Phase 2 measurements; zero baselines use deltas, never fabricated ratios."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, field_validator

from .signals import SIGNAL_NAMES, SignalSnapshot


class FixedBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, float]

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: dict[str, float]) -> dict[str, float]:
        if not values or values.keys() - SIGNAL_NAMES:
            raise ValueError("baseline requires known signal names")
        if any(not math.isfinite(v) or v < 0 for v in values.values()):
            raise ValueError("baseline must contain finite nonnegative measurements")
        return values

    def normalize(self, snapshot: SignalSnapshot) -> dict[str, float | None]:
        values = snapshot.values()
        for name, value in snapshot.values().items():
            base = self.values.get(name)
            if value is not None and math.isfinite(value) and base is not None:
                values[f"{name}_ratio"] = value / base if base > 0 else None
                values[f"{name}_delta"] = value - base
            else:
                values[f"{name}_ratio"] = None
                values[f"{name}_delta"] = None
        return values
