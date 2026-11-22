from __future__ import annotations

import json

from conftest import (
    ROOT,
    db_rows,
    manager_for,
    plan_v1,
    run_cli,
    shard_policy,
)


UNINTERRUPTED_V1_MANIFEST = (
    "bb3281a1e511651950d1c2f8c51e9a9e7c90000f34056b8dd5815bdea4be3304"
)


def _common(tmp_path):
    return (
        "--db",
        str(tmp_path / "ledger.sqlite3"),
        "--store",
        str(tmp_path / "store"),
    )


def test_forced_exit_after_two_shards_then_resume(tmp_path):
    process = run_cli(
        "save",
        "--plan",
        str(ROOT / "samples" / "training_plan.json"),
        "--shards",
        str(ROOT / "samples" / "shard_policy.json"),
        "--crash-plan",
        str(ROOT / "samples" / "crash_after_two_shards.json"),
        *_common(tmp_path),
    )
    assert process.returncode == 91
    builds = db_rows(tmp_path / "ledger.sqlite3", "SELECT * FROM builds")
    pointer = db_rows(tmp_path / "ledger.sqlite3", "SELECT * FROM pointer")
    commits = db_rows(
        tmp_path / "ledger.sqlite3", "SELECT * FROM shard_commits ORDER BY shard_id"
    )
    assert builds[0]["status"] == "writing"
    assert pointer[0]["version"] is None
    assert [item["shard_id"] for item in commits] == ["shard-0000", "shard-0001"]
    assert len(list((tmp_path / "store" / "objects").glob("*.shard"))) == 3

    visible = run_cli("restore", *_common(tmp_path))
    assert visible.returncode == 1
    assert json.loads(visible.stderr)["error"]["code"] == "CHECKPOINT_NOT_FOUND"
    crashed_status = manager_for(tmp_path).status()
    assert crashed_status["selected_version"] == 1
    assert crashed_status["selected_build"]["status"] == "writing"
    assert crashed_status["recovery_decision"]["decision"] == "resume_required"
    assert crashed_status["recovery_decision"]["committed_shards"] == [
        "shard-0000",
        "shard-0001",
    ]
    assert len(crashed_status["recovery_decision"]["missing_shards"]) == 2

    resumed = manager_for(tmp_path).resume()
    assert resumed["committed"] is True
    assert resumed["manifest_digest"] == UNINTERRUPTED_V1_MANIFEST
    assert resumed["summary"]["written"] == 1
    assert resumed["summary"]["hit"] == 3
    assert manager_for(tmp_path).verify()["verified"] is True


def test_partial_manifest_temp_is_cleaned_and_replayed(tmp_path):
    process = run_cli(
        "save",
        "--plan",
        str(ROOT / "samples" / "training_plan.json"),
        "--shards",
        str(ROOT / "samples" / "shard_policy.json"),
        "--crash-plan",
        str(ROOT / "samples" / "crash_during_manifest_write.json"),
        *_common(tmp_path),
    )
    assert process.returncode == 92
    temp_files = list((tmp_path / "store" / "manifests").glob(".tmp-v1-manifest-*"))
    assert len(temp_files) == 1
    assert temp_files[0].stat().st_size < 5020
    assert db_rows(tmp_path / "ledger.sqlite3", "SELECT version FROM pointer")[0][
        "version"
    ] is None

    resumed = manager_for(tmp_path).resume(plan_v1(), shard_policy())
    assert resumed["committed"] is True
    assert resumed["manifest_digest"] == UNINTERRUPTED_V1_MANIFEST
    assert not list((tmp_path / "store" / "manifests").glob(".tmp-v1-manifest-*"))
    assert manager_for(tmp_path).verify()["verified"] is True
