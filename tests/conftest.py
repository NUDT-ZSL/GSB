from __future__ import annotations

from pathlib import Path

import pytest

from multilabel_lab import (
    SnapshotStore,
    build_default_payloads,
    create_projection,
    default_config,
)


@pytest.fixture
def snapshot_factory(tmp_path: Path):
    counter = {"value": 0}

    def factory(*, sample_count: int = 8, annotation_seed: int = 7319, **config_kwargs):
        counter["value"] += 1
        config = default_config(**config_kwargs)
        projection = create_projection(config)
        payloads = build_default_payloads(
            config,
            sample_count=sample_count,
            annotation_seed=annotation_seed,
            projection=projection,
        )
        store = SnapshotStore(tmp_path / f"snapshot-{counter['value']}")
        return store.commit(
            payloads,
            versions={"config": 1, "projection": 1, "annotations": 1},
        )["snapshot"]

    return factory
