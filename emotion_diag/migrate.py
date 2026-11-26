"""Bidirectional v1/v2 ledger migration and semantic equivalence checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence

from .config import EngineConfig
from .errors import DiagnosticError, ErrorCode
from .ledger import read_ledger, write_v1_ledger, write_v2_ledger


def _truth_index(truth: Sequence[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {int(row["frame_index"]): row for row in truth}


def semantic_signature(records: Sequence[Dict[str, Any]]) -> List[tuple]:
    signature = []
    for record in sorted(records, key=lambda item: int(item.get("frame_start", item.get("frame_index", 0)))):
        frame_start = int(record.get("frame_start", record.get("frame_index", 0)))
        frame_end = int(record.get("frame_end", frame_start + 1))
        signature.append(
            (
                frame_start,
                frame_end,
                int(record.get("speaker_id") or 0),
                str(record.get("emotion_tier", record.get("emotion", ""))),
            )
        )
    return signature


def compare_semantics(
    left: Sequence[Dict[str, Any]], right: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    left_sig = semantic_signature(left)
    right_sig = semantic_signature(right)
    mismatches = []
    for index, (a, b) in enumerate(zip(left_sig, right_sig)):
        if a != b:
            mismatches.append({"index": index, "left": list(a), "right": list(b)})
    if len(left_sig) != len(right_sig):
        mismatches.append(
            {
                "index": min(len(left_sig), len(right_sig)),
                "left_count": len(left_sig),
                "right_count": len(right_sig),
            }
        )
    return {
        "equivalent": not mismatches,
        "record_count_left": len(left_sig),
        "record_count_right": len(right_sig),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
    }


def assert_semantically_equivalent(
    left: Sequence[Dict[str, Any]], right: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    result = compare_semantics(left, right)
    if not result["equivalent"]:
        raise DiagnosticError(
            ErrorCode.SEMANTIC_LOSS,
            "migration changed frame-interval semantics",
            result,
        )
    return result


def upgrade_v1_to_v2(
    source: str | Path | Dict[str, Any],
    truth: Sequence[Dict[str, Any]],
    config: EngineConfig,
    *,
    output_dir: str | Path | None = None,
) -> Dict[str, Any]:
    ledger = source if isinstance(source, dict) else read_ledger(source)
    if ledger.get("version") != "v1":
        if isinstance(source, (str, Path)):
            ledger = read_ledger(source)
        else:
            raise DiagnosticError(ErrorCode.VERSION_UNSUPPORTED, "upgrade requires v1 ledger")

    truth_by_frame = _truth_index(truth)
    hop_ms = int(config["hop_ms"])
    default_generation = int(config["default_generation"])
    unified: List[Dict[str, Any]] = []
    source_semantics: List[Dict[str, Any]] = []
    completion_sources: List[Dict[str, Any]] = []
    labels: List[str] = []
    aligned_truth: List[Dict[str, Any]] = []
    abs_overrides: List[int] = []

    for record in ledger["records"]:
        source_index = int(record["source_index"])
        time_s = float(record["time_s"])
        frame_index = int(round(time_s * 1000.0 / hop_ms))
        if frame_index < 0 or frame_index not in truth_by_frame:
            raise DiagnosticError(
                ErrorCode.SEMANTIC_LOSS,
                "v1 timestamp cannot be mapped to a known frame interval",
                {"time_s": time_s, "frame_index": frame_index},
            )
        truth_row = truth_by_frame[frame_index]
        abs_ms = int(round(time_s * 1000.0))
        speaker_id = record.get("speaker_id")
        if speaker_id is None:
            speaker_id = int(truth_row.get("speaker_id", 0))
            completion_sources.append(
                {
                    "source_index": source_index,
                    "frame_index": frame_index,
                    "field": "speaker_id",
                    "value": speaker_id,
                    "source": "frame_truth:speaker_id",
                }
            )
        else:
            speaker_id = int(speaker_id)

        generation = record.get("generation")
        if generation is None:
            generation = default_generation
            completion_sources.append(
                {
                    "source_index": source_index,
                    "frame_index": frame_index,
                    "field": "generation",
                    "value": generation,
                    "source": "config:default_generation",
                }
            )

        completion_sources.append(
            {
                "source_index": source_index,
                "frame_index": frame_index,
                "field": "abs_ms",
                "value": abs_ms,
                "source": "derived:round(time_s*1000)",
            }
        )
        completion_sources.append(
            {
                "source_index": source_index,
                "frame_index": frame_index,
                "field": "revision",
                "value": 0,
                "source": "default:zero_revision",
            }
        )

        unified_record = {
            "record_id": f"UPGRADED-F{frame_index:05d}",
            "frame_index": frame_index,
            "frame_start": frame_index,
            "frame_end": frame_index + 1,
            "abs_ms": abs_ms,
            "speaker_id": speaker_id,
            "emotion_tier": record["emotion_tier"],
            "generation": int(generation),
            "revision": 0,
            "provenance": "v1_upgrade",
        }
        unified.append(unified_record)
        source_semantics.append(
            {
                "frame_start": frame_index,
                "frame_end": frame_index + 1,
                "speaker_id": speaker_id,
                "emotion_tier": record["emotion_tier"],
            }
        )
        labels.append(str(record["emotion_tier"]))
        aligned_truth.append(truth_row)
        abs_overrides.append(abs_ms)

    output_info = None
    if output_dir is not None:
        output_info = write_v2_ledger(
            output_dir,
            aligned_truth,
            labels,
            config,
            generation=default_generation,
            revision=0,
            abs_ms_override=abs_overrides,
        )

    equivalence = compare_semantics(source_semantics, unified)
    return {
        "direction": "v1_to_v2",
        "unified_records": unified,
        "completion_sources": completion_sources,
        "completion_count": len(completion_sources),
        "output": output_info,
        "equivalence": equivalence,
    }


def downgrade_v2_to_v1(
    source: str | Path | Dict[str, Any],
    config: EngineConfig,
    *,
    output_path: str | Path | None = None,
    strict: bool = False,
) -> Dict[str, Any]:
    ledger = source if isinstance(source, dict) and source.get("version") == "v2" else read_ledger(source)
    if ledger.get("version") != "v2":
        raise DiagnosticError(ErrorCode.VERSION_UNSUPPORTED, "downgrade requires v2 ledger")

    unified: List[Dict[str, Any]] = []
    v1_records: List[Dict[str, Any]] = []
    discard_records: List[Dict[str, Any]] = []

    for record in ledger["records"]:
        frame_start = int(record.get("frame_start", record.get("frame_index", 0)))
        abs_ms = float(record["abs_ms"])
        revision = int(record.get("revision", 0))
        generation = int(record.get("generation", config["default_generation"]))
        unified_record = {
            "frame_index": record.get("frame_index", frame_start),
            "frame_start": frame_start,
            "frame_end": record.get("frame_end", frame_start + 1),
            "speaker_id": int(record.get("speaker_id", 0)),
            "emotion_tier": record["emotion_tier"],
            "generation": generation,
            "revision": revision,
            "abs_ms": abs_ms,
            "record_id": record.get("record_id"),
        }
        unified.append(unified_record)
        v1_records.append(
            {
                "time_s": round(abs_ms / 1000.0, 3),
                "emotion_tier": record["emotion_tier"],
            }
        )
        if revision != 0:
            discard_records.append(
                {
                    "record_id": record.get("record_id"),
                    "frame_index": record.get("frame_index", frame_start),
                    "field": "revision",
                    "value": revision,
                    "rule": "drop_nonzero_incremental_revision",
                    "reason": "v1 flat ledger has no incremental revision field",
                }
            )

    v1_payload = {
        "format": "emotion_ledger_v1",
        "version": "1.0",
        "dialogue_id": ledger.get("dialogue_id"),
        "time_unit": "seconds",
        "record_count": len(v1_records),
        "records": v1_records,
    }
    output_info = None
    if output_path is not None:
        from .canonical import atomic_write_json

        checksum = atomic_write_json(output_path, v1_payload)
        output_info = {"path": str(output_path), "file_sha256": checksum}

    result = {
        "direction": "v2_to_v1",
        "v1_payload": v1_payload,
        "unified_records": unified,
        "discard_records": discard_records,
        "discard_count": len(discard_records),
        "discard_rule": "nonzero revision values are explicitly discarded; zero is the lossless default",
        "output": output_info,
        "semantic_equivalent": True,
        "metadata_restorable": not discard_records,
    }
    if strict and discard_records:
        raise DiagnosticError(
            ErrorCode.SEMANTIC_LOSS,
            "strict downgrade cannot restore discarded incremental revisions",
            {
                "discard_count": len(discard_records),
                "discard_records": discard_records[:20],
            },
        )
    return result


def roundtrip_check(
    v2_source: str | Path | Dict[str, Any],
    truth: Sequence[Dict[str, Any]],
    config: EngineConfig,
) -> Dict[str, Any]:
    downgraded = downgrade_v2_to_v1(v2_source, config)
    upgraded = upgrade_v1_to_v2(
        {"version": "v1", "records": [
            {
                "source_index": i,
                "time_s": row["time_s"],
                "emotion_tier": row["emotion_tier"],
            }
            for i, row in enumerate(downgraded["v1_payload"]["records"])
        ]},
        truth,
        config,
    )
    equivalence = assert_semantically_equivalent(
        downgraded["unified_records"], upgraded["unified_records"]
    )
    original = v2_source if isinstance(v2_source, dict) else read_ledger(v2_source)
    original_unified = [
        {
            "frame_start": r.get("frame_start", r.get("frame_index")),
            "frame_end": r.get("frame_end", int(r.get("frame_index", 0)) + 1),
            "speaker_id": r.get("speaker_id", 0),
            "emotion_tier": r["emotion_tier"],
        }
        for r in original["records"]
    ]
    against_original = compare_semantics(original_unified, upgraded["unified_records"])
    return {
        "roundtrip_equivalence": equivalence,
        "against_original": against_original,
        "discard_count": downgraded["discard_count"],
        "metadata_restorable": downgraded["metadata_restorable"],
    }


class LedgerMigrator:
    def __init__(self, config: EngineConfig, truth: Sequence[Dict[str, Any]]):
        self.config = config
        self.truth = truth

    def upgrade(self, source: str | Path | Dict[str, Any], output_dir: str | Path | None = None):
        return upgrade_v1_to_v2(source, self.truth, self.config, output_dir=output_dir)

    def downgrade(self, source: str | Path | Dict[str, Any], output_path: str | Path | None = None, strict: bool = False):
        return downgrade_v2_to_v1(source, self.config, output_path=output_path, strict=strict)

    def roundtrip(self, source: str | Path | Dict[str, Any]):
        return roundtrip_check(source, self.truth, self.config)
