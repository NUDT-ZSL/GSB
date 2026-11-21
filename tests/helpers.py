from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from config_engine.canonical import pretty_json, sha256_bytes
from config_engine.models import content_digest


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample"
PYTHON = sys.executable


def copy_sample(destination: Path) -> Path:
    package = destination / "sample"
    shutil.copytree(SAMPLE, package)
    return package / "package.json"


def write_json(path: Path, value: Any) -> None:
    path.write_text(pretty_json(value) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rebuild_v2_metadata(package_path: Path) -> None:
    package = read_json(package_path)
    base = package_path.parent
    shard_paths = []
    for pattern in package["v2"]["shards"]:
        shard_paths.extend(sorted(base.glob(pattern)))
    metadata_path = base / package["v2"]["metadata_db"]
    if metadata_path.exists():
        metadata_path.unlink()
    conn = sqlite3.connect(metadata_path)
    try:
        conn.executescript(
            """
            CREATE TABLE shard_registry (
                shard_path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                schema_version INTEGER NOT NULL
            );
            CREATE TABLE config_metadata (
                service TEXT NOT NULL,
                environment TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                shard_path TEXT NOT NULL,
                record_digest TEXT NOT NULL,
                tombstone INTEGER NOT NULL,
                PRIMARY KEY (service, environment, generation, record_digest)
            );
            """
        )
        for shard_path in shard_paths:
            document = read_json(shard_path)
            conn.execute(
                "INSERT INTO shard_registry(shard_path, sha256, logical_clock_ms, "
                "schema_version) VALUES(?, ?, ?, 2)",
                (
                    shard_path.name,
                    sha256_bytes(shard_path.read_bytes()),
                    document["logical_clock_ms"],
                ),
            )
            for record in document["records"]:
                digest = content_digest(
                    service=record["service"],
                    environment=record["environment"],
                    generation=record["generation"],
                    version=record["version"],
                    params=record["params"],
                    upstreams=record.get("upstreams", []),
                    tombstone=record.get("tombstone", False),
                    expires_at_ms=record.get("expires_at_ms"),
                )
                conn.execute(
                    "INSERT INTO config_metadata(service, environment, generation, "
                    "effective_at_ms, logical_clock_ms, shard_path, record_digest, tombstone) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["service"],
                        record["environment"],
                        record["generation"],
                        record["effective_at_ms"],
                        document["logical_clock_ms"],
                        shard_path.name,
                        digest,
                        int(record.get("tombstone", False)),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def run_cli(
    db: Path,
    package: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> tuple[int, dict[str, Any]]:
    command = [
        PYTHON,
        "-m",
        "config_engine",
        "--db",
        str(db),
        "--package",
        str(package),
        *arguments,
    ]
    process_env = os.environ.copy()
    process_env.pop("CONFIG_ENGINE_CRASH_AFTER_STEP", None)
    if env:
        process_env.update(env)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if (
        env
        and env.get("CONFIG_ENGINE_CRASH_AFTER_STEP")
        and completed.returncode == 97
        and not completed.stdout
    ):
        return completed.returncode, {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"non-JSON CLI output: stdout={completed.stdout!r} "
            f"stderr={completed.stderr!r}"
        ) from exc
    if check and completed.returncode != 0:
        raise AssertionError(
            f"CLI failed: code={completed.returncode} output={payload} "
            f"stderr={completed.stderr}"
        )
    return completed.returncode, payload


def make_tiny_v1_v2_tie_package(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    v1 = {
        "logical_clock_ms": 1000,
        "records": [
            {
                "effective_after_s": 0,
                "environment": "dev",
                "generation": 1,
                "logical_clock_ms": 1000,
                "params": {"winner": "v1"},
                "replicas": 1,
                "retry_factor": 1.0,
                "service": "tie",
                "timeout_ms": 100,
                "version": "1.0.0",
            }
        ],
        "schema_version": 1,
    }
    shard = {
        "logical_clock_ms": 1000,
        "records": [
            {
                "effective_at_ms": 1000,
                "environment": "dev",
                "generation": 1,
                "params": {"winner": "v2"},
                "service": "tie",
                "tombstone": False,
                "upstreams": [],
                "version": "1.0.0",
            }
        ],
        "schema_version": 2,
        "shard_id": "tie",
    }
    write_json(destination / "v1.json", v1)
    write_json(destination / "shard.json", shard)
    package = {
        "clock": {
            "default_step": "t0",
            "rollback_tolerance_ms": 100,
            "steps": {"t0": 1000},
        },
        "schema_version": 1,
        "v1": {"path": "v1.json"},
        "v2": {"metadata_db": "meta.sqlite", "shards": ["shard.json"]},
    }
    package_path = destination / "package.json"
    write_json(package_path, package)
    rebuild_v2_metadata(package_path)
    return package_path

