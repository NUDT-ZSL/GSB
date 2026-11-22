#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256(value: object) -> str:
    import hashlib

    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def main(argv: list[str]) -> int:
    directory = Path(argv[1] if len(argv) > 1 else "data/rules/v2").resolve()
    rows = []
    previous_digest = None
    for index, path in enumerate(sorted(directory.glob("*.json")), start=1):
        document = json.loads(path.read_text(encoding="utf-8"))
        content_digest = sha256(document)
        rows.append(
            {
                "version": document["version"],
                "content_digest": content_digest,
                "effective_clock_ms": int(document["effective_clock_ms"]),
                "generation": int(document["generation"]),
                "rollback_target": document.get("rollback_target"),
                "shard_file": path.name,
                "source_priority": 20,
                "previous_digest": previous_digest,
                "ordinal": index,
            }
        )
        previous_digest = content_digest
    database = directory / "meta.sqlite"
    if database.exists():
        database.unlink()
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE v2_rules (
                ordinal INTEGER PRIMARY KEY,
                version TEXT NOT NULL UNIQUE,
                content_digest TEXT NOT NULL,
                effective_clock_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                rollback_target TEXT,
                shard_file TEXT NOT NULL,
                source_priority INTEGER NOT NULL,
                previous_digest TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO v2_rules(
                ordinal, version, content_digest, effective_clock_ms,
                generation, rollback_target, shard_file, source_priority,
                previous_digest
            ) VALUES(
                :ordinal, :version, :content_digest, :effective_clock_ms,
                :generation, :rollback_target, :shard_file, :source_priority,
                :previous_digest
            )
            """,
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "ok": True,
                "database": str(database),
                "rows": rows,
            },
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
