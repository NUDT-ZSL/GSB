from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from cdn_edge_workbench.engine import WorkbenchEngine
from cdn_edge_workbench.errors import BusinessError, ErrorCode
from cdn_edge_workbench.models import CacheEntry
from cdn_edge_workbench.snapshots import load_samples
from cdn_edge_workbench.validation import (
    enforce_cache_generation_monotonic,
    normalize_path,
    parse_cache_control,
    parse_etag,
)


def test_path_normalization_rejects_traversal() -> None:
    assert normalize_path("/assets//app.js") == "/assets/app.js"
    assert normalize_path("/assets/./app.js") == "/assets/app.js"
    with pytest.raises(BusinessError) as error:
        normalize_path("/assets/../secret")
    assert error.value.code is ErrorCode.PATH_TRAVERSAL
    with pytest.raises(BusinessError) as error:
        normalize_path("/assets/%2e%2e/secret")
    assert error.value.code is ErrorCode.PATH_TRAVERSAL
    with pytest.raises(BusinessError):
        normalize_path("assets/app.js")


def test_etag_must_match_body_digest() -> None:
    body = b"hello"
    algorithm, digest, etag, weak = parse_etag(
        '"sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"',
        declared_algorithm="sha256",
        body=body,
    )
    assert (algorithm, weak) == ("sha256", False)
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    digest_hex = digest
    for candidate in (
        f'"sha256-{digest_hex}"',
        f'"{digest_hex}"',
        f'W/"sha256:{digest_hex}"',
    ):
        parsed_algorithm, parsed_digest, _etag, weak = parse_etag(
            candidate,
            declared_algorithm="sha256",
            body=body,
        )
        assert parsed_algorithm == "sha256"
        assert parsed_digest == digest_hex
        assert weak is candidate.startswith("W/")
    with pytest.raises(BusinessError) as error:
        parse_etag('"sha256:0000"', declared_algorithm="sha256", body=body)
    assert error.value.code in {
        ErrorCode.ETAG_DIGEST_MISMATCH,
        ErrorCode.ETAG_INVALID,
    }


def test_cache_control_effective_ttl_is_minimum() -> None:
    parsed = parse_cache_control({"max_age": 60, "s_maxage": 20})
    assert parsed.effective_ttl_seconds == 20
    assert parsed.max_age == 60
    assert parsed.s_maxage == 20
    with pytest.raises(BusinessError) as error:
        parse_cache_control({"max_age": -1})
    assert error.value.code is ErrorCode.CACHE_CONTROL_INVALID


def test_cache_generation_rollback_is_rejected() -> None:
    old = CacheEntry(
        node="edge-a",
        path="/a",
        digest_algorithm="sha256",
        content_digest="1" * 64,
        content_version=2,
        etag='"sha256:' + "1" * 64 + '"',
        last_verified_clock=10,
        expires_at_clock=20,
        cache_generation=2,
        source_version="v1",
        source_ref="v1",
        raw_time={},
        effective_ttl_seconds=10,
    )
    new = old.with_changes(source_version="v2", cache_generation=1)
    with pytest.raises(BusinessError) as error:
        enforce_cache_generation_monotonic([old], [new])
    assert error.value.code is ErrorCode.CACHE_GENERATION_ROLLBACK


def test_invalid_injection_writes_no_state(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    sample_copy = tmp_path / "samples"
    shutil.copytree(samples_dir, sample_copy)
    injection_path = sample_copy / "injections.json"
    payload = json.loads(injection_path.read_text(encoding="utf-8"))
    payload["injections"][0]["path"] = "/missing/resource.json"
    injection_path.write_text(json.dumps(payload), encoding="utf-8")
    state = tmp_path / "state"
    engine = WorkbenchEngine(samples_dir=sample_copy, state_dir=state)
    with pytest.raises(BusinessError) as error:
        engine.run()
    assert error.value.code is ErrorCode.INJECTION_REFERENCE_MISSING
    assert not state.exists()


def test_shard_hash_mismatch_is_rejected(
    tmp_path: Path,
    samples_dir: Path,
) -> None:
    sample_copy = tmp_path / "samples"
    shutil.copytree(samples_dir, sample_copy)
    shard = sample_copy / "v2" / "shards" / "edge-a.json"
    shard.write_text(shard.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(BusinessError) as error:
        load_samples(sample_copy)
    assert error.value.code is ErrorCode.SHARD_HASH_MISMATCH


def test_v1_relative_time_records_conversion(samples_dir: Path) -> None:
    loaded = load_samples(samples_dir)
    entry = next(
        item
        for item in loaded.snapshot_candidates
        if item.node == "edge-a"
        and item.path == "/assets/app.js"
        and item.source_version == "v1"
    )
    assert entry.last_verified_clock == 980
    assert entry.expires_at_clock == 998
    conversion = entry.raw_time["last_verified_conversion"]
    assert conversion["scale_seconds_per_tick"] == 10
    assert conversion["rounding"] == "ceil"
    assert conversion["raw"] == 200
    assert conversion["converted_ticks"] == 20


def test_v2_metadata_is_sqlite(samples_dir: Path) -> None:
    connection = sqlite3.connect(samples_dir / "v2_meta.sqlite")
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        shards = list(connection.execute("SELECT shard_file FROM shards ORDER BY shard_index"))
    finally:
        connection.close()
    assert metadata["snapshot_generation"] == "2"
    assert len(shards) == 3

