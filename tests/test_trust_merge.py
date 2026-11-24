from __future__ import annotations

from pathlib import Path

from certpath.engine import CertPathEngine, parse_sample
from certpath.generator import ROOT_ALPHA, ROOT_BETA, ROOT_PARTIAL
from certpath.model import V2AnchorRecord, V2Snapshot
from certpath.trust import build_trust_view


def test_v1_clock_is_converted_without_system_time(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    report = CertPathEngine(tmp_path / "state").adjudicate(sample_dir)
    quantization = report["snapshot"]["v1_quantization"]
    assert quantization["observation_clock_ms"] == 1_700_000_000_000
    assert quantization["relative_effective_seconds"] == 7
    assert quantization["lower_ms"] == 1_700_000_007_000
    assert quantization["upper_ms"] == 1_700_000_007_999
    assert quantization["max_error_ms"] == 999
    assert report["snapshot"]["digest"]


def test_cross_generation_conflicts_are_pending_not_silently_resolved(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    report = CertPathEngine(tmp_path / "state").adjudicate(sample_dir)
    conflicts = report["snapshot"]["conflicts"]
    pairs = {(item["fingerprint"], item["field"]) for item in conflicts}
    assert (ROOT_ALPHA, "trusted") in pairs
    assert (ROOT_ALPHA, "path_length") in pairs
    assert (ROOT_PARTIAL, "path_length") in pairs
    anchors = {
        item["fingerprint"]: item for item in report["trust_view"]["anchors"]
    }
    assert anchors[ROOT_ALPHA]["source"] == "v2"
    assert anchors[ROOT_ALPHA]["generation"] == 2
    assert anchors[ROOT_ALPHA]["trusted"] is False
    assert anchors[ROOT_ALPHA]["tombstone"] is True
    assert anchors[ROOT_ALPHA]["pending_confirmation"] is True
    assert anchors[ROOT_BETA]["source"] == "v2"
    assert anchors[ROOT_BETA]["pending_confirmation"] is False


def test_tombstone_covers_older_non_tombstone_in_same_source(
    sample_dir: Path,
) -> None:
    parsed = parse_sample(sample_dir)
    active = V2AnchorRecord(
        anchor_id="active-old",
        fingerprint=ROOT_BETA,
        subject_id="root-beta",
        effective_ms=1_000,
        generation=1,
        path_length=4,
        trusted=True,
        tombstone=False,
        revoked=False,
        shard_id="test",
        record_index=0,
    )
    tombstone = V2AnchorRecord(
        anchor_id="tombstone-new",
        fingerprint=ROOT_BETA,
        subject_id="root-beta",
        effective_ms=2_000,
        generation=2,
        path_length=4,
        trusted=True,
        tombstone=True,
        revoked=False,
        shard_id="test",
        record_index=1,
    )
    view = build_trust_view(
        parsed.certificates,
        None,
        V2Snapshot(
            generation=2,
            effective_ms=2_000,
            anchors=(active, tombstone),
            shard_count=1,
            metadata={},
        ),
        3_000,
    )
    anchor = view.anchors[ROOT_BETA]
    assert anchor.source_record_ids == ["tombstone-new"]
    assert anchor.tombstone is True
    assert anchor.trusted is False
    assert anchor.generation == 2
