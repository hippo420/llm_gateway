from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ..registry.models import TimeoutConfig


def http_timeout(config: "TimeoutConfig") -> httpx.Timeout:
    """HTTP read is an idle interval; the policy separately caps the entire request."""
    return httpx.Timeout(
        connect=config.connect,
        read=config.read,
        write=config.total,
        pool=config.connect,
    )
