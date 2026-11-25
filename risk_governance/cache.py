"""Content-addressed cache and hash-linked reference chain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_json,
    read_json,
    sha256_json,
)


def build_cache_key(
    *,
    sample_hash: str,
    algorithm_version: str,
    segment_parameter_version: str,
    event_seq: int,
    snapshot_version: int | str,
    extra: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    key_inputs = {
        "upstream_batch_sample_content_hash": sample_hash,
        "sample_content_hash": sample_hash,
        "algorithm_version": algorithm_version,
        "segment_parameter_version": segment_parameter_version,
        "algorithm_and_segment_parameter_version": {
            "algorithm_version": algorithm_version,
            "segment_parameter_version": segment_parameter_version,
        },
        "event_seq": int(event_seq),
        "calibration_event_seq": int(event_seq),
        "snapshot_version": snapshot_version,
        "subordinate_snapshot_version": snapshot_version,
        "dependent_snapshot_version": snapshot_version,
    }
    if extra:
        key_inputs["extra"] = extra
    return sha256_json(key_inputs), key_inputs


class ContentAddressedCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)

    def object_path(self, key: str) -> Path:
        return self.objects / key[:2] / f"{key}.json"

    def has(self, key: str) -> bool:
        return self.object_path(key).is_file()

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.object_path(key)
        if not path.is_file():
            return None
        envelope = read_json(path)
        if envelope.get("cache_key") != key:
            return None
        return envelope

    def put(self, key: str, key_inputs: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
        envelope = {
            "cache_key": key,
            "key_inputs": key_inputs,
            "artifact": artifact,
        }
        atomic_write_json(self.object_path(key), envelope)
        return envelope


class ReferenceChain:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "reference_chain.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(__import__("json").loads(line))
        return rows

    def append_change(
        self,
        *,
        ref_name: str,
        old_key: str | None,
        new_key: str,
        event_seq: int,
        reason: str,
        snapshot_version: int | str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rows = self.entries()
        chain_seq = len(rows) + 1
        prev_hash = rows[-1]["entry_hash"] if rows else "0" * 64
        entry = {
            "chain_seq": chain_seq,
            "ref_name": ref_name,
            "old_key": old_key,
            "new_key": new_key,
            "event_seq": int(event_seq),
            "reason": reason,
            "snapshot_version": snapshot_version,
            "details": details or {},
            "prev_hash": prev_hash,
        }
        entry_hash = sha256_json(entry)
        row = {**entry, "entry_hash": entry_hash}
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            import json

            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            import os

            os.fsync(handle.fileno())
        return row

    def verify(self) -> dict[str, Any]:
        rows = self.entries()
        previous = "0" * 64
        for expected_seq, row in enumerate(rows, 1):
            stored_hash = row.pop("entry_hash", None)
            if row.get("chain_seq") != expected_seq:
                row["entry_hash"] = stored_hash
                return {"valid": False, "reason": "sequence_gap", "seq": expected_seq}
            if row.get("prev_hash") != previous:
                row["entry_hash"] = stored_hash
                return {"valid": False, "reason": "broken_prev_hash", "seq": expected_seq}
            actual_hash = sha256_json(row)
            row["entry_hash"] = stored_hash
            if actual_hash != stored_hash:
                return {"valid": False, "reason": "hash_mismatch", "seq": expected_seq}
            previous = stored_hash
        return {"valid": True, "entries": len(rows), "head_hash": previous}


class CurrentRefs:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"refs": {}}
        return read_json(self.path)

    def get(self, name: str) -> str | None:
        return self.load().get("refs", {}).get(name, {}).get("cache_key")

    def set_many(self, updates: dict[str, dict[str, Any]]) -> None:
        current = self.load()
        refs = current.setdefault("refs", {})
        refs.update(updates)
        atomic_write_json(self.path, current)
