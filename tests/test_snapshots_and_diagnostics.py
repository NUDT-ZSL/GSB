from __future__ import annotations

import json
import shutil
from pathlib import Path

from asr_editor.diagnostics import build_cross_format_report
from asr_editor.formats import transcript_fingerprint
from asr_editor.operations import apply_operation
from asr_editor.parsers import parse_v1_file, parse_v2_file
from asr_editor.workspace import Workspace

OPS = [
    {"op": "merge", "utterance_id": "utt-001", "other_utterance_id": "utt-002"},
    {"op": "split", "utterance_id": "utt-001", "token_index": 3},
    {"op": "shift", "start_ms": 1500, "end_ms": 2900, "delta_ms": 250},
    {"op": "replace", "utterance_id": "utt-003", "token_index": 1, "text": "总结"},
]


def _copy_sample(sample_dir: Path, destination: Path) -> tuple[Path, Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    for suffix in (".v1.json", ".v2.srt", ".tokens.meta.json"):
        shutil.copy2(
            sample_dir / f"sample01{suffix}", destination / f"sample01{suffix}"
        )
    return (
        destination / "sample01.v1.json",
        destination / "sample01.v2.srt",
        destination / "sample01.tokens.meta.json",
    )


def _expected_transcript(sample_dir: Path):
    transcript = parse_v1_file(sample_dir / "sample01.v1.json").transcript
    for operation in OPS:
        transcript = apply_operation(transcript, operation).transcript
    return transcript


def test_each_edit_is_snapshotted_and_restored(
    sample_dir: Path, tmp_path: Path
) -> None:
    v1, v2, sidecar = _copy_sample(sample_dir, tmp_path / "work")
    state_dir = tmp_path / "state"
    workspace = Workspace.open(v1, v2, sidecar, state_dir, create_initial_snapshot=True)
    for index, operation in enumerate(OPS, start=1):
        workspace.apply_operation(operation, index)
    expected = _expected_transcript(sample_dir)
    assert transcript_fingerprint(workspace.transcript) == transcript_fingerprint(
        expected
    )
    assert workspace.version == 4
    assert len(list((state_dir / "snapshots").glob("snapshot-*.json"))) == 5

    state_path = state_dir / "workspace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["workspace_version"] = 999
    state["snapshot_version"] = 999
    state_path.write_text(json.dumps(state), encoding="utf-8")
    recovered = Workspace.open(v1, v2, sidecar, state_dir)
    assert recovered.recovery.restored is True
    assert recovered.version == 4
    assert recovered.transcript.to_dict() == workspace.transcript.to_dict()


def test_recovery_restores_when_v2_is_corrupted(
    sample_dir: Path, tmp_path: Path
) -> None:
    v1, v2, sidecar = _copy_sample(sample_dir, tmp_path / "work")
    state_dir = tmp_path / "state"
    workspace = Workspace.open(v1, v2, sidecar, state_dir, create_initial_snapshot=True)
    workspace.apply_operation(OPS[0], 1)
    expected = workspace.transcript.to_dict()
    v2.write_text("not valid srt", encoding="utf-8")
    recovered = Workspace.open(v1, v2, sidecar, state_dir)
    assert recovered.recovery.restored is True
    assert recovered.transcript.to_dict() == expected
    assert "00:00:00,000 -->" in v2.read_text(encoding="utf-8")


def test_snapshot_store_prefers_highest_complete_version(
    sample_dir: Path, tmp_path: Path
) -> None:
    v1, v2, sidecar = _copy_sample(sample_dir, tmp_path / "work")
    state_dir = tmp_path / "state"
    workspace = Workspace.open(v1, v2, sidecar, state_dir, create_initial_snapshot=True)
    workspace.apply_operation(OPS[0], 1)
    workspace.apply_operation(OPS[1], 2)
    snapshot_dir = state_dir / "snapshots"
    # Simulate a crash after snapshot-000002 was committed but before the
    # latest-name file was atomically replaced.
    (snapshot_dir / "snapshot.json").write_text(
        (snapshot_dir / "snapshot-000001.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    latest = workspace.store.load_latest()
    assert latest is not None
    assert latest.version == 2


def test_all_edit_prefixes_restore_identically(
    sample_dir: Path, tmp_path: Path
) -> None:
    expected = parse_v1_file(sample_dir / "sample01.v1.json").transcript
    for count, operation in enumerate(OPS, start=1):
        expected = apply_operation(expected, operation).transcript
        work = tmp_path / f"prefix-{count}"
        v1, v2, sidecar = _copy_sample(sample_dir, work)
        state_dir = work / "state"
        expected = parse_v1_file(v1).transcript
        for item in OPS[:count]:
            expected = apply_operation(expected, item).transcript
        workspace = Workspace.open(
            v1, v2, sidecar, state_dir, create_initial_snapshot=True
        )
        for index, item in enumerate(OPS[:count], start=1):
            workspace.apply_operation(item, index)
        state_path = state_dir / "workspace.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["workspace_version"] += 10
        state_path.write_text(json.dumps(state), encoding="utf-8")
        recovered = Workspace.open(v1, v2, sidecar, state_dir)
        assert recovered.recovery.restored is True
        assert recovered.transcript.to_dict() == expected.to_dict()


def test_exported_v1_v2_are_equivalent(sample_dir: Path, tmp_path: Path) -> None:
    v1, v2, sidecar = _copy_sample(sample_dir, tmp_path / "work")
    workspace = Workspace.open(
        v1, v2, sidecar, tmp_path / "state", create_initial_snapshot=True
    )
    for index, operation in enumerate(OPS, start=1):
        workspace.apply_operation(operation, index)
    parsed_v1 = parse_v1_file(v1)
    parsed_v2 = parse_v2_file(v2)
    assert not parsed_v1.failures and not parsed_v2.failures
    report = build_cross_format_report(
        parsed_v1.transcript,
        parsed_v2.transcript,
        snapshot_version=workspace.version,
    )
    assert report["summary"]["mismatch_utterance_count"] == 0
    for left, right in zip(
        parsed_v1.transcript.utterances, parsed_v2.transcript.utterances
    ):
        assert (left.start_ms, left.end_ms) == (right.start_ms, right.end_ms)
        assert left.text == right.text
        assert len(left.tokens) == len(right.tokens)


def test_cross_format_report_flags_mismatch(sample_dir: Path) -> None:
    v1 = parse_v1_file(sample_dir / "sample02.v1.json")
    v2 = parse_v2_file(sample_dir / "sample02.v2.srt")
    report = build_cross_format_report(
        v1.transcript,
        v2.transcript,
        snapshot_version=7,
        precision_v1=v1.precision,
        precision_v2=v2.precision,
    )
    assert report["snapshot_version"] == 7
    assert report["summary"]["mismatch_utterance_count"] == 2
    assert report["utterances"][0]["max_time_deviation_ms"] == 30
    assert report["utterances"][1]["token_count_difference"] == 2
    assert "E_CROSS_FORMAT_MISMATCH" in report["business_codes"]
    assert report["precision"]["internal_unit"] == "integer_millisecond"
    assert report["precision"]["output_seconds_decimal_places"] == 3
    assert report["precision"]["meets_10ms_granularity"] is True


def test_report_records_parse_rounding(tmp_path: Path) -> None:
    sample_root = Path(__file__).parents[1] / "samples"
    v1 = parse_v1_file(sample_root / "sample03.v1.json")
    v2 = parse_v2_file(sample_root / "sample03.v2.srt")
    report = build_cross_format_report(
        v1.transcript,
        v2.transcript,
        precision_v1=v1.precision,
        precision_v2=v2.precision,
    )
    assert report["precision"]["parse_rounding_count"] == 3
    assert report["precision"]["max_parse_rounding_error_ms"] == 0.5
    assert report["summary"]["mismatch_utterance_count"] == 0
