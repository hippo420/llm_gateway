"""Validated routing policy, published with the registry snapshot."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Literal["static", "weighted"] = "static"
    bucket_header: str = Field(
        default="X-Session-Id",
        min_length=1,
        max_length=128,
        pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$",
    )
