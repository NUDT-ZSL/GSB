"""Cross-version phoneme/viseme normalization tables."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .catalog import (
    CANONICAL_CHANNELS,
    CLOSED_VISEMES,
    LIP_CHANNELS,
    VISEME_TARGETS,
    phoneme_reference,
)
from .models import VersionData


def build_mapping_table(versions: list[VersionData]) -> dict[str, Any]:
    labels_by_version: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    canonical_ids: dict[str, set[str]] = defaultdict(set)
    for version in versions:
        for sentence in version.sentences:
            for phoneme in sentence.phonemes:
                labels_by_version[phoneme.canonical][version.version].add(
                    phoneme.source_label
                )
                canonical_ids[phoneme.canonical].add(phoneme.id)
                occurrences[phoneme.canonical].append(
                    {
                        "version": version.version,
                        "sentence_id": sentence.id,
                        "phoneme_id": phoneme.id,
                        "source_label": phoneme.source_label,
                        "start_ms": round(phoneme.start_us / 1000.0, 6),
                        "end_ms": round(phoneme.end_us / 1000.0, 6),
                        "viseme": phoneme.viseme,
                        "targets": dict(phoneme.targets),
                    }
                )
    entries = []
    for canonical in sorted(labels_by_version):
        reference = phoneme_reference(canonical)
        target_rows = [
            item["targets"]
            for item in occurrences[canonical]
        ]
        consistent = all(row == target_rows[0] for row in target_rows)
        entries.append(
            {
                "canonical": canonical,
                "category": reference["category"],
                "viseme": reference["viseme"],
                "targets": reference["targets"],
                "labels_by_version": {
                    version: sorted(labels)
                    for version, labels in sorted(labels_by_version[canonical].items())
                },
                "phoneme_id_count": len(canonical_ids[canonical]),
                "normalization_consistent": consistent,
                "occurrences": occurrences[canonical],
            }
        )
    all_targets_consistent = all(
        entry["normalization_consistent"] for entry in entries
    )
    return {
        "format": "museum-lipsync-viseme-mapping/v1",
        "channels": list(CANONICAL_CHANNELS),
        "lip_channels": list(LIP_CHANNELS),
        "coordinate_ranges": {
            "jaw_open": [0.0, 1.0],
            "lip_width": [0.0, 1.0],
            "mouth_corner": [-1.0, 1.0],
            "brow_raise": [-1.0, 1.0],
            "brow_inner_up": [-1.0, 1.0],
        },
        "visemes": {
            viseme: {
                "targets": dict(targets),
                "closed_lips": viseme in CLOSED_VISEMES,
                "jaw_open_zero": targets["jaw_open"] == 0.0,
            }
            for viseme, targets in sorted(VISEME_TARGETS.items())
        },
        "closed_visemes_zero_jaw": all(
            VISEME_TARGETS[viseme]["jaw_open"] == 0.0 for viseme in CLOSED_VISEMES
        ),
        "all_phoneme_targets_consistent": all_targets_consistent,
        "phonemes": entries,
    }


def mapping_digest_payload(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        "visemes": mapping["visemes"],
        "all_phoneme_targets_consistent": mapping["all_phoneme_targets_consistent"],
        "phonemes": [
            {
                "canonical": item["canonical"],
                "category": item["category"],
                "viseme": item["viseme"],
                "targets": item["targets"],
                "labels_by_version": item["labels_by_version"],
            }
            for item in mapping["phonemes"]
        ],
    }
