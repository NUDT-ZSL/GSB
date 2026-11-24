from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from smuggle_detector.verify import verify_truth

from .helpers import conclusion_projection, run_sample


def test_deterministic_sample_matches_truth(
    sample_dir: Path, tmp_path: Path, truth: dict
) -> None:
    report, _, _, manager = run_sample(sample_dir, tmp_path / "workspace")
    verification = verify_truth(truth, report)
    assert verification["ok"], verification["mismatches"]
    assert report["summary"]["task_count"] == 14
    assert report["summary"]["success_count"] == 13
    assert report["summary"]["timeout_count"] == 1
    assert report["summary"]["queue_rejected_count"] == 0
    assert report["summary"]["budget_stopped_count"] == 0
    assert report["summary"]["duplicate_delivery_count"] == 1
    assert manager.verify()["valid"] is True

    by_id = {item["connection_id"]: item for item in report["connections"]}
    assert by_id["conn-cl-te"]["rule_view"]["generation"] == 1
    assert by_id["conn-v2-cl-te"]["rule_view"]["generation"] == 2
    assert by_id["conn-v2-cl-te"]["hits"][0]["rule_effective_ms"] == 2000
    assert by_id["conn-timeout"]["status"] == "timeout"
    assert by_id["conn-gap"]["errors"][0]["missing_ranges"] == [[1, 1]]
    assert by_id["conn-mixed"]["errors"][0]["code"] == "E_MIXED_CONN"


def test_concurrency_does_not_change_conclusions(
    sample_dir: Path, tmp_path: Path
) -> None:
    serial, _, _, _ = run_sample(
        sample_dir,
        tmp_path / "serial",
        overrides={"concurrency": 1, "queue_limit": 64},
    )
    parallel, _, _, _ = run_sample(
        sample_dir,
        tmp_path / "parallel",
        overrides={"concurrency": 4, "queue_limit": 64},
    )
    assert conclusion_projection(serial) == conclusion_projection(parallel)
    assert serial["summary"] == parallel["summary"]


def test_record_interleaving_does_not_change_conclusions(
    sample_dir: Path, tmp_path: Path
) -> None:
    baseline, _, _, _ = run_sample(sample_dir, tmp_path / "baseline")
    reordered_dir = tmp_path / "reordered-samples"
    shutil.copytree(sample_dir, reordered_dir)
    lines = (reordered_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    lines.reverse()
    (reordered_dir / "messages.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    reordered, _, _, _ = run_sample(reordered_dir, tmp_path / "reordered-workspace")
    assert conclusion_projection(baseline) == conclusion_projection(reordered)


def test_one_connection_failure_does_not_abort_batch(
    sample_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import smuggle_detector.engine as engine

    original = engine.detect_connection

    def flaky(connection_id: str, *args, **kwargs):
        if connection_id == "conn-clean":
            raise RuntimeError("synthetic per-connection failure")
        return original(connection_id, *args, **kwargs)

    monkeypatch.setattr(engine, "detect_connection", flaky)
    report, _, _, _ = run_sample(sample_dir, tmp_path / "failure-workspace")
    by_id = {item["connection_id"]: item for item in report["connections"]}
    assert by_id["conn-clean"]["status"] == "failed"
    assert by_id["conn-cl-te"]["status"] == "ok"
    assert report["summary"]["failure_count"] == 1
    assert report["summary"]["success_count"] == 12

