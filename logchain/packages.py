from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ConflictError, ValidationError
from .merge import _normalize_v2, sort_chain
from .store import connect_state, quote_identifier
from .engine import CHAIN_TABLE_COLUMNS, _insert_chain_entries, _stage_table_name, _view_table_name
from .util import canonical_hash, file_lock, read_json, sha256_file, stable_json
from .validation import effective_clock_ms


def load_package_manifest(package_dir: Path) -> tuple[dict[str, Any], Path]:
    for filename in ("package_manifest.json", "manifest.json"):
        path = package_dir / filename
        if path.exists():
            return read_json(path), path
    raise ValidationError(
        "PACKAGE_MANIFEST_MISSING",
        "snapshot package manifest was not found",
        {"package_dir": str(package_dir)},
    )


def validate_package(package_dir: Path) -> dict[str, Any]:
    manifest, manifest_path = load_package_manifest(package_dir)
    if not isinstance(manifest, dict):
        raise ValidationError("PACKAGE_MANIFEST_INVALID", "package manifest must be an object", {})
    version = manifest.get("manifest_version", manifest.get("package_version"))
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise ValidationError("PACKAGE_VERSION_INVALID", "manifest_version must be a nonnegative integer", {})
    for field in ("generation_min", "generation_max"):
        if not isinstance(manifest.get(field), int):
            raise ValidationError("PACKAGE_SCHEMA_INVALID", f"{field} must be an integer", {field: manifest.get(field)})
    shard_refs = manifest.get("shards")
    if not isinstance(shard_refs, list) or not shard_refs:
        raise ValidationError("PACKAGE_SCHEMA_INVALID", "package requires shards list", {})

    entries: list[dict[str, Any]] = []
    shards: list[dict[str, Any]] = []
    for ref in shard_refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
            raise ValidationError("PACKAGE_SCHEMA_INVALID", "invalid shard reference", {"ref": ref})
        shard_path = package_dir / ref["path"]
        if not shard_path.exists():
            raise ValidationError("V2_SHARD_MISSING", "package shard is missing", {"path": ref["path"]})
        actual_hash = sha256_file(shard_path)
        if ref.get("sha256") != actual_hash:
            raise ValidationError(
                "V2_SHARD_HASH_MISMATCH",
                "package shard hash mismatch",
                {"path": ref["path"], "expected": ref.get("sha256"), "actual": actual_hash},
            )
        shard = read_json(shard_path)
        shard_entries = shard.get("entries", []) if isinstance(shard, dict) else []
        if not isinstance(shard_entries, list):
            raise ValidationError("PACKAGE_SCHEMA_INVALID", "shard entries must be a list", {"path": ref["path"]})
        entries.extend(shard_entries)
        shards.append({**ref, "sha256": actual_hash})

    set_digest = canonical_hash(entries)
    if manifest.get("set_digest") and manifest["set_digest"] != set_digest:
        raise ValidationError(
            "V2_SET_DIGEST_MISMATCH",
            "package set_digest mismatch",
            {"expected": manifest["set_digest"], "actual": set_digest},
        )
    normalized = _normalize_v2(entries)
    chain = sort_chain(normalized)
    for index, entry in enumerate(chain):
        entry["chain_order"] = index
    return {
        **manifest,
        "manifest_version": version,
        "shards": shards,
        "set_digest": set_digest,
        "_chain": chain,
        "_manifest_path": str(manifest_path),
    }


def _current_package(store: Any) -> Any | None:
    key_row = store.conn.execute(
        "SELECT value FROM meta WHERE key = 'current_package_key'"
    ).fetchone()
    if key_row is None:
        return None
    return store.conn.execute(
        "SELECT * FROM snapshot_packages WHERE package_key = ?", (key_row["value"],)
    ).fetchone()


