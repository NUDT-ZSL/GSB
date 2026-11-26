from __future__ import annotations

from typing import Any

import numpy as np

from .audio import ChunkedFrameFeatureStream, extract_all_features
from .config import AuditConfig, DEFAULT_CONFIG
from .detector import TurnDetectionStream, detect_turns
from .rules import classify_turns, compare_rule_views, migrate_v1_to_v2, migrate_v2_to_v1


def turn_identity_set(detection: dict[str, Any]) -> list[tuple[str, int, int, str]]:
    return [
        (str(event["event_id"]), int(event["frame_index"]), int(event["at_ms"]), str(event["kind"]))
        for event in detection["turn_points"]
    ]


def check_chunked_detection(
    pcm: np.ndarray,
    config: AuditConfig = DEFAULT_CONFIG,
    chunk_sizes: list[int] | None = None,
) -> dict[str, Any]:
    full_features = extract_all_features(pcm, config, "fixed")
    full_detection = detect_turns(full_features, config)
    expected = turn_identity_set(full_detection)

    if chunk_sizes is None:
        # Deliberately awkward sample boundaries, including sub-frame splits.
        chunk_sizes = [1, 127, 160, 319, 320, 321, 1600, 73, 4096]

    feature_stream = ChunkedFrameFeatureStream(config, "fixed")
    detector_stream = TurnDetectionStream(config)
    offset = 0
    size_index = 0
    emitted_feature_count = 0
    while offset < len(pcm):
        size = chunk_sizes[size_index % len(chunk_sizes)]
        size_index += 1
        chunk = pcm[offset : offset + size]
        offset += size
        features = feature_stream.push(chunk.tobytes())
        emitted_feature_count += len(features)
        for feature in features:
            detector_stream.push(feature)
    feature_stream.flush()
    from .detector import build_detection_result

    chunked_detection = build_detection_result(
        detector_stream.raw_events, detector_stream.vad_segments, config
    )
    actual = turn_identity_set(chunked_detection)
    return {
        "ok": actual == expected and emitted_feature_count == len(full_features),
        "full_event_count": len(expected),
        "chunked_event_count": len(actual),
        "frame_count": len(full_features),
        "chunk_sizes": chunk_sizes,
        "expected": expected,
        "actual": actual,
    }


def check_float_fixed_parity(
    pcm: np.ndarray, config: AuditConfig = DEFAULT_CONFIG
) -> dict[str, Any]:
    fixed_features = extract_all_features(pcm, config, "fixed")
    float_features = extract_all_features(pcm, config, "float")
    fixed = detect_turns(fixed_features, config)
    floating = detect_turns(float_features, config)
    fixed_set = turn_identity_set(fixed)
    float_set = turn_identity_set(floating)
    feature_diffs = [
        index
        for index, (left, right) in enumerate(zip(fixed_features, float_features))
        if left.rms_q != right.rms_q or left.zcr_count != right.zcr_count
    ]
    return {
        "ok": fixed_set == float_set and not feature_diffs,
        "fixed_event_set": fixed_set,
        "float_event_set": float_set,
        "feature_diff_frames": feature_diffs[:10],
        "feature_diff_count": len(feature_diffs),
    }


def check_rule_migration(
    v1_doc: dict[str, Any],
    config: AuditConfig = DEFAULT_CONFIG,
    turn_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    v2_doc, upgrade_report = migrate_v1_to_v2(v1_doc)
    downgraded_v1, downgrade_report = migrate_v2_to_v1(v2_doc)
    equivalence = compare_rule_views(
        v1_doc, v2_doc, turn_points or [], config
    )
    v1_codes = [row["business_code"] for row in classify_turns(turn_points or [], v1_doc, config)]
    v2_codes = [row["business_code"] for row in classify_turns(turn_points or [], v2_doc, config)]
    return {
        "ok": equivalence["equivalent"] and v1_codes == v2_codes,
        "upgrade_report": upgrade_report,
        "downgrade_report": downgrade_report,
        "equivalence": equivalence,
        "v1_codes": v1_codes,
        "v2_codes": v2_codes,
        "downgraded_v1": downgraded_v1,
        "v2_doc": v2_doc,
    }


def verify_deterministic_foundations(
    pcm: np.ndarray,
    v1_doc: dict[str, Any],
    turn_points: list[dict[str, Any]],
    config: AuditConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    chunk = check_chunked_detection(pcm, config)
    parity = check_float_fixed_parity(pcm, config)
    migration = check_rule_migration(v1_doc, config, turn_points)
    return {
        "ok": chunk["ok"] and parity["ok"] and migration["ok"],
        "chunked_detection": chunk,
        "float_fixed_parity": parity,
        "rule_migration": {
            "ok": migration["ok"],
            "upgrade_completed_items": migration["upgrade_report"]["completed_item_count"],
            "downgrade_discarded_items": migration["downgrade_report"]["discarded_item_count"],
            "equivalent": migration["equivalence"]["equivalent"],
        },
    }
