"""Content-addressed cache and deterministic suffix invalidation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .canonical import atomic_write_json, canonical_bytes, read_json, sha256_hex


class ContentCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.object_root = self.root / "objects"
        self.object_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_cache_key(
        *,
        category: str,
        pcm_hash: str,
        frame_params_hash: str,
        annotation_generation: int | str,
        upstream_hashes: Sequence[str] | None = None,
        frame_index: int | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> str:
        payload = {
            "category": category,
            "pcm_sha256": pcm_hash,
            "frame_params_sha256": frame_params_hash,
            "annotation_generation": int(annotation_generation),
            "upstream_hashes": list(upstream_hashes or []),
            "frame_index": frame_index,
            "extra": extra or {},
        }
        return sha256_hex(payload)

    def object_path(self, content_hash: str) -> Path:
        return self.object_root / content_hash[:2] / f"{content_hash}.json"

    def put(self, payload: Any) -> str:
        content_hash = sha256_hex(payload)
        path = self.object_path(content_hash)
        if not path.exists():
            atomic_write_json(path, payload)
        return content_hash

    def get(self, content_hash: str) -> Any:
        return read_json(self.object_path(content_hash))

    def has(self, content_hash: str) -> bool:
        return self.object_path(content_hash).exists()


def row_hash(row: Dict[str, Any]) -> str:
    return sha256_hex(canonical_bytes(row))


def diff_truth(
    old_truth: Sequence[Dict[str, Any]], new_truth: Sequence[Dict[str, Any]]
) -> List[int]:
    old_by_frame = {int(row["frame_index"]): row for row in old_truth}
    changed: List[int] = []
    for row in new_truth:
        index = int(row["frame_index"])
        old_row = old_by_frame.get(index)
        if old_row is None or row_hash(old_row) != row_hash(row):
            changed.append(index)
    old_frames = set(old_by_frame)
    new_frames = {int(row["frame_index"]) for row in new_truth}
    for index in sorted(old_frames - new_frames):
        changed.append(index)
    return sorted(set(changed))


def diff_ledger_records(
    old_records: Sequence[Dict[str, Any]], new_records: Sequence[Dict[str, Any]]
) -> List[int]:
    def key(record: Dict[str, Any]) -> int:
        return int(record.get("frame_start", record.get("frame_index", 0)))

    old_by_frame = {key(record): record for record in old_records}
    changed: List[int] = []
    for record in new_records:
        frame = key(record)
        old = old_by_frame.get(frame)
        comparable_new = {
            "emotion_tier": record.get("emotion_tier"),
            "speaker_id": record.get("speaker_id"),
            "generation": record.get("generation"),
            "revision": record.get("revision"),
            "abs_ms": record.get("abs_ms", record.get("abs_start_ms")),
        }
        if old is None:
            changed.append(frame)
            continue
        comparable_old = {
            "emotion_tier": old.get("emotion_tier"),
            "speaker_id": old.get("speaker_id"),
            "generation": old.get("generation"),
            "revision": old.get("revision"),
            "abs_ms": old.get("abs_ms", old.get("abs_start_ms")),
        }
        if sha256_hex(comparable_old) != sha256_hex(comparable_new):
            changed.append(frame)
    return sorted(set(changed))


def ranges_from_changed(
    changed_frames: Sequence[int], frame_count: int
) -> Dict[str, List[List[int]]]:
    if not changed_frames:
        return {"recompute_ranges": [], "reused_ranges": [[0, frame_count]] if frame_count else []}
    first = max(0, min(frame_count - 1, min(int(x) for x in changed_frames)))
    recompute = [[first, frame_count]] if first < frame_count else []
    reused = [[0, first]] if first > 0 else []
    return {"recompute_ranges": recompute, "reused_ranges": reused}


def plan_truth_invalidation(
    old_truth: Sequence[Dict[str, Any]],
    new_truth: Sequence[Dict[str, Any]],
    frame_count: int,
) -> Dict[str, Any]:
    changed = diff_truth(old_truth, new_truth)
    ranges = ranges_from_changed(changed, frame_count)
    return {
        "trigger": "truth_frame_correction",
        "changed_frames": changed,
        "earliest_changed_frame": changed[0] if changed else None,
        "policy": "state transition is sequential; earliest correction invalidates its suffix",
        **ranges,
    }


def plan_generation_invalidation(
    old_records: Sequence[Dict[str, Any]],
    new_records: Sequence[Dict[str, Any]],
    frame_count: int,
    *,
    new_generation: int,
    old_generation: int,
) -> Dict[str, Any]:
    changed = diff_ledger_records(old_records, new_records)
    if int(new_generation) > int(old_generation) and not changed:
        changed = [0]
    ranges = ranges_from_changed(changed, frame_count)
    return {
        "trigger": "v2_higher_generation",
        "old_generation": int(old_generation),
        "new_generation": int(new_generation),
        "changed_frames": changed,
        "earliest_changed_frame": changed[0] if changed else None,
        "policy": "ledger aggregation and downstream report reuse only the committed prefix",
        **ranges,
    }


def merge_ranges(ranges: Iterable[Sequence[int]]) -> List[List[int]]:
    normalized = sorted((int(start), int(end)) for start, end in ranges if end > start)
    merged: List[List[int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged
