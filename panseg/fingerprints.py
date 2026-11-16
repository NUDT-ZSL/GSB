"""Stable cache keys and dependency fingerprints.

Instance cache keys derive from the run-length content fingerprint, semantic
class, metadata fingerprint and the source format version.  Fusion dependency
fingerprints derive from the participating instance cache keys and the IoU
threshold (plus the retention policy, because policy changes invalidate
downstream fused regions).
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable, Sequence

from .models import InstanceRecord
from .rle import run_content_fingerprint


def metadata_fingerprint(record: InstanceRecord) -> str:
    payload = json.dumps(
        {
            "confidence": round(record.confidence, 12),
            "is_crowd": record.is_crowd,
            "semantic_class": record.semantic_class,
            "palette_name": record.palette_name,
            "palette_rgb": list(record.palette_rgb) if record.palette_rgb else None,
            "source_flags": record.source_flags,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def instance_cache_key(record: InstanceRecord) -> str:
    run_fp = run_content_fingerprint(record.runs, record.height, record.width)
    meta_fp = metadata_fingerprint(record)
    payload = "|".join(
        [
            "instance",
            record.instance_id,
            record.scene_id,
            record.source_version,
            f"{record.width}x{record.height}",
            run_fp,
            record.semantic_class,
            meta_fp,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fusion_dependency_key(
    scene_id: str,
    participant_keys: Sequence[str],
    iou_threshold: float,
    retention_policy: str,
    min_coverage: float,
) -> str:
    payload = json.dumps(
        {
            "scene_id": scene_id,
            "participants": sorted(participant_keys),
            "iou_threshold": round(iou_threshold, 15),
            "retention_policy": retention_policy,
            "min_coverage": round(min_coverage, 15),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_hash(config_dict: dict) -> str:
    payload = json.dumps(config_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def union_cache_keys(keys: Iterable[str]) -> str:
    payload = "\n".join(sorted(set(keys)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
