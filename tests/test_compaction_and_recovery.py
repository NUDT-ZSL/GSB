from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from wadiag.compaction import merge_selected_records
from wadiag.models import WriteRecord
from wadiag.segments import build_flush_segments
from wadiag.validation import parse_storage_config

from .conftest import ROOT, cli_json, load_sample, run_cli


def _small_storage(retention: int = 0):
    return parse_storage_config(
        {
            "schema_version": 1,
            "memtable_capacity_bytes": 4096,
            "segment_size_limit_bytes": 4096,
            "level_count": 3,
            "level_trigger_thresholds": [2, 2, 2],
            "tombstone_retention_generations": retention,
            "compression_block_size_bytes": 256,
            "merge_budget_bytes": 4096,
            "logical_clock_step_ms": 1,
            "level_compression_ratios": ["0.9", "0.8", "0.7"],
            "bloom_bits_per_key": 10,
            "bloom_hash_count": 3,
        }
    )


def test_tombstone_retention_and_reference_guard() -> None:
    storage = _small_storage(0)
    records = (
        WriteRecord(1, "user:one", 10, "put", 1),
        WriteRecord(2, "user:one", 0, "delete", 2),
    )
    segment = build_flush_segments(records, storage, "Runit")[0]
    kept, cleared, generation = merge_selected_records((segment,), (), storage)
    assert generation == 1
    assert cleared == ["user:one"]
    assert kept == []

    outside_segment = build_flush_segments(
        (WriteRecord(0, "user:one", 8, "put", 0),),
        storage,
        "Runit",
        first_segment_no=2,
    )[0]
    kept, cleared, _ = merge_selected_records(
        (segment,),
        (segment, outside_segment),
        storage,
    )
    assert cleared == []
    assert len(kept) == 1
    assert kept[0].is_tombstone

    retained_storage = _small_storage(2)
    kept, cleared, _ = merge_selected_records(
        (segment,),
        (),
        retained_storage,
    )
    assert cleared == []
    assert kept[0].is_tombstone


def test_budget_outcome_categories_and_exit_report(tmp_path: Path) -> None:
    workspace = tmp_path / "compaction"
    load_sample(workspace)
    assert run_cli(workspace, "flush").returncode == 0
    outcomes: set[str] = set()
    for _ in range(8):
        result = run_cli(workspace, "compact")
        assert result.returncode == 0, result.stdout + result.stderr
        payload = cli_json(result)
        if not payload.get("applied"):
            continue
        plan = payload["plan"]
        assert plan["budget_used"] <= json.loads(
            (ROOT / "samples" / "storage_config.json").read_text(encoding="utf-8")
        )["merge_budget_bytes"]
        for outcome in plan["outcomes"]:
            outcomes.add(outcome["outcome"])
            assert outcome["first_trigger_constraint"]
            assert outcome["reason"]
    assert {
        "SELECTED",
        "NOT_SELECTED_WITHIN_BUDGET",
        "SELECTED_THEN_CANCELLED",
        "ACTIVE_ABANDONED",
    }.issubset(outcomes)
    assert run_cli(workspace, "verify").returncode == 0


def _assert_pending_requires_resume(workspace: Path) -> None:
    status = run_cli(workspace, "status")
    assert status.returncode == 0
    assert cli_json(status)["pending_prepared"] >= 1


def test_hard_kill_during_flush_prepare_recovers(tmp_path: Path) -> None:
    workspace = tmp_path / "crash_flush"
    load_sample(workspace)
    crashed = run_cli(
        workspace,
        "flush",
        env={"WADIAG_CRASH_POINT": "flush_prepare"},
    )
    assert crashed.returncode == 137
    _assert_pending_requires_resume(workspace)
    resumed = run_cli(workspace, "resume")
    assert resumed.returncode == 0
    resumed_payload = cli_json(resumed)
    assert resumed_payload["remaining_prepared"] == 0
    verify = run_cli(workspace, "verify")
    assert verify.returncode == 0
    assert cli_json(verify)["ok"] is True


def test_hard_kill_during_flush_files_recovers(tmp_path: Path) -> None:
    workspace = tmp_path / "crash_flush_files"
    load_sample(workspace)
    crashed = run_cli(
        workspace,
        "flush",
        env={"WADIAG_CRASH_POINT": "flush_files"},
    )
    assert crashed.returncode == 137
    _assert_pending_requires_resume(workspace)
    resumed = run_cli(workspace, "resume")
    assert resumed.returncode == 0
    assert cli_json(resumed)["remaining_prepared"] == 0
    assert not list(workspace.rglob("*.tmp"))
    assert run_cli(workspace, "verify").returncode == 0


