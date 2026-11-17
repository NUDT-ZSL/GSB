"""Atomic, checksummed incremental diagnostic checkpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .manifest import (
    CHECKPOINT_SCHEMA_VERSION,
    HASH_ALGORITHM_WHITELIST_VERSION,
    ParsedManifest,
)
from .report import atomic_write_json, canonical_json_bytes
from .versioning import Version


class CheckpointError(ValueError):
    pass


@dataclass
class CheckpointState:
    compatible: bool
    entries: dict[str, dict] = field(default_factory=dict)
    sequence: int = 0
    watermark: str | None = None
    reason: str = "not_found"
    present: bool = False


def checkpoint_identity(manifest: ParsedManifest) -> str | None:
    if manifest.project_id is None or manifest.manifest_version is None:
        return None
    return f"{manifest.project_id}\0{manifest.manifest_version.text()}"


def _checksum(payload: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def load_checkpoint(path: str | Path) -> CheckpointState:
    checkpoint_path = Path(path)
    if not checkpoint_path.exists():
        return CheckpointState(compatible=True, reason="not_found")
    try:
        raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return CheckpointState(
            compatible=False,
            reason=f"checkpoint_unreadable:{type(exc).__name__}",
        )
    if not isinstance(raw, dict):
        return CheckpointState(compatible=False, reason="checkpoint_root_not_object")
    payload = raw.get("payload")
    checksum = raw.get("checksum")
    if not isinstance(payload, dict) or not isinstance(checksum, str):
        return CheckpointState(compatible=False, reason="checkpoint_structure_invalid")
    if _checksum(payload) != checksum:
        return CheckpointState(compatible=False, reason="checkpoint_checksum_mismatch")
    if payload.get("complete") is not True:
        return CheckpointState(compatible=False, reason="checkpoint_incomplete")
    if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return CheckpointState(
            compatible=False,
            reason="checkpoint_schema_version_changed",
        )
    if (
        payload.get("hash_algorithm_whitelist_version")
        != HASH_ALGORITHM_WHITELIST_VERSION
    ):
        return CheckpointState(
            compatible=False,
            reason="hash_algorithm_whitelist_version_changed",
        )
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return CheckpointState(compatible=False, reason="checkpoint_entries_invalid")
    entries: dict[str, dict] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            return CheckpointState(compatible=False, reason="checkpoint_entry_invalid")
        key = entry.get("key")
        result = entry.get("result")
        if not isinstance(key, str) or not isinstance(result, dict):
            return CheckpointState(compatible=False, reason="checkpoint_entry_invalid")
        if result.get("status") == "failed":
            continue
        entries[key] = entry
    try:
        sequence = int(payload.get("sequence", 0))
    except (TypeError, ValueError):
        sequence = 0
    return CheckpointState(
        compatible=True,
        entries=entries,
        sequence=sequence,
        watermark=payload.get("watermark"),
        reason="loaded",
        present=True,
    )


def _aggregate(entries: Iterable[dict]) -> dict:
    entry_list = list(entries)
    manifests = [entry["result"] for entry in entry_list]
    totals: dict[str, int] = {}
    for manifest in manifests:
        counts = manifest.get("counts", {})
        for key, value in counts.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
    return {
        "applied_manifest_count": len(manifests),
        "success_manifest_count": sum(
            manifest.get("status") == "success" for manifest in manifests
        ),
        "component_totals": dict(sorted(totals.items())),
    }


def _watermark(entries: Iterable[dict]) -> str | None:
    versions: list[Version] = []
    for entry in entries:
        raw = entry.get("manifest_version")
        if raw is None:
            continue
        try:
            versions.append(Version.parse(str(raw)))
        except ValueError:
            continue
    if not versions:
        return None
    return max(versions).text()


def save_checkpoint(
    path: str | Path,
    *,
    entries: Iterable[dict],
    sequence: int = 0,
) -> dict:
    ordered_entries = sorted(entries, key=lambda entry: entry["key"])
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "hash_algorithm_whitelist_version": HASH_ALGORITHM_WHITELIST_VERSION,
        "complete": True,
        "sequence": int(sequence),
        "watermark": _watermark(ordered_entries),
        "aggregate": _aggregate(ordered_entries),
        "entries": ordered_entries,
    }
    document = {"checksum": _checksum(payload), "payload": payload}
    atomic_write_json(path, document)
    return payload


def make_checkpoint_entry(
    manifest: ParsedManifest,
    result: dict,
) -> dict | None:
    key = checkpoint_identity(manifest)
    if key is None or result.get("status") not in {"success", "partial"}:
        return None
    return {
        "key": key,
        "project_id": manifest.project_id,
        "manifest_version": manifest.manifest_version_text,
        "content_digest": manifest.content_digest,
        "file_digest": manifest.file_digest,
        "status": result["status"],
        "result": result,
    }

