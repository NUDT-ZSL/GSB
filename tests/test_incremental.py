from __future__ import annotations

import numpy as np

from meeting_audit.config import AuditConfig
from meeting_audit.incremental import IncrementalManager
from meeting_audit.pipeline import AuditPipeline


CONFIG = AuditConfig()


def test_truth_correction_invalidates_one_ledger_row(tmp_path) -> None:
    pipeline = AuditPipeline(tmp_path / "ws", CONFIG)
    pipeline.run_full()
    manager = IncrementalManager(pipeline)
    report = manager.apply_truth_correction("SEG-0005", "SPK_B", "truth-test")
    assert report["ok"]
    assert report["recomputed"]["segments"] == ["SEG-0005"]
    assert report["recomputed"]["ledger_rows"] == ["LED-0005"]
    assert len(report["reused"]["ledger_rows"]) == 10
    assert report["byte_equality"]["ok"]


def test_rule_promotion_recomputes_classification_ledger(tmp_path) -> None:
    pipeline = AuditPipeline(tmp_path / "ws", CONFIG)
    pipeline.run_full()
    report = IncrementalManager(pipeline).promote_rules(
        "v2.1.0-test", {"rapid_switch_ms": 800}, "rule-test"
    )
    assert report["ok"]
    assert report["rule_version"] == "v2.1.0-test"
    assert "anomaly_classification" in report["recomputed"]["stages"]
    assert "frame_features" in report["reused"]["stages"]
    assert len(report["recomputed"]["ledger_rows"]) == 11
    assert report["byte_equality"]["ok"]


def test_audio_revision_invalidates_suffix(tmp_path) -> None:
    pipeline = AuditPipeline(tmp_path / "ws", CONFIG)
    pipeline.run_full()
    pcm, _ = pipeline.ensure_sample()
    revised = pcm.copy()
    start_sample = 15_000 * CONFIG.samples_per_ms
    revised[start_sample:] = np.clip(
        np.rint(revised[start_sample:].astype(np.float64) * 0.82),
        -32768,
        32767,
    ).astype(np.int16)
    report = IncrementalManager(pipeline).apply_audio_revision(
        revised, "audio-test", "test revision"
    )
    assert report["ok"]
    assert report["invalidation"]["changed_ms_range"][0] == 15_000
    assert report["recomputed"]["ledger_rows"] == [
        "LED-0007",
        "LED-0008",
        "LED-0009",
        "LED-0010",
        "LED-0011",
    ]
    assert report["reused"]["ledger_rows"] == [
        "LED-0001",
        "LED-0002",
        "LED-0003",
        "LED-0004",
        "LED-0005",
        "LED-0006",
    ]
    assert report["byte_equality"]["ok"]


def test_snapshot_recovery_discards_uncommitted(tmp_path) -> None:
    pipeline = AuditPipeline(tmp_path / "ws", CONFIG)
    pipeline.run_full()
    before = pipeline.snapshots.current_version("ledger_write")
    pipeline.snapshots.write_fake_uncommitted(
        "ledger_write", {"version": before + 1, "committed": False}
    )
    recovered = pipeline.snapshots.recover()
    after = pipeline.snapshots.current_version("ledger_write")
    assert recovered["discarded_intermediate_files"]
    assert before == after