def test_hard_kill_during_compact_prepare_recovers(tmp_path: Path) -> None:
    workspace = tmp_path / "crash_compact"
    load_sample(workspace)
    assert run_cli(workspace, "flush").returncode == 0
    crashed = run_cli(
        workspace,
        "compact",
        env={"WADIAG_CRASH_POINT": "compact_prepare"},
    )
    assert crashed.returncode == 137
    _assert_pending_requires_resume(workspace)
    resumed = run_cli(workspace, "resume")
    assert resumed.returncode == 0
    payload = cli_json(resumed)
    assert payload["remaining_prepared"] == 0
    assert any(
        item.get("kind") == "compact"
        for item in payload["replayed_pending_operations"]
    )
    verify = run_cli(workspace, "verify")
    assert verify.returncode == 0
    assert cli_json(verify)["ok"] is True


def test_parallel_disjoint_flush_ranges(tmp_path: Path) -> None:
    workspace = tmp_path / "parallel"
    load_sample(workspace)
    base = [
        sys.executable,
        "-m",
        "wadiag",
        "--workspace",
        str(workspace),
        "flush",
    ]
    first = subprocess.Popen(
        [*base, "--from-segment", "1", "--to-segment", "7"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        [*base, "--from-segment", "8", "--to-segment", "14"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_out, first_err = first.communicate(timeout=60)
    second_out, second_err = second.communicate(timeout=60)
    assert first.returncode == 0, first_out + first_err
    assert second.returncode == 0, second_out + second_err
    first_payload = json.loads(first_out)
    second_payload = json.loads(second_out)
    written = first_payload["segments_written"] + second_payload["segments_written"]
    assert written == 14
    status = run_cli(workspace, "status")
    assert status.returncode == 0
    status_payload = cli_json(status)
    l0 = status_payload["level_views"]["0"]
    assert l0["segment_count"] == 14
    assert len(set(l0["segment_uids"])) == 14
    assert status_payload["memtable_flushed_seq"] == 36
    assert run_cli(workspace, "verify").returncode == 0


def test_flush_watermark_advances_only_over_contiguous_prefix(tmp_path: Path) -> None:
    workspace = tmp_path / "watermark"
    load_sample(workspace)
    suffix = run_cli(
        workspace,
        "flush",
        "--from-segment",
        "8",
        "--to-segment",
        "14",
    )
    assert suffix.returncode == 0
    status = cli_json(run_cli(workspace, "status"))
    assert status["memtable_flushed_seq"] == 0
    prefix = run_cli(
        workspace,
        "flush",
        "--from-segment",
        "1",
        "--to-segment",
        "7",
    )
    assert prefix.returncode == 0
    status = cli_json(run_cli(workspace, "status"))
    assert status["memtable_flushed_seq"] == 36


def test_verify_detects_corrupt_committed_segment(tmp_path: Path) -> None:
    workspace = tmp_path / "corrupt"
    load_sample(workspace)
    assert run_cli(workspace, "flush").returncode == 0
    segment_files = sorted((workspace / "segments").rglob("*.seg"))
    assert segment_files
    segment_files[0].write_bytes(segment_files[0].read_bytes() + b"corruption")
    result = run_cli(workspace, "verify")
    assert result.returncode == 1
    payload = cli_json(result)
    assert payload["ok"] is False
    assert any(
        error["code"] in {"E_CHECKSUM", "E_BAD_SEGMENT"}
        for error in payload["errors"]
    )


def test_verify_detects_corrupt_manifest_version(tmp_path: Path) -> None:
    workspace = tmp_path / "manifest_corrupt"
    load_sample(workspace)
    assert run_cli(workspace, "flush").returncode == 0
    manifest_files = sorted((workspace / "manifests").rglob("*.json"))
    assert manifest_files
    manifest_files[0].write_text("{}\n", encoding="utf-8")
    result = run_cli(workspace, "verify")
    assert result.returncode == 1
    payload = cli_json(result)
    assert payload["ok"] is False
    assert any(
        error["code"].startswith("E_VERIFY_MANIFEST")
        for error in payload["errors"]
    )
