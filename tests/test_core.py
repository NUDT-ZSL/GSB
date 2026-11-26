from __future__ import annotations

import numpy as np

from meeting_audit.audio import frame_ranges
from meeting_audit.config import AuditConfig
from meeting_audit.pipeline import AuditPipeline, process_audio
from meeting_audit.query import QueryIndex, check_query_batch_consistency
from meeting_audit.rules import build_default_v1_rules, migrate_v1_to_v2
from meeting_audit.sample_generator import generate_sample
from meeting_audit.selfcheck import check_identity_stability, check_turn_order
from meeting_audit.verification import (
    check_chunked_detection,
    check_float_fixed_parity,
    check_rule_migration,
)


CONFIG = AuditConfig()


def test_sample_is_fixed_and_deterministic() -> None:
    first = generate_sample(CONFIG)
    second = generate_sample(CONFIG)
    assert first.pcm_int16.tobytes() == second.pcm_int16.tobytes()
    assert first.manifest["pcm_sha256"] == second.manifest["pcm_sha256"]
    assert first.manifest["seed"] == 20261127


def test_exact_frame_boundaries() -> None:
    ranges = frame_ranges(16_000, CONFIG)
    assert ranges[0] == (0, 0, 320)
    assert ranges[1] == (1, 160, 480)
    assert ranges[10] == (10, 1600, 1920)
    assert all(end - start == 320 for _, start, end in ranges)


def test_chunked_and_dual_path_detection_match() -> None:
    sample = generate_sample(CONFIG)
    chunked = check_chunked_detection(sample.pcm_int16, CONFIG)
    parity = check_float_fixed_parity(sample.pcm_int16, CONFIG)
    assert chunked["ok"]
    assert parity["ok"]
    assert chunked["full_event_count"] == 11


def test_detects_expected_business_anomalies() -> None:
    sample = generate_sample(CONFIG)
    result = process_audio(sample.pcm_int16, CONFIG)
    by_type = {row["anomaly_type"]: row["business_code"] for row in result["classified_turns"]}
    assert by_type["rapid_switch"] == "BIZ_TURN_TOO_FAST"
    assert by_type["speaker_preemption"] == "BIZ_SPEAKER_PREEMPT"
    assert by_type["silence_too_long"] == "BIZ_SILENCE_TOO_LONG"
    anomalies = [
        (row["at_ms"], row["anomaly_type"])
        for row in result["classified_turns"]
        if row["anomaly_type"] != "normal_switch"
    ]
    assert anomalies == [
        (12120, "rapid_switch"),
        (16200, "speaker_preemption"),
        (21050, "silence_too_long"),
    ]


def test_v1_v2_migration_and_equivalence() -> None:
    sample = generate_sample(CONFIG)
    result = process_audio(sample.pcm_int16, CONFIG)
    v1 = build_default_v1_rules(CONFIG)
    report = check_rule_migration(v1, CONFIG, result["turn_points"])
    assert report["ok"]
    upgrade = report["upgrade_report"]
    assert upgrade["completed_item_count"] >= 2
    completed_rule_ids = {
        item["rule_id"]: item["completed_fields"]
        for item in upgrade["completed_items"]
    }
    assert "anomaly_type" in completed_rule_ids["V2-R0006"]
    assert "anomaly_type" in completed_rule_ids["V2-R0009"]
    assert report["downgrade_report"]["discarded_item_count"] == len(v1["rules"])


def test_identity_labels_are_stable_clusters() -> None:
    sample = generate_sample(CONFIG)
    result = process_audio(sample.pcm_int16, CONFIG)
    labels = [
        result["assignments"][segment["segment_id"]]["speaker_id"]
        for segment in result["segments"]
    ]
    assert labels == [
        "SPK-0001",
        "SPK-0002",
        "SPK-0003",
        "SPK-0001",
        "SPK-0002",
        "SPK-0003",
        "SPK-0001",
        "SPK-0002",
        "SPK-0003",
        "SPK-0002",
        "SPK-0001",
    ]


def test_query_is_exact_repeatable_and_batch_consistent() -> None:
    sample = generate_sample(CONFIG)
    result = process_audio(sample.pcm_int16, CONFIG)
    index = QueryIndex(result, CONFIG)
    first = index.at_ms("12120")
    second = index.at_ms(12120)
    assert first == second
    assert first["latest_turn"]["event_id"] == "T0006"
    assert first["business_code"] == "BIZ_TURN_TOO_FAST"
    assert first["speaker_id"] == "SPK-0003"
    segment = index.segment("SEG-0006")
    assert segment["speaker_id"] == "SPK-0003"
    consistency = check_query_batch_consistency(result, CONFIG)
    assert consistency["ok"]


def test_self_check_error_codes_locate_first_frame() -> None:
    drift = check_identity_stability(
        {
            "segments": [{"segment_id": "SEG-X", "frame_range": [40, 44]}],
            "assignments": {"SEG-X": {"speaker_id": "SPK-0001"}},
            "identity_frames": [
                {"frame_index": 41, "segment_id": "SEG-X", "speaker_id": "SPK-0001", "at_ms": 420},
                {"frame_index": 42, "segment_id": "SEG-X", "speaker_id": "SPK-0009", "at_ms": 430},
            ],
        }
    )
    assert drift["code"] == "E_IDENTITY_DRIFT"
    assert drift["first_violation_frame"] == 42

    spurious = check_turn_order(
        {
            "classified_turns": [
                {"event_id": "T1", "at_ms": 1000, "frame_index": 99, "business_code": "X"},
                {"event_id": "T2", "at_ms": 1100, "frame_index": 109, "business_code": "Y"},
            ],
            "segments": [],
        },
        CONFIG,
    )
    assert spurious["code"] == "E_SPURIOUS_TURN"
    assert spurious["first_violation_frame"] == 109

    overlap = check_turn_order(
        {
            "classified_turns": [
                {"event_id": "T1", "at_ms": 1000, "frame_index": 99},
                {"event_id": "T2", "at_ms": 1000, "frame_index": 100},
            ],
            "segments": [],
        },
        CONFIG,
    )
    assert overlap["code"] == "E_OVERLAP_TURN"


def test_full_pipeline_self_check_passes(tmp_path) -> None:
    pipeline = AuditPipeline(tmp_path / "ws", CONFIG)
    result = pipeline.run_full()
    assert result["self_check"]["ok"]
    assert (tmp_path / "ws" / "ledger" / "ledger_v1.jsonl").exists()
    assert (tmp_path / "ws" / "ledger" / "ledger_v2.jsonl").exists()
    assert result["snapshots"]["audit_report"]["version"] == 1
