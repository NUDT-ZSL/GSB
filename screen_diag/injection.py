from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .errors import SchemaError
from .hashing import sha256_hex
from .validation import load_json_file


def _remove_readonly(func: Any, path: str, exc_info: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except Exception:
        shutil.rmtree(path, ignore_errors=True)


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def load_injection_manifest(path: Path | str) -> Dict[str, Any]:
    raw = load_json_file(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise SchemaError("injection manifest must contain a cases list")
    return raw


def get_injection_case(manifest: Dict[str, Any], case_id: str) -> Dict[str, Any]:
    for case in manifest["cases"]:
        if isinstance(case, dict) and case.get("case_id") == case_id:
            return case
    raise SchemaError(f"unknown injection case: {case_id}")


def prepare_injected_sample(
    base_dir: Path | str,
    case_id: str,
    *,
    destination: Path | str,
    injection_manifest_path: Optional[Path | str] = None,
) -> Tuple[Path, Dict[str, Any]]:
    base_dir = Path(base_dir)
    destination = Path(destination)
    manifest_path = Path(injection_manifest_path) if injection_manifest_path else base_dir / "injection_manifest.json"
    injection_manifest = load_injection_manifest(manifest_path)
    case = get_injection_case(injection_manifest, case_id)

    if destination.exists():
        shutil.rmtree(destination, onerror=_remove_readonly)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base_dir, destination)
    for root, _dirs, files in os.walk(destination):
        for name in files:
            try:
                os.chmod(Path(root) / name, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass

    if case_id == "corrupt_digest":
        target = destination / case["target"]
        doc = load_json_file(target)
        record = doc["records"][0]
        record["play_position_ms"] = int(record.get("play_position_ms") or 0) + 1
        _write_json(target, doc)

    elif case_id == "screen_count_mismatch":
        db_path = destination / case.get("target_database", "snapshots/v2/metadata.sqlite")
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE shards SET declared_screen_count = declared_screen_count + 1 WHERE shard_id = ?",
                (case["target_shard_id"],),
            )
            connection.commit()
        finally:
            connection.close()

    elif case_id == "time_out_of_bounds":
        target = destination / case["target"]
        doc = load_json_file(target)
        target_seq = case.get("event_seq", 1)
        changed = False
        for event in doc.get("events", []):
            if event.get("event_seq") == target_seq:
                event["sampled_at_ms"] = int(case["sampled_at_ms"])
                changed = True
                break
        if not changed:
            raise SchemaError("injection could not find target event", {"event_seq": target_seq})
        _write_json(target, doc)

    elif case_id == "replay_drift":
        target = destination / case["target"]
        canonical_id = case["canonical_screen_id"]
        field_name = case["field"]
        new_value = case["new_value"]
        doc = load_json_file(target)
        changed = False
        for record in doc.get("records", []):
            if record.get("canonical_screen_id") == canonical_id:
                record[field_name] = new_value
                changed = True
                break
        if not changed:
            raise SchemaError("replay drift injection could not find target record")
        _write_json(target, doc)
        new_digest = sha256_hex(doc)
        db_path = destination / "snapshots" / "v2" / "metadata.sqlite"
        shard_id = doc["shard_id"]
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("UPDATE shards SET digest = ? WHERE shard_id = ?", (new_digest, shard_id))
            connection.execute(
                f"UPDATE screen_records SET {field_name} = ? WHERE shard_id = ? AND canonical_screen_id = ?",
                (new_value, shard_id, canonical_id),
            )
            connection.commit()
        finally:
            connection.close()

    else:
        raise SchemaError(f"injection case is not implemented by runner: {case_id}")

    return destination, case
