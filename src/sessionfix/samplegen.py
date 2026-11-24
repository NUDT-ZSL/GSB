from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from .util import atomic_write_json, canonical_json


DEFAULT_SEED = 20261125
DEFAULT_OBSERVED_CLOCK_MS = 1_893_456_000_000


def _session(
    session_id: str,
    user_id: str,
    issued_at_ms: int,
    device_id: str,
    generation: int,
    parent_session_id: str | None,
    policy_key: str,
    window_start_ms: int,
    window_end_ms: int,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "user_id": user_id,
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": window_end_ms,
        "device_id": device_id,
        "generation": generation,
        "parent_session_id": parent_session_id,
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "policy_key": policy_key,
    }


def _event(
    event_seq: int,
    session_id: str,
    event_time_ms: int,
    user_id: str,
    device_id: str,
    *,
    result: str = "success",
    privilege_escalated: bool = False,
    event_type: str = "login",
) -> dict[str, Any]:
    return {
        "event_seq": event_seq,
        "session_id": session_id,
        "event_time_ms": event_time_ms,
        "attempt_result": result,
        "privilege_escalated": privilege_escalated,
        "event_type": event_type,
        "user_id": user_id,
        "device_id": device_id,
    }


def _build_policy_records(
    observed_clock_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    v1_records = [
        {
            "policy_key": "default",
            "relative_start_seconds": 0,
            "rotation_mode": "enforce",
            "min_rotation_interval_seconds": 120,
            "bind_device_required": True,
            "policy_generation": 1,
        },
        {
            "policy_key": "legacy",
            "relative_start_seconds": 15,
            "rotation_mode": "enforce",
            "min_rotation_interval_seconds": 300,
            "bind_device_required": False,
            "policy_generation": 1,
        },
    ]
    v2_records = [
        {
            "policy_key": "default",
            "effective_at_ms": observed_clock_ms - 1000,
            "policy_generation": 2,
            "rotation_mode": "enforce",
            "min_rotation_interval_seconds": 120,
            "bind_device_required": True,
            "tombstone": False,
        },
        {
            "policy_key": "retired",
            "effective_at_ms": observed_clock_ms - 2000,
            "policy_generation": 1,
            "rotation_mode": "enforce",
            "min_rotation_interval_seconds": 60,
            "bind_device_required": True,
            "tombstone": False,
        },
        {
            "policy_key": "retired",
            "effective_at_ms": observed_clock_ms - 500,
            "policy_generation": 5,
            "rotation_mode": "observe",
            "min_rotation_interval_seconds": 0,
            "bind_device_required": False,
            "tombstone": True,
        },
    ]
    return v1_records, v2_records


def _write_v2(
    root: Path,
    records: list[dict[str, Any]],
    observed_clock_ms: int,
) -> None:
    v2_dir = root / "policies" / "v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    first = records[:2]
    second = records[2:]
    shard_files = [
        ("shard-000.json", first),
        ("shard-001.json", second),
    ]
    shard_entries: list[dict[str, Any]] = []
    for name, shard_records in shard_files:
        data = (
            json.dumps(
                {"records": shard_records},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        path = v2_dir / name
        path.write_bytes(data)
        shard_entries.append(
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "record_count": len(shard_records),
            }
        )
    index = {
        "format": "v2",
        "observed_clock_ms": observed_clock_ms,
        "metadata_db": "policy_meta.sqlite3",
        "shards": shard_entries,
    }
    atomic_write_json(v2_dir / "index.json", index)

    db_path = v2_dir / "policy_meta.sqlite3"
    temp_path = v2_dir / ".policy_meta.sqlite3.tmp"
    if temp_path.exists():
        temp_path.unlink()
    connection = sqlite3.connect(temp_path)
    try:
        connection.executescript(
            """
            CREATE TABLE policy_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE policy_shards (
                path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO policy_metadata(key, value) VALUES(?, ?)",
            [
                ("format_version", "2"),
                ("shard_count", str(len(shard_entries))),
                ("record_count", str(len(records))),
            ],
        )
        connection.executemany(
            "INSERT INTO policy_shards(path, sha256, record_count) VALUES(?, ?, ?)",
            [
                (item["path"], item["sha256"], item["record_count"])
                for item in shard_entries
            ],
        )
        connection.commit()
    finally:
        connection.close()
    temp_path.replace(db_path)


def generate_sample(
    output_dir: Path,
    *,
    seed: int = DEFAULT_SEED,
    sample_id: str | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if sample_id is None:
        sample_id = f"sessionfix-fixed-{seed}"
    rng = random.Random(seed)
    clock = DEFAULT_OBSERVED_CLOCK_MS + rng.randrange(0, 10) * 1000
    window_start = clock - 1_200_000
    window_end = clock + 3_600_000

    sessions = [
        _session(
            "sf-vuln-0000",
            "user-vuln",
            clock - 900_000,
            "device-vuln",
            0,
            None,
            "default",
            window_start,
            window_end,
        ),
        _session(
            "sf-fixed-0000",
            "user-fixed",
            clock - 950_000,
            "device-fixed",
            0,
            None,
            "default",
            window_start,
            window_end,
        ),
        _session(
            "sf-fixed-0001",
            "user-fixed",
            clock - 690_000,
            "device-fixed",
            1,
            "sf-fixed-0000",
            "default",
            window_start,
            window_end,
        ),
        _session(
            "orphan-0000",
            "user-orphan",
            clock - 900_000,
            "device-orphan",
            0,
            None,
            "default",
            window_start,
            window_end,
        ),
        _session(
            "orphan-0001",
            "user-orphan",
            clock - 750_000,
            "device-orphan",
            1,
            "orphan-0000",
            "default",
            window_start,
            window_end,
        ),
        _session(
            "fast-base",
            "user-fast",
            clock - 900_000,
            "device-fast",
            0,
            None,
            "default",
            window_start,
            window_end,
        ),
        _session(
            "fast-a",
            "user-fast",
            clock - 850_000,
            "device-fast",
            1,
            "fast-base",
            "default",
            window_start,
            window_end,
        ),
        _session(
            "fast-b",
            "user-fast",
            clock - 845_000,
            "device-fast",
            1,
            "fast-base",
            "default",
            window_start,
            window_end,
        ),
        _session(
            "revoke-a",
            "user-revoke",
            clock - 900_000,
            "device-revoke",
            0,
            None,
            "default",
            window_start,
            window_end,
        ),
        _session(
            "revoke-b",
            "user-revoke",
            clock - 800_000,
            "device-revoke",
            1,
            "revoke-a",
            "default",
            window_start,
            window_end,
        ),
        _session(
            "revoke-c",
            "user-revoke",
            clock - 700_000,
            "device-revoke",
            2,
            "revoke-b",
            "default",
            window_start,
            window_end,
        ),
        _session(
            "bind-0000",
            "user-bind",
            clock - 900_000,
            "device-bind",
            0,
            None,
            "default",
            window_start,
            window_end,
        ),
        _session(
            "retired-0000",
            "user-retired",
            clock - 900_000,
            "device-retired",
            0,
            None,
            "retired",
            window_start,
            window_end,
        ),
    ]
    events = [
        _event(
            1,
            "sf-vuln-0000",
            clock - 600_000,
            "user-vuln",
            "device-vuln",
            privilege_escalated=True,
        ),
        _event(
            2,
            "sf-fixed-0000",
            clock - 700_000,
            "user-fixed",
            "device-fixed",
            privilege_escalated=True,
        ),
        _event(
            3,
            "orphan-0000",
            clock - 800_000,
            "user-orphan",
            "device-orphan",
            privilege_escalated=True,
        ),
        _event(
            4,
            "orphan-0000",
            clock - 700_000,
            "user-orphan",
            "device-orphan",
            event_type="access",
        ),
        _event(
            5,
            "fast-base",
            clock - 880_000,
            "user-fast",
            "device-fast",
            privilege_escalated=True,
        ),
        _event(
            6,
            "bind-0000",
            clock - 600_000,
            "user-bind",
            "device-other",
        ),
    ]
    random.Random(seed + 1).shuffle(events)

    manifest = {
        "sample_id": sample_id,
        "seed": seed,
        "observed_clock_ms": clock,
        "files": {
            "sessions": "sessions.json",
            "events": "events.json",
            "policy_v1": "policies/v1.json",
            "policy_v2": "policies/v2",
        },
    }
    v1_records, v2_records = _build_policy_records(clock)
    atomic_write_json(output_dir / "manifest.json", manifest)
    atomic_write_json(output_dir / "sessions.json", {"sessions": sessions})
    atomic_write_json(output_dir / "events.json", {"events": events})
    atomic_write_json(
        output_dir / "policies" / "v1.json",
        {"format": "v1", "records": v1_records},
    )
    _write_v2(output_dir, v2_records, clock)
    summary = {
        "sample_id": sample_id,
        "seed": seed,
        "observed_clock_ms": clock,
        "output": str(output_dir),
        "sessions": len(sessions),
        "events": len(events),
        "policy_v1_records": len(v1_records),
        "policy_v2_records": len(v2_records),
        "digest": hashlib.sha256(
            canonical_json(
                {
                    "manifest": manifest,
                    "sessions": sessions,
                    "events": events,
                    "v1": v1_records,
                    "v2": v2_records,
                }
            ).encode("utf-8")
        ).hexdigest(),
    }
    return summary
