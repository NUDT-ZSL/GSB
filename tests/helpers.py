from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from audio_pipeline.seed import generate_fixtures


def make_workspace(root: Path, name: str = "ws") -> Path:
    workspace = root / name
    generate_fixtures(workspace, force=True)
    return workspace


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def configure_compression(
    workspace: Path,
    *,
    threshold_lufs: float,
    ratio: float,
    enabled: bool,
) -> None:
    shard_path = workspace / "criteria" / "v2" / "core.json"
    shard = read_json(shard_path)
    target = None
    for entry in shard["entries"]:
        if entry["record_id"] == "v2-promo-compression":
            entry["suggestion_rule"] = {
                "threshold_lufs": threshold_lufs,
                "ratio": ratio,
                "enabled": enabled,
            }
            target = entry
            break
    if target is None:
        raise AssertionError("compression record not found")
    write_json(shard_path, shard)
    suggestions = json.dumps(target["suggestion_rule"], sort_keys=True)
    payload = json.dumps(target, sort_keys=True)
    db_path = workspace / "criteria" / "v2.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE snapshot_shards SET sha256=? WHERE shard_id='core'",
            (hashlib.sha256(shard_path.read_bytes()).hexdigest(),),
        )
        connection.execute(
            "UPDATE criteria SET suggestion_rule=?, payload_json=? "
            "WHERE record_id='v2-promo-compression'",
            (suggestions, payload),
        )
        connection.commit()
    finally:
        connection.close()
    task_path = workspace / "task.json"
    task = read_json(task_path)
    task["snapshots"]["v2"]["sha256"] = hashlib.sha256(db_path.read_bytes()).hexdigest()
    write_json(task_path, task)
