from __future__ import annotations

import json
from pathlib import Path

from tsaudit.codes import Code
from tsaudit.engine import AuditEngine
from tsaudit.generator import OBSERVATION_CLOCK_MS
from tsaudit.trust_parse import parse_v1_snapshot


def test_generated_sample_has_required_files(sample_dir: Path) -> None:
    for relative in [
        "manifest.json",
        "signatures.json",
        "tokens.json",
        "truth.json",
        "sync.marker.json",
        "snapshots/v1/trust_v1.json",
        "snapshots/v2/meta.db",
        "snapshots/v2/shards/shard_0.json",
        "snapshots/v2/shards/shard_1.json",
    ]:
        assert (sample_dir / relative).is_file(), relative


def test_invalid_records_are_rejected_but_valid_records_continue(
    sample_dir: Path, workspace: Path
) -> None:
    report = AuditEngine(workspace).adjudicate_sample(sample_dir)
    codes = {item["code"] for item in report["rejections"]}
    expected = {
        Code.E_DIGEST_HEX,
        Code.E_DIGEST_LENGTH,
        Code.E_TIME_INTEGER,
        Code.E_DUP_ARTIFACT_ID,
        Code.E_ID_EMPTY,
        Code.E_TIME_WINDOW,
        Code.E_GENERATION,
        Code.E_DUP_TOKEN_ID,
    }
    assert expected <= codes
    for rejection in report["rejections"]:
        assert rejection["source_category"]
        assert rejection["record_seq"] >= 0
        assert rejection["code"]

    assert report["summary"]["artifact_count"] == 12
    assert report["summary"]["token_count"] == 14


def test_v1_relative_seconds_use_manifest_clock_not_system_time(
    sample_dir: Path, tmp_path: Path
) -> None:
    v1_path = tmp_path / "v1.json"
    v1_path.write_text(json.dumps({
        "snapshot_id": "v1-test",
        "trust_generation": 7,
        "relative_effective_seconds": 3,
        "issuers": ["TSA-X"],
        "revocations": [],
    }), encoding="utf-8")
    snapshot, records, errors = parse_v1_snapshot(v1_path, OBSERVATION_CLOCK_MS)
    assert errors == []
    assert snapshot is not None
    assert snapshot["effective_at_ms"] == OBSERVATION_CLOCK_MS + 3000
    assert records[0]["effective_at_ms"] == OBSERVATION_CLOCK_MS + 3000


def test_bad_json_file_is_a_rejection_not_a_crash(tmp_path: Path) -> None:
    bad = tmp_path / "signatures.json"
    bad.write_text("{not json", encoding="utf-8")
    engine = AuditEngine(tmp_path / "state")
    report = engine.import_signatures(bad)
    assert report["rejections"][0]["code"] == Code.E_PARSE_JSON
    assert report["artifacts"] == []
