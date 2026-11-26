from __future__ import annotations

import json

import pytest

from telemetry_drift.errors import DriftError, E_VERSION_UNSUPPORTED
from telemetry_drift.snapshots import (
    PredictorModel,
    SnapshotBundle,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_snapshot,
    snapshot_error_replay,
    write_v2,
)


def test_v1_v2_current_predictor_is_equivalent(data_dir):
    v1 = read_snapshot(data_dir / "predictor_v1.json")
    v2 = read_snapshot(data_dir / "predictor_v2.json")
    assert v1.version == "v1"
    assert v2.version == "v2"
    assert v1.current.coefficients == v2.current.coefficients
    assert v1.current.intercept == v2.current.intercept
    assert v2.tombstones


def test_upgrade_fills_metadata_and_preserves_predictor(tmp_path, data_dir):
    target = tmp_path / "upgrade.json"
    result = migrate_v1_to_v2(
        data_dir / "predictor_v1.json",
        target,
        epoch_ms=1_700_000_000_000,
        lock_path=tmp_path / "commit.lock",
    )
    bundle = read_snapshot(target)
    original = read_snapshot(data_dir / "predictor_v1.json")
    assert bundle.version == "v2"
    assert bundle.current.coefficients == original.current.coefficients
    assert bundle.current.created_ms == 1_700_000_000_000
    assert any(item.field == "created_ms" for item in result.completions)
    assert bundle.completions


def test_downgrade_emits_explicit_incremental_drop_records(tmp_path, data_dir):
    target = tmp_path / "downgrade.json"
    result = migrate_v2_to_v1(
        data_dir / "predictor_v2.json",
        target,
        lock_path=tmp_path / "commit.lock",
    )
    assert result.target_version == "v1"
    assert result.drops
    assert result.drops[0].reason == "E_INCREMENTAL_RETRAIN_DROPPED"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["dropped_incremental_generations"]


def test_unknown_snapshot_version_returns_stable_error(tmp_path):
    path = tmp_path / "unknown.json"
    path.write_text('{"version":"v99"}\n', encoding="utf-8")
    replay = snapshot_error_replay(path)
    assert replay["ok"] is False
    assert replay["code"] == E_VERSION_UNSUPPORTED
    with pytest.raises(DriftError) as raised:
        read_snapshot(path)
    assert raised.value.code == E_VERSION_UNSUPPORTED


def test_downgrade_of_only_incremental_model_still_records_drop(tmp_path):
    model = PredictorModel(
        predictor_id="incremental-only",
        model_type="linear",
        feature_names=("x",),
        coefficients=(1.5,),
        intercept=0.2,
        config_generation=3,
        retrain_generation=2,
        created_ms=1000,
        trained_until_ms=2000,
        origin="snapshot",
    )
    source = tmp_path / "incremental-v2.json"
    write_v2(
        SnapshotBundle(
            version="v2",
            current=model,
            models=(model,),
            tombstones=(),
            completions=(),
            created_ms=1000,
        ),
        source,
        lock_path=tmp_path / "commit.lock",
    )
    target = tmp_path / "incremental-v1.json"
    result = migrate_v2_to_v1(
        source, target, lock_path=tmp_path / "commit.lock"
    )
    assert len(result.drops) == 1
    assert result.drops[0].retrain_generation == 2
    downgraded = read_snapshot(target)
    assert downgraded.current.retrain_generation == 0
    assert downgraded.current.coefficients == (1.5,)

