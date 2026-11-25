"""Cross-path consistency checks and replay helpers."""

from __future__ import annotations

from typing import Any

import numpy as np

from .codes import BusinessCode
from .detect import detect_drift, replay_report
from .dsp import estimate_envelope_oneshot, estimate_envelope_serial
from .hashing import canonical_hash
from .thresholds import gate_decisions_match, migrate_v1_to_v2, migrate_v2_to_v1, read_thresholds


def verify_serial_vs_oneshot(
    pcm: np.ndarray,
    threshold_doc: dict[str, Any],
    frame_length: int | None = None,
    frame_hop: int | None = None,
) -> dict[str, Any]:
    frame_length = frame_length or int(threshold_doc.get("frame_length", 320))
    frame_hop = frame_hop or int(threshold_doc.get("frame_hop", 160))
    serial_blocks = estimate_envelope_serial(pcm, frame_length, frame_hop)
    oneshot_blocks = estimate_envelope_oneshot(pcm, frame_length, frame_hop)

    if len(serial_blocks) != len(oneshot_blocks):
        return {
            "ok": False,
            "code": BusinessCode.E_INCONSISTENT,
            "first_mismatch": {
                "field": "block_count",
                "serial": len(serial_blocks),
                "oneshot": len(oneshot_blocks),
            },
        }

    for index, (serial_block, oneshot_block) in enumerate(zip(serial_blocks, oneshot_blocks)):
        serial_hash = canonical_hash(serial_block)
        oneshot_hash = canonical_hash(oneshot_block)
        if serial_hash != oneshot_hash:
            return {
                "ok": False,
                "code": BusinessCode.E_INCONSISTENT,
                "first_mismatch": {
                    "field": "envelope_block",
                    "position": index,
                    "frame_index": serial_block.get("frame_index"),
                    "sample_interval": [serial_block.get("start_sample"), serial_block.get("end_sample")],
                    "serial_hash": serial_hash,
                    "oneshot_hash": oneshot_hash,
                },
            }

    serial_drift = detect_drift(serial_blocks, threshold_doc)
    oneshot_drift = detect_drift(oneshot_blocks, threshold_doc)
    if serial_drift["code"] != oneshot_drift["code"]:
        return {
            "ok": False,
            "code": BusinessCode.E_INCONSISTENT,
            "first_mismatch": {
                "field": "dual_numeric_consistency",
                "serial_code": serial_drift["code"],
                "oneshot_code": oneshot_drift["code"],
            },
        }

    for index, (left, right) in enumerate(zip(serial_drift["records"], oneshot_drift["records"])):
        fields = ("business_code", "sample_interval", "precomputed_hash", "dominant_subband")
        for field in fields:
            if left.get(field) != right.get(field):
                return {
                    "ok": False,
                    "code": BusinessCode.E_INCONSISTENT,
                    "first_mismatch": {
                        "field": field,
                        "position": index,
                        "frame_index": left.get("frame_index"),
                        "sample_interval": left.get("sample_interval"),
                        "serial": left.get(field),
                        "oneshot": right.get(field),
                    },
                }

    code = BusinessCode.VERIFY_OK if serial_drift["dual_consistent"] else BusinessCode.E_INCONSISTENT
    return {
        "ok": code == BusinessCode.VERIFY_OK,
        "code": code,
        "block_count": len(serial_blocks),
        "event_count": serial_drift["event_count"],
        "event_codes": _count_codes(serial_drift["events"]),
        "first_mismatch": None,
        "envelope_hash": canonical_hash(serial_blocks),
        "drift_hash": canonical_hash(serial_drift["events"]),
    }


def _count_codes(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        code = event["business_code"]
        counts[code] = counts.get(code, 0) + 1
    return counts


def verify_migration_gates(blocks: list[dict[str, Any]], threshold_doc: dict[str, Any]) -> dict[str, Any]:
    if threshold_doc["source_version"] == "v1":
        migrated_payload, report = migrate_v1_to_v2(threshold_doc)
    else:
        migrated_payload, report = migrate_v2_to_v1(threshold_doc)
    migrated_doc = read_thresholds(migrated_payload)
    equivalence = gate_decisions_match(blocks, threshold_doc, migrated_doc)
    return {
        "ok": equivalence["consistent"],
        "code": BusinessCode.VERIFY_OK if equivalence["consistent"] else BusinessCode.E_INCONSISTENT,
        "equivalence": equivalence,
        "migration_report": report,
        "migrated_doc": migrated_doc,
    }


def verify_and_replay_report(report: dict[str, Any]) -> dict[str, Any]:
    replay = replay_report(report)
    return {
        "ok": True,
        "code": BusinessCode.VERIFY_OK,
        "replay": replay,
    }
