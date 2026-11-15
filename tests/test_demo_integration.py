import json
from pathlib import Path

import numpy as np
import soundfile as sf

from piano_workbench.demo import run_demo


def test_full_demo_report_and_artifacts(tmp_path):
    report = run_demo(tmp_path / "demo_output")

    # Determinism across every state-tree node.
    assert all(item["byte_identical"] for item in report["determinism"])

    # Undo/redo and forced-termination recovery both hold byte-for-byte.
    undo_step = next(step for step in report["steps"] if step["phase"] == "undo_redo")
    assert undo_step["roundtrip_byte_identical"] is True
    assert undo_step["undo_matches_cache"] is True
    assert report["recovery"]["stable_state_byte_identical"] is True
    assert report["recovery"]["orphan_quarantined"] is True

    # All five required error codes plus E_PARAM are demonstrated.
    codes = {entry["code"] for entry in report["errors"].values()}
    assert {
        "E_SEL_RANGE",
        "E_NODE_MISSING",
        "E_CACHE_HASH",
        "E_VERSION_CONFLICT",
        "E_PARAM",
    }.issubset(codes)

    # Report + snapshot are written as JSON for local review.
    report_path = Path(report["report_path"])
    snapshot_path = Path(report["snapshot_path"])
    assert report_path.exists() and snapshot_path.exists()
    json.loads(report_path.read_text(encoding="utf-8"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["format_version"] == 1
    assert snapshot["sample_rate"] == 48000

    # Exported WAV is 48 kHz / 16-bit / mono.
    data, sample_rate = sf.read(report["export"]["path"], dtype="int16")
    assert sample_rate == 48000
    assert data.ndim == 1
    assert data.dtype == np.int16


def test_demo_is_repeatable(tmp_path):
    first = run_demo(tmp_path / "run_a")
    second = run_demo(tmp_path / "run_b")
    first_metrics = [(m["node_label"], m["rms_dbfs"], m["peak_abs_int16"])
                     for m in first["metrics"]]
    second_metrics = [(m["node_label"], m["rms_dbfs"], m["peak_abs_int16"])
                      for m in second["metrics"]]
    assert first_metrics == second_metrics
    assert [d["node_id"] for d in first["determinism"]] == \
           [d["node_id"] for d in second["determinism"]]
