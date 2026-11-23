from __future__ import annotations

import json
import shutil
from pathlib import Path

from transformer_fusion import execute


def test_strict_resource_limit_and_separate_fallback(dataset, stores):
    data_dir, manifest = dataset
    strict = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"] / "strict",
        snapshot_dir=stores["snapshot_dir"] / "strict",
        limits={"max_resident_bytes": 1, "strict": True},
        case_ids=["case_gelu_perm"],
        run_selfcheck=False,
    )
    assert not strict.ok
    assert any(
        diagnostic.code == "E_RESOURCE_LIMIT" and diagnostic.stage == "resource_plan"
        for diagnostic in strict.diagnostics
    )

    fallback = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"] / "fallback",
        snapshot_dir=stores["snapshot_dir"] / "fallback",
        limits={"max_blocks": 1, "allow_block_expand": False},
        case_ids=["case_gelu_perm"],
    )
    assert fallback.ok
    case = fallback.cases[0]
    assert case.plan.path == "separate"
    assert case.plan.fallback_category == "block_count"
    assert case.plan.upstream_code == "E_RESOURCE_LIMIT"
    assert case.equivalence["ok"]
    assert case.snapshot["replacement"] == "full"


def test_snapshot_versions_checksums_and_corruption(dataset, stores):
    data_dir, manifest = dataset
    case_id = "case_gelu_perm"
    first = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
        case_ids=[case_id],
    )
    second = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
        case_ids=[case_id],
    )
    first_snapshot = first.cases[0].snapshot
    second_snapshot = second.cases[0].snapshot
    assert second_snapshot["version"] > first_snapshot["version"]
    assert first_snapshot["checksum"] and second_snapshot["checksum"]
    assert first_snapshot["plan_checksum"] and second_snapshot["plan_checksum"]
    assert second_snapshot["replacement"] == "full"

    corrupt_root = stores["snapshot_dir"] / "corrupt"
    shutil.copytree(stores["snapshot_dir"], corrupt_root)
    index = json.loads((corrupt_root / "index.json").read_text(encoding="utf-8"))
    entry = index["cases"][case_id]
    snapshot_file = corrupt_root / entry["snapshot"]
    snapshot_file.write_bytes(snapshot_file.read_bytes()[:-5] + b"BROKEN")

    corrupt = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"] / "corrupt-cache",
        snapshot_dir=corrupt_root,
        case_ids=[case_id],
        run_selfcheck=False,
    )
    assert not corrupt.ok
    assert any(
        diagnostic.code == "E_SNAPSHOT_CORRUPT"
        and diagnostic.case_id == case_id
        and diagnostic.stage == "snapshot"
        for diagnostic in corrupt.diagnostics
    )
