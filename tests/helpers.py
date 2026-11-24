from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from certpath.canon import canonical_bytes, strict_json_loads


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any:
    return strict_json_loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_bytes(value) + b"\n")


def chain_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["chain_id"]: item for item in report["chains"]}


def append_v2_record(sample_dir: Path, record: dict[str, Any]) -> None:
    v2_dir = sample_dir / "snapshots/v2"
    shard_path = v2_dir / "shards/shard_0.json"
    shard = load_json(shard_path)
    shard["records"].append(record)
    write_json(shard_path, shard)
    connection = sqlite3.connect(v2_dir / "meta.db")
    try:
        digest = hashlib.sha256(shard_path.read_bytes()).hexdigest()
        connection.execute(
            "UPDATE shards SET record_count=?, digest=? WHERE shard_id=?",
            (len(shard["records"]), digest, shard["shard_id"]),
        )
        connection.commit()
    finally:
        connection.close()


def update_v2_anchor(
    sample_dir: Path,
    anchor_id: str,
    **changes: Any,
) -> None:
    v2_dir = sample_dir / "snapshots/v2"
    for shard_path in sorted((v2_dir / "shards").glob("*.json")):
        shard = load_json(shard_path)
        changed = False
        for record in shard["records"]:
            if record.get("anchor_id") == anchor_id:
                record.update(changes)
                changed = True
        if not changed:
            continue
        write_json(shard_path, shard)
        connection = sqlite3.connect(v2_dir / "meta.db")
        try:
            digest = hashlib.sha256(shard_path.read_bytes()).hexdigest()
            connection.execute(
                "UPDATE shards SET digest=? WHERE shard_id=?",
                (digest, shard["shard_id"]),
            )
            connection.commit()
        finally:
            connection.close()
        return
    raise AssertionError(f"v2 anchor not found: {anchor_id}")


def run_python(
    code: str,
    *args: str,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    import os
    import subprocess

    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT)
    if env:
        merged_env.update(env)
    process = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return process.returncode, process.stdout, process.stderr
