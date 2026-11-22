#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "samples" / "v2"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(record: dict[str, Any], shard_name: str) -> str:
    payload = {
        "canonical_name": record["canonical_name"],
        "service": record["service"],
        "instances": record["instances"],
        "severity": record["severity"],
        "effective_at_ms": record["effective_at_ms"],
        "generation": record["generation"],
        "upstream_dependencies": record["upstream_dependencies"],
        "silence_labels": record["silence_labels"],
        "tombstone": record["tombstone"],
        "shard_name": shard_name,
    }
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def main() -> None:
    index_path = V2 / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    shard_names = [item["path"] for item in index["shards"]]
    all_records: list[tuple[str, dict[str, Any]]] = []

    for shard_name in shard_names:
        shard_path = V2 / shard_name
        shard = json.loads(shard_path.read_text(encoding="utf-8"))
        for record in shard["rules"]:
            record["record_hash"] = record_hash(record, shard_name)
            all_records.append((shard_name, record))
        shard_path.write_text(
            json.dumps(shard, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

    for descriptor in index["shards"]:
        data = (V2 / descriptor["path"]).read_bytes()
        descriptor["sha256"] = hashlib.sha256(data).hexdigest()
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    database_path = V2 / index["metadata_db"]
    temporary = database_path.with_suffix(".tmp")
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    try:
        connection.execute(
            """
            CREATE TABLE v2_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE v2_rules (
                canonical_name TEXT NOT NULL,
                service TEXT NOT NULL,
                instances_json TEXT NOT NULL,
                severity TEXT NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                upstream_dependencies_json TEXT NOT NULL,
                silence_labels_json TEXT NOT NULL,
                tombstone INTEGER NOT NULL,
                shard_name TEXT NOT NULL,
                record_hash TEXT NOT NULL,
                PRIMARY KEY (canonical_name, generation)
            )
            """
        )
        meta = {
            "snapshot_id": index["snapshot_id"],
            "snapshot_version": index["snapshot_version"],
            "logical_clock_ms": index["logical_clock_ms"],
            "format_priority": index["format_priority"],
        }
        connection.executemany(
            "INSERT INTO v2_meta(key, value) VALUES (?, ?)",
            [(key, str(value)) for key, value in sorted(meta.items())],
        )
        for shard_name, record in sorted(
            all_records, key=lambda item: (item[1]["canonical_name"], item[1]["generation"])
        ):
            connection.execute(
                """
                INSERT INTO v2_rules(
                    canonical_name, service, instances_json, severity,
                    effective_at_ms, generation, upstream_dependencies_json,
                    silence_labels_json, tombstone, shard_name, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["canonical_name"],
                    record["service"],
                    canonical(record["instances"]),
                    record["severity"],
                    record["effective_at_ms"],
                    record["generation"],
                    canonical(record["upstream_dependencies"]),
                    canonical(record["silence_labels"]),
                    1 if record["tombstone"] else 0,
                    shard_name,
                    record["record_hash"],
                ),
            )
        connection.commit()
    finally:
        connection.close()
    temporary.replace(database_path)
    print(f"built {database_path}")


if __name__ == "__main__":
    main()
