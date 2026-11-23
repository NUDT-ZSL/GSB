"""Detection-conclusion provenance, replay and stale-result rejection."""

from __future__ import annotations

import json
import os

from .cache import ContentCache, canonical_json, sha256_bytes
from .config import FEATURE_VERSION, PipelineConfig
from .errors import AEDError, Code

NODE_PATH = ["features", "candidates", "segments"]


class TraceLog:
    """Append-only provenance log, one JSON record per detected event."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def record_run(self, dialog_id: str, cfg: PipelineConfig,
                   segments: list, segments_key: str) -> list:
        records = []
        for seg in segments:
            rec = {
                "dialog_id": dialog_id,
                "event_seq": seg["event_seq"],
                "event_type": seg["event_type"],
                "start_ms": seg["start_ms"],
                "end_ms": seg["end_ms"],
                "confidence": seg["confidence"],
                "cache_key": segments_key,
                "template_version": cfg.template_version,
                "feature_version": FEATURE_VERSION,
                "node_path": list(NODE_PATH),
            }
            rec["record_hash"] = sha256_bytes(canonical_json(rec))
            records.append(rec)
        with open(self.path, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return records

    def load(self) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


def check_stale(record: dict, cfg: PipelineConfig,
                feature_version: str = FEATURE_VERSION) -> None:
    """Reject conclusions whose dependency versions have moved on."""
    stale = {}
    if record["template_version"] != cfg.template_version:
        stale["template_version"] = record["template_version"]
    if record["feature_version"] != feature_version:
        stale["feature_version"] = record["feature_version"]
    if stale:
        raise AEDError(Code.E_STALE_RESULT,
                       "conclusion was produced from outdated versions",
                       event_seq=record["event_seq"],
                       dialog_id=record["dialog_id"],
                       old_versions=stale)


def replay(record: dict, cache: ContentCache) -> dict:
    """Reproduce one conclusion from its recorded cache key."""
    blob = cache.get(record["cache_key"])
    if blob is None:
        raise AEDError(Code.E_CACHE_CORRUPT,
                       "recorded cache key no longer resolves",
                       cache_key=record["cache_key"],
                       event_seq=record["event_seq"])
    segments = json.loads(blob.decode())
    for seg in segments:
        if seg["event_seq"] == record["event_seq"]:
            replayed = {
                "event_type": seg["event_type"],
                "start_ms": seg["start_ms"],
                "end_ms": seg["end_ms"],
                "confidence": seg["confidence"],
            }
            expected = {k: record[k] for k in replayed}
            if replayed != expected:
                raise AEDError(Code.E_CACHE_CORRUPT,
                               "replay diverged from the record",
                               event_seq=record["event_seq"])
            return replayed
    raise AEDError(Code.E_CACHE_CORRUPT,
                   "event missing from recorded artifact",
                   event_seq=record["event_seq"])
