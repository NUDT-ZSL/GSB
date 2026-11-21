from __future__ import annotations

from pathlib import Path

import pytest

from driftmon.alerts import AlertStore
from driftmon.cache import CacheStore
from driftmon.config import default_config


@pytest.fixture()
def runtime(tmp_path: Path):
    config = default_config()
    cache = CacheStore(
        tmp_path / "cache",
        max_entries=config.limits.max_cache_entries,
        resource_policy=config.limits.resource_policy,
    )
    alerts = AlertStore(tmp_path / "alerts")
    return config, cache, alerts