def receive_package(
    root: Path,
    package_dir: Path,
    clock_ms: int | None = None,
) -> dict[str, Any]:
    clock = effective_clock_ms(root, clock_ms)
    incoming = validate_package(package_dir)
    digest = str(incoming["set_digest"])
    version = int(incoming["manifest_version"])
    store = connect_state(root)
    try:
        with file_lock(root / "runtime" / "locks" / "snapshot-package.lock"):
            current = _current_package(store)
            if current is not None:
                current_manifest = json.loads(current["manifest_json"])
                current_version = int(current["manifest_version"])
                current_shards = {
                    str(ref["path"]): str(ref["sha256"])
                    for ref in current_manifest.get("shards", [])
                }
                incoming_shards = {str(ref["path"]): str(ref["sha256"]) for ref in incoming["shards"]}
                same_content = (
                    current["set_digest"] == digest
                    and current_shards == incoming_shards
                    and int(current["generation_min"]) == int(incoming["generation_min"])
                    and int(current["generation_max"]) == int(incoming["generation_max"])
                )
                if version == current_version and same_content:
                    return {
                        "ok": True,
                        "command": "load",
                        "decision": "noop",
                        "package_version": version,
                        "set_digest": digest,
                    }
                if version == current_version:
                    raise ConflictError(
                        "SNAPSHOT_VERSION_CONFLICT",
                        "same manifest version has different content",
                        {"version": version},
                    )
                if version < current_version:
                    raise ConflictError(
                        "SNAPSHOT_VERSION_ROLLBACK",
                        "snapshot package version rollback is rejected",
                        {"incoming_version": version, "current_version": current_version},
                    )

                retained = all(
                    path in incoming_shards and incoming_shards[path] == sha256
                    for path, sha256 in current_shards.items()
                )
                lower_bound_same = int(current["generation_min"]) == int(incoming["generation_min"])
                if retained and lower_bound_same:
                    decision = "incremental_append"
                else:
                    decision = "full_replace"
            else:
                decision = "full_replace"

            package_key = f"v2-package-v{version}-{digest[:12]}"
            sequence = store.next_sequence("package_sequence")
            with store.immediate():
                store.conn.execute(
                    """
                    INSERT INTO snapshot_packages(
                        package_key, manifest_version, generation_min, generation_max,
                        set_digest, decision, clock_ms, manifest_json, created_sequence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        package_key,
                        version,
                        int(incoming["generation_min"]),
                        int(incoming["generation_max"]),
                        digest,
                        decision,
                        clock,
                        stable_json({key: value for key, value in incoming.items() if not key.startswith("_")}),
                        sequence,
                    ),
                )

            view_id = f"pkg-{digest[:16]}"
            final_table = _view_table_name(view_id)
            stage_table = _stage_table_name(view_id)
            chain = incoming["_chain"]
            store.conn.execute(f"DROP TABLE IF EXISTS {quote_identifier(stage_table)}")
            store.conn.execute(f"CREATE TABLE {quote_identifier(stage_table)} ({CHAIN_TABLE_COLUMNS})")
            _insert_chain_entries(store.conn, stage_table, chain)
            with store.immediate():
                store.conn.execute(
                    f"ALTER TABLE {quote_identifier(stage_table)} RENAME TO {quote_identifier(final_table)}"
                )
                store.conn.execute(
                    """
                    INSERT OR REPLACE INTO chain_views(
                        view_id, clock_ms, chain_digest, view_table, generation_min,
                        generation_max, status, created_sequence
                    ) VALUES (?, ?, ?, ?, ?, ?, 'package', ?)
                    """,
                    (
                        view_id,
                        clock,
                        digest,
                        final_table,
                        int(incoming["generation_min"]),
                        int(incoming["generation_max"]),
                        sequence,
                    ),
                )
                store.conn.execute(
                    """
                    INSERT OR REPLACE INTO pointer_state(
                        name, view_id, view_table, version_integer, clock_ms, chain_digest
                    ) VALUES ('current_snapshot_package', ?, ?, ?, ?, ?)
                    """,
                    (view_id, final_table, version, clock, digest),
                )
                store.conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('current_package_key', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (package_key,),
                )
            return {
                "ok": True,
                "command": "load",
                "decision": decision,
                "package_key": package_key,
                "package_version": version,
                "set_digest": digest,
                "view_id": view_id,
                "view_table": final_table,
            }
    finally:
        store.close()
