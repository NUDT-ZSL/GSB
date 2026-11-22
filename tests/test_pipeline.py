from __future__ import annotations

import json
from pathlib import Path

import pytest

from prefworkbench.canonical import read_json, sealed, verify_sealed
from prefworkbench.config import load_config, validate_config
from prefworkbench.errors import (
    E_CHECKSUM_MISMATCH,
    E_STALE_RESULT,
    E_VERSION_REGRESSION,
    E_WORKER_FAILURE,
    BusinessError,
)
from prefworkbench.generator import default_config, write_sample
from prefworkbench.pipeline import run_pipeline
from prefworkbench.snapshots import SnapshotStore, publish_run_manifest


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            )


def test_pipeline_reuse_incremental_and_byte_identity(tmp_path: Path) -> None:
    data_path = tmp_path / "sample.jsonl"
    config_path = tmp_path / "config.json"
    write_sample(data_path, config_path, count=40, seed=20261123)

    baseline = run_pipeline(
        input_path=data_path,
        config_path=config_path,
        out_dir=tmp_path / "baseline",
        cache_root=tmp_path / "cache",
    )
    repeated = run_pipeline(
        input_path=data_path,
        config_path=config_path,
        out_dir=tmp_path / "repeat",
        cache_root=tmp_path / "cache",
    )
    assert baseline["manifest_file_sha256"] == repeated["manifest_file_sha256"]
    assert all(
        repeated["diagnostics"]["stage_audit"][stage]["status"] == "reused"
        for stage in ("quality", "neardup", "consistency", "sampling", "manifest")
    )
    assert repeated["manifest_hit"] is True

    rows = [
        json.loads(line)
        for line in data_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    rows[0]["chosen"] += " corrected-annotation-detail"
    modified_path = tmp_path / "modified.jsonl"
    _write_jsonl(modified_path, rows)

    incremental = run_pipeline(
        input_path=modified_path,
        config_path=config_path,
        out_dir=tmp_path / "incremental",
        cache_root=tmp_path / "cache",
    )
    full = run_pipeline(
        input_path=modified_path,
        config_path=config_path,
        out_dir=tmp_path / "full",
        cache_root=tmp_path / "full-cache",
        cold_cache=True,
    )
    assert incremental["manifest_file_sha256"] == full["manifest_file_sha256"]
    assert (
        (tmp_path / "incremental" / "manifest.json").read_bytes()
        == (tmp_path / "full" / "manifest.json").read_bytes()
    )
    quality_audit = incremental["diagnostics"]["stage_audit"]["quality"]
    assert quality_audit["status"] == "partially_recomputed"
    assert len(quality_audit["recomputed_pair_ids"]) == 1
    assert len(quality_audit["reused_pair_ids"]) == 39
    assert set(quality_audit["recomputed_pair_ids"]) & set(
        quality_audit["reused_pair_ids"]
    ) == set()
    assert (
        incremental["diagnostics"]["stage_audit"]["quality"]["status"]
        == "partially_recomputed"
    )
    assert "neardup" in incremental["diagnostics"]["incremental"]["recomputed_stages"]
    assert incremental["manifest"]["selfcheck"]["ok"] is True


def test_config_snapshot_stale_regression_and_tamper(tmp_path: Path) -> None:
    data_path = tmp_path / "sample.jsonl"
    config_path = tmp_path / "config.json"
    write_sample(data_path, config_path, count=24, seed=20261123)
    result = run_pipeline(
        input_path=data_path,
        config_path=config_path,
        out_dir=tmp_path / "run",
        cache_root=tmp_path / "cache",
    )
    manifest_raw = read_json(tmp_path / "run" / "manifest.json")
    manifest_payload = verify_sealed(manifest_raw, context="manifest")

    config_v1 = load_config(config_path)
    changed_payload = json.loads(json.dumps(config_v1.payload))
    changed_payload["quality"]["weights"]["length"] = 0.31
    changed_payload["quality"]["weights"]["repetition"] = 0.34
    same_version_changed = validate_config(changed_payload)
    store = SnapshotStore(tmp_path / "cache" / "snapshots")
    with pytest.raises(BusinessError) as error:
        store.publish(same_version_changed)
    assert error.value.code == E_CHECKSUM_MISMATCH

    config_v2_payload = json.loads(json.dumps(config_v1.payload))
    config_v2_payload["version"] = 2
    config_v2_payload["neardup"]["threshold"] = 0.70
    config_v2 = validate_config(config_v2_payload)
    store.publish(config_v2)
    with pytest.raises(BusinessError) as stale:
        store.assert_fresh(manifest_payload, config_v2)
    assert stale.value.code == E_STALE_RESULT

    with pytest.raises(BusinessError) as regression:
        store.publish(config_v1)
    assert regression.value.code == E_VERSION_REGRESSION

    tampered = dict(manifest_raw)
    tampered["config"] = dict(tampered["config"])
    tampered["config"]["version"] = 999
    with pytest.raises(BusinessError) as tamper:
        verify_sealed(tampered, context="tampered manifest")
    assert tamper.value.code == E_CHECKSUM_MISMATCH

    guard_path = tmp_path / "guard" / "manifest.json"
    v2_payload = json.loads(json.dumps(manifest_payload))
    v2_payload["config"]["version"] = 2
    v2_payload["config"]["checksum"] = config_v2.checksum
    publish_run_manifest(guard_path, sealed(v2_payload))
    rollback_payload = json.loads(json.dumps(manifest_payload))
    rollback_payload["config"]["version"] = 1
    rollback_payload["config"]["checksum"] = config_v1.checksum
    rollback = sealed(rollback_payload)
    with pytest.raises(BusinessError) as rollback_error:
        publish_run_manifest(guard_path, rollback)
    assert rollback_error.value.code == E_VERSION_REGRESSION


def test_worker_failure_is_attributed_to_pair_stage_and_batch(tmp_path: Path) -> None:
    data_path = tmp_path / "sample.jsonl"
    config_path = tmp_path / "config.json"
    write_sample(data_path, config_path, count=24, seed=20261123)
    rows = [
        json.loads(line)
        for line in data_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    failing = dict(rows[0])
    failing["pair_id"] = "P99999"
    failing["__fail_stage__"] = "quality"
    rows.append(failing)
    _write_jsonl(data_path, rows)
    result = run_pipeline(
        input_path=data_path,
        config_path=config_path,
        out_dir=tmp_path / "run",
        cache_root=tmp_path / "cache",
    )
    records = {
        record["pair_id"]: record for record in result["manifest"]["records"]
    }
    error = records["P99999"]["admission"]
    assert error["code"] == E_WORKER_FAILURE
    assert error["stage"] == "quality"
    assert error["batch_id"] >= 0
    assert error["execution_error"]["pair_ids"] == ["P99999"]
    assert result["manifest"]["selfcheck"]["ok"] is True
