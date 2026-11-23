"""Child process used by the force-termination durability test."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

from rope_arbiter.cache import ContentAddressedCache
from rope_arbiter.models import SnapshotStage, SnapshotStore, VariantSnapshot
from rope_arbiter.sample import build_sample_cases


def main() -> None:
    root = Path(sys.argv[1])
    ready_path = Path(sys.argv[2])
    cases = build_sample_cases()
    first = VariantSnapshot.from_cases(cases, 1)
    second = VariantSnapshot.with_overrides(
        first,
        2,
        {cases[0].case_id: {"scale_ratio": cases[0].scale_ratio * 2.0}},
    )
    stage = SnapshotStage(store=SnapshotStore(root / "snapshot_stage"), initial=first)
    stage.try_commit(second)

    cache = ContentAddressedCache(root / "cache", max_entries=100)
    access = cache.get_or_compute(
        case_id="durable-case",
        variant="rope",
        node="angles",
        key_components={"durable": True, "node": "angles"},
        snapshot_version=2,
        compute=lambda: (
            {"kind": "durable"},
            {"angles": np.arange(12.0, dtype=np.float64).reshape(6, 2)},
        ),
    )
    ready_path.write_text(
        json.dumps(
            {
                "snapshot_version": stage.snapshot().version,
                "snapshot_checksum": stage.snapshot().content_checksum,
                "artifact_hash": access.artifact_hash,
                "key": access.key,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    time.sleep(60)


if __name__ == "__main__":
    main()
