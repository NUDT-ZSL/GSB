from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from deploy_convergence.errors import EngineError


def _write_yaml(path: Path, payload: dict) -> None:
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def test_v1_duplicate_service_is_rejected_without_state(
    copied_samples: Path,
    engine_factory,
    clock_ms: int,
    tmp_path: Path,
) -> None:
    v1_path = copied_samples / "v1" / "manifest.yaml"
    payload = yaml.safe_load(v1_path.read_text(encoding="utf-8"))
    duplicate = copy.deepcopy(payload["environments"]["development"]["services"][0])
    payload["environments"]["development"]["services"].append(duplicate)
    _write_yaml(v1_path, payload)
    engine = engine_factory(
        v1_path=v1_path,
        state_name="invalid-duplicate.sqlite",
    )
    with pytest.raises(EngineError) as raised:
        engine.run(clock_ms=clock_ms)
    assert raised.value.code == "E_BAD_MANIFEST"
    assert raised.value.details["environment"] == "development"
    assert not (tmp_path / "invalid-duplicate.sqlite").exists()


def test_v1_flat_params_must_match_service_fields(
    copied_samples: Path,
    engine_factory,
    clock_ms: int,
    tmp_path: Path,
) -> None:
    v1_path = copied_samples / "v1" / "manifest.yaml"
    payload = yaml.safe_load(v1_path.read_text(encoding="utf-8"))
    payload["environments"]["development"]["services"][0]["timeout_ms"] = -9
    _write_yaml(v1_path, payload)
    engine = engine_factory(
        v1_path=v1_path,
        state_name="invalid-flat.sqlite",
    )
    with pytest.raises(EngineError) as raised:
        engine.run(clock_ms=clock_ms)
    assert raised.value.code == "E_BAD_MANIFEST"
    assert raised.value.details["field"] == "timeout_ms"
    assert not (tmp_path / "invalid-flat.sqlite").exists()


def test_v2_digest_mismatch_is_rejected_without_state(
    copied_samples: Path,
    engine_factory,
    clock_ms: int,
    tmp_path: Path,
) -> None:
    shard = copied_samples / "v2" / "shards" / "development-core.json"
    shard.write_bytes(shard.read_bytes() + b"\n")
    engine = engine_factory(
        v2_meta=copied_samples / "v2" / "meta.sqlite",
        v2_shards=copied_samples / "v2" / "shards",
        state_name="invalid-digest.sqlite",
    )
    with pytest.raises(EngineError) as raised:
        engine.run(clock_ms=clock_ms)
    assert raised.value.code == "E_DIGEST_MISMATCH"
    assert raised.value.details["shard_id"] == "development-core"
    assert raised.value.details["expected"] != raised.value.details["actual"]
    assert not (tmp_path / "invalid-digest.sqlite").exists()


def test_v2_parameter_type_is_validated_after_digest(
    copied_samples: Path,
    engine_factory,
    clock_ms: int,
) -> None:
    meta_path = copied_samples / "v2" / "meta.sqlite"
    shard = copied_samples / "v2" / "shards" / "development-core.json"
    payload = json.loads(shard.read_text(encoding="utf-8"))
    payload["services"][0]["params"]["timeout_ms"] = "850"
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    shard.write_bytes(encoded)
    import hashlib

    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    connection = sqlite3.connect(meta_path)
    try:
        connection.execute(
            "UPDATE shards SET digest=? WHERE shard_id='development-core'",
            (digest,),
        )
        connection.commit()
    finally:
        connection.close()
    engine = engine_factory(
        v2_meta=meta_path,
        v2_shards=copied_samples / "v2" / "shards",
        state_name="invalid-type.sqlite",
    )
    with pytest.raises(EngineError) as raised:
        engine.run(clock_ms=clock_ms)
    assert raised.value.code == "E_BAD_PARAM_TYPE"
    assert raised.value.details["parameter"] == "timeout_ms"


def test_v2_generation_below_minimum_is_rejected(
    copied_samples: Path,
    engine_factory,
    clock_ms: int,
) -> None:
    meta_path = copied_samples / "v2" / "meta.sqlite"
    connection = sqlite3.connect(meta_path)
    try:
        connection.execute(
            "UPDATE shards SET generation=10 WHERE shard_id='development-core'"
        )
        connection.commit()
    finally:
        connection.close()
    engine = engine_factory(
        v2_meta=meta_path,
        v2_shards=copied_samples / "v2" / "shards",
        state_name="invalid-generation.sqlite",
    )
    with pytest.raises(EngineError) as raised:
        engine.run(clock_ms=clock_ms)
    assert raised.value.code == "E_GENERATION_CONFLICT"
    assert raised.value.details["generation_min"] == 11
