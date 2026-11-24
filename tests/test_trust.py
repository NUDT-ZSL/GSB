from __future__ import annotations

import json
import shutil
from pathlib import Path

from tlsdiag import codes
from tlsdiag.models import normalize_fingerprint
from tlsdiag.trust import (
    diff_trust_views,
    load_trust_view,
    merge_trust_records,
    parse_v1_snapshot,
    parse_v2_snapshot,
)


FP_A = "sha256:" + "a" * 64
FP_B = "sha256:" + "b" * 64
FP_C = "sha256:" + "c" * 64


def _v1(anchors, epoch_ms=1_000, version="v1-test") -> bytes:
    return json.dumps(
        {
            "format": "trust-v1",
            "snapshot_version": version,
            "epoch_ms": epoch_ms,
            "anchors": anchors,
        }
    ).encode()


def test_v1_relative_trust_seconds_and_record_validation() -> None:
    data = _v1(
        [
            {
                "alias": "Root Alpha",
                "fingerprint": FP_A,
                "trust_seconds": 3,
                "enabled": True,
            },
            {"alias": "", "fingerprint": FP_A, "trust_seconds": 0, "enabled": True},
            {"alias": "bad", "fingerprint": "nope", "trust_seconds": 0, "enabled": True},
            {"alias": "bad2", "fingerprint": FP_B, "trust_seconds": -2, "enabled": True},
            {"alias": "bad3", "fingerprint": FP_B, "trust_seconds": 0, "enabled": "maybe"},
        ]
    )
    records, versions, summary, diagnostics = parse_v1_snapshot(data, "v1.json")
    assert len(records) == 1
    assert records[0].effective_ms == 4_000
    assert records[0].source_generation == "v1"
    assert summary["record_count"] == 1
    found = {d.code for d in diagnostics}
    assert {
        codes.E_V1_ALIAS,
        codes.E_V1_FP,
        codes.E_V1_TRUST,
        codes.E_V1_ENABLED,
    } <= found
    assert all(d.source_file == "v1.json" and d.record_index for d in diagnostics)


def test_merge_uses_generation_then_effective_time_and_reports_conflict() -> None:
    v1 = _v1(
        [
            {
                "alias": "Root Alpha",
                "fingerprint": FP_A,
                "trust_seconds": 0,
                "enabled": True,
            }
        ]
    )
    v2_records = [
        {
            "canonical_alias": "root-alpha.example",
            "fingerprint": FP_A,
            "effective_ms": 2_000,
            "generation": 2,
            "tombstone": False,
            "revoked": False,
        }
    ]
    v2_doc = json.dumps({"records": v2_records}).encode()
    # Use the documented shard parser through a temporary directory.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        shard = Path(tmp) / "part-000.json"
        shard.write_bytes(v2_doc)
        v2_parsed, versions, summary, diagnostics = parse_v2_snapshot(Path(tmp), None)
    v1_parsed, *_ = parse_v1_snapshot(v1, "v1.json")
    view = merge_trust_records(
        v1_parsed,
        v2_parsed,
        diagnostics=diagnostics,
        source_versions={**versions, "v1": "v1-test"},
    )
    entity = view.entities[FP_A]
    assert entity.generation == 2
    assert entity.effective_ms == 2_000
    assert entity.canonical_alias == "root-alpha.example"
    assert entity.authoritative_source.startswith("v2:")
    assert view.conflicts
    assert set(view.conflicts[0].fields) >= {"effective_ms", "generation"}


def test_tombstone_covers_earlier_non_tombstone() -> None:
    v1 = _v1(
        [
            {
                "alias": "Root Alpha",
                "fingerprint": FP_A,
                "trust_seconds": 0,
                "enabled": True,
            }
        ]
    )
    v2_records = [
        {
            "canonical_alias": "root-alpha.example",
            "fingerprint": FP_A,
            "effective_ms": 1_500,
            "generation": 2,
            "tombstone": True,
            "revoked": False,
        }
    ]
    with __import__("tempfile").TemporaryDirectory() as tmp:
        (Path(tmp) / "part-000.json").write_text(json.dumps({"records": v2_records}))
        v2_parsed, _, _, diagnostics = parse_v2_snapshot(Path(tmp), None)
    v1_parsed, *_ = parse_v1_snapshot(v1, "v1.json")
    view = merge_trust_records(v1_parsed, v2_parsed, diagnostics=diagnostics)
    entity = view.entities[FP_A]
    assert entity.tombstone
    assert not entity.trusted
    assert entity.tombstone_covers
    assert any(source.startswith("v1:") for source in entity.tombstone_covers)


