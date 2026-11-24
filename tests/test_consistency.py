from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import run_sample


def _conclusion(report: dict) -> dict:
    return {
        "rules": report["rules"],
        "bindings": report["bindings"],
        "clusters": report["clusters"],
        "rejections": report["rejections"],
        "version_ambiguous": report["diagnostics"]["version_ambiguous"],
        "window_ambiguous": report["diagnostics"]["window_ambiguous"],
        "proc_cycles": report["diagnostics"]["proc_cycles"],
    }


def _copy_sample(source: Path, target: Path) -> None:
    shutil.copytree(source, target)


def test_worker_count_does_not_change_conclusions(sample_dir, tmp_path):
    _, serial = run_sample(
        sample_dir, tmp_path / "serial", workers=1, queue_limit=64
    )
    _, parallel = run_sample(
        sample_dir, tmp_path / "parallel", workers=4, queue_limit=64
    )
    assert _conclusion(serial) == _conclusion(parallel)
    assert serial["status"] == parallel["status"] == "OK"


def test_alert_line_arrival_order_does_not_change_clusters(
    sample_dir, tmp_path
):
    reordered = tmp_path / "reordered-alerts"
    _copy_sample(sample_dir, reordered)
    lines = (reordered / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
    valid_lines = lines[:24]
    invalid_lines = lines[24:]
    (reordered / "alerts.jsonl").write_text(
        "\n".join(list(reversed(valid_lines)) + invalid_lines) + "\n",
        encoding="utf-8",
    )
    _, baseline = run_sample(sample_dir, tmp_path / "baseline")
    _, changed = run_sample(reordered, tmp_path / "changed")
    assert baseline["clusters"] == changed["clusters"]
    assert baseline["bindings"] == changed["bindings"]


def test_patch_stream_reordering_keeps_final_conclusions(
    sample_dir, tmp_path
):
    reordered = tmp_path / "reordered-patches"
    _copy_sample(sample_dir, reordered)
    lines = (reordered / "patches.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    (reordered / "patches.jsonl").write_text(
        "\n".join(reversed(lines)) + "\n",
        encoding="utf-8",
    )
    _, baseline = run_sample(sample_dir, tmp_path / "baseline-patches")
    _, changed = run_sample(reordered, tmp_path / "changed-patches")
    assert _conclusion(baseline) == _conclusion(changed)
    assert changed["diagnostics"]["recompute"]["full_match"] is True


def test_truth_sync_marker_matches_report(sample_dir, tmp_path):
    _, report = run_sample(sample_dir, tmp_path / "truth")
    truth = json.loads(
        (sample_dir / "truth_sync.json").read_text(encoding="utf-8")
    )
    expected = truth["expected"]
    assert report["status"] == expected["report_status"]
    assert (
        report["diagnostics"]["recompute"]["full_match"]
        is expected["recompute_full_match"]
    )
    assert set(expected["business_codes_present"]) <= set(
        report["business_codes"]
    )
    assert (
        report["patch_processing"]["arrival_order"]
        == expected["patch_arrival_order"]
    )
    assert (
        report["patch_processing"]["logical_order"]
        == expected["patch_logical_order"]
    )