def test_version_changes_when_revocation_changes_and_import_order_is_irrelevant() -> None:
    v1 = _v1(
        [
            {
                "alias": "Root Alpha",
                "fingerprint": FP_A,
                "trust_seconds": 0,
                "enabled": True,
            }
        ]
    )
    records = [
        {
            "canonical_alias": "root-alpha.example",
            "fingerprint": FP_A,
            "effective_ms": 2_000,
            "generation": 2,
            "tombstone": False,
            "revoked": False,
        },
        {
            "canonical_alias": "root-beta.example",
            "fingerprint": FP_B,
            "effective_ms": 2_000,
            "generation": 1,
            "tombstone": False,
            "revoked": False,
        },
    ]
    with __import__("tempfile").TemporaryDirectory() as tmp:
        (Path(tmp) / "part-001.json").write_text(json.dumps({"records": [records[1]]}))
        (Path(tmp) / "part-000.json").write_text(json.dumps({"records": [records[0]]}))
        v2_a, _, _, diagnostics_a = parse_v2_snapshot(Path(tmp), None)
        v2_rev = [dict(records[0], revoked=True), records[1]]
        (Path(tmp) / "part-000.json").write_text(json.dumps({"records": [v2_rev[0]]}))
        v2_b, _, _, diagnostics_b = parse_v2_snapshot(Path(tmp), None)
    v1_parsed, *_ = parse_v1_snapshot(v1, "v1.json")
    view_a = merge_trust_records(v1_parsed, v2_a, diagnostics=diagnostics_a)
    view_b = merge_trust_records(list(reversed(v1_parsed)), list(reversed(v2_b)), diagnostics=diagnostics_b)
    assert view_a.version != view_b.version
    assert view_b.entities[FP_A].revoked
    view_a_reordered = merge_trust_records(list(reversed(v1_parsed)), list(reversed(v2_a)))
    assert view_a.version == view_a_reordered.version
    diff = diff_trust_views(view_a.trusted_summary(), view_a_reordered)
    assert not diff["replaced"]


def test_v2_metadata_and_shard_checksum(sample_dir: Path, tmp_path: Path) -> None:
    copied = tmp_path / "sample"
    shutil.copytree(sample_dir / "input", copied)
    shards = copied / "trust_v2"
    db = copied / "trust_v2.sqlite3"
    view = load_trust_view(
        v1_bytes=(copied / "trust_v1.json").read_bytes(),
        v1_source=str(copied / "trust_v1.json"),
        v2_shards_dir=shards,
        v2_db_path=db,
    )
    assert view.source_summaries["v2"]["manifest_count"] == 3
    assert view.trusted
    # Tamper with one shard: it must be rejected with a source-specific code,
    # while the other shards and the valid entries in them remain available.
    target = sorted(shards.glob("*.json"))[0]
    target.write_bytes(target.read_bytes() + b"\n")
    tampered = load_trust_view(v1_bytes=None, v2_shards_dir=shards, v2_db_path=db)
    assert any(d.code == codes.E_V2_SHARD and d.source_file == str(target) for d in tampered.diagnostics)
    assert tampered.source_summaries["v2"]["record_count"] < view.source_summaries["v2"]["record_count"]


def test_normalize_fingerprint() -> None:
    assert normalize_fingerprint("sha256:" + "A" * 64) == FP_A
    assert normalize_fingerprint("a" * 64) == FP_A
    assert normalize_fingerprint("sha256:1234") is None
    assert normalize_fingerprint(123) is None


def test_snapshot_version_changes_when_trusted_set_changes(sample_dir: Path) -> None:
    v1_bytes = (sample_dir / "input/trust_v1.json").read_bytes()
    original = load_trust_view(
        v1_bytes=v1_bytes,
        v1_source="v1",
        v2_shards_dir=sample_dir / "input/trust_v2",
        v2_db_path=sample_dir / "input/trust_v2.sqlite3",
    )
    document = json.loads(v1_bytes)
    document["anchors"].append(
        {
            "alias": "Root Gamma",
            "fingerprint": FP_C,
            "trust_seconds": 0,
            "enabled": True,
        }
    )
    changed = load_trust_view(
        v1_bytes=json.dumps(document).encode(),
        v1_source="v1",
        v2_shards_dir=sample_dir / "input/trust_v2",
        v2_db_path=sample_dir / "input/trust_v2.sqlite3",
    )
    assert original.version != changed.version
    diff = diff_trust_views(original.trusted_summary(), changed)
    assert diff["replaced"]
    assert FP_C in diff["added"]


def test_v2_boolean_markers_are_strict(tmp_path: Path) -> None:
    records = [
        {
            "canonical_alias": "root-alpha.example",
            "fingerprint": FP_A,
            "effective_ms": 1000,
            "generation": 1,
            "tombstone": "true",
            "revoked": False,
        }
    ]
    (tmp_path / "part.json").write_text(json.dumps({"records": records}), encoding="utf-8")
    parsed, _, _, diagnostics = parse_v2_snapshot(tmp_path, None)
    assert parsed == []
    assert codes.E_V2_TOMBSTONE in {d.code for d in diagnostics}
