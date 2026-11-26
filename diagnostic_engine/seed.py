from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps, deterministic_payload, sha256_hex, signature, write_stable_json
from .clock import DEFAULT_CLOCK_PROFILE
from .errors import FAULT_ERROR_CODES
from .validation import shard_hash_payload


FIXED_SEED = 20_261_127


def _event_clock(seq: int) -> int:
    return (seq - 1) * 5 + (seq - 1) % 3


def _build_events(seed: int) -> list[dict[str, Any]]:
    keys = [
        "alpha/report.bin",
        "beta/image.dat",
        "gamma/table.sqlite",
        "delta/log.ndjson",
        "epsilon/config.json",
        "zeta/archive.part1",
    ]
    specs: list[tuple[int, str, int]] = [
        (0, "WRITE", 128),
        (1, "WRITE", 256),
        (0, "UPDATE", 160),
        (2, "WRITE", 512),
        (1, "UPDATE", 320),
        (2, "DELETE", 0),
        (3, "WRITE", 96),
        (0, "UPDATE", 220),
        (4, "WRITE", 144),
        (3, "UPDATE", 128),
        (5, "WRITE", 400),
        (4, "DELETE", 0),
        (5, "UPDATE", 360),
        (2, "WRITE", 192),
        (4, "WRITE", 176),
        (1, "UPDATE", 288),
    ]
    versions = [0 for _ in keys]
    live_signatures: dict[int, str] = {}
    events: list[dict[str, Any]] = []
    for seq, (key_index, op, size) in enumerate(specs, start=1):
        key = keys[key_index]
        target_version: int | None = None
        if op == "DELETE":
            target_version = versions[key_index]
            if target_version <= 0:
                raise RuntimeError("fixed seed DELETE before WRITE")
            versions[key_index] += 1
            version = versions[key_index]
            expected_signature = live_signatures[key_index]
            payload_signature = signature(b"")
        else:
            versions[key_index] += 1
            version = versions[key_index]
            payload = deterministic_payload(seed, seq, key, version, size)
            expected_signature = signature(payload)
            payload_signature = expected_signature
            live_signatures[key_index] = expected_signature
        events.append(
            {
                "seq": seq,
                "logical_clock": _event_clock(seq),
                "op": op,
                "key": key,
                "content_version": version,
                "target_content_version": target_version,
                "payload_size": size,
                "payload_algorithm": "sha256-counter-v1",
                "expected_signature": expected_signature,
                "payload_signature": payload_signature,
            }
        )
    return events


def _build_manifest(seed: int, events: list[dict[str, Any]]) -> dict[str, Any]:
    live_by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["op"] in {"WRITE", "UPDATE"}:
            live_by_key[event["key"]] = event
    objects: list[dict[str, Any]] = []
    for key in sorted(live_by_key):
        event = live_by_key[key]
        objects.append(
            {
                "key": key,
                "size_bytes": event["payload_size"],
                "content_version": event["content_version"],
                "expected_signature": event["expected_signature"],
                "source_event_seq": event["seq"],
            }
        )
    return {
        "schema": "object-manifest-v1",
        "seed": seed,
        "signature_algorithm": "sha256",
        "objects": objects,
    }


def _trace(record: dict[str, Any], generation: int) -> str:
    return "|".join(
        [
            f"version={record['content_version']}",
            f"generation={generation}",
            f"clock={record['logical_clock']}",
            f"signature={record['expected_signature']}",
        ]
    )


def _build_snapshots(events: list[dict[str, Any]], clock_profile: dict[str, Any]) -> tuple[dict, dict, list[dict], list[dict]]:
    by_seq = {event["seq"]: event for event in events}
    anchor = int(clock_profile["anchor_ms"])
    tick = int(clock_profile["tick_ms"])

    def absolute_ms(clock: int) -> int:
        return anchor + clock * tick

    v1_sources = {
        "alpha/report.bin": (by_seq[3], 2),
        "beta/image.dat": (by_seq[5], 4),
        "gamma/table.sqlite": (by_seq[4], 3),
        "delta/log.ndjson": (by_seq[7], 8),
        "epsilon/config.json": (by_seq[9], 7),
    }
    flat_records: dict[str, str] = {}
    v1_normalized: list[dict[str, Any]] = []
    for key, (event, generation) in v1_sources.items():
        flat_records[key] = _trace(event, generation)
        v1_normalized.append(
            {
                "key": key,
                "content_version": event["content_version"],
                "generation": generation,
                "logical_clock": event["logical_clock"],
                "absolute_clock_ms": absolute_ms(event["logical_clock"]),
                "signature": event["expected_signature"],
            }
        )
    v1 = {
        "format": "metadata-snapshot-v1",
        "snapshot_id": "v1-fixed-seed",
        "seed": FIXED_SEED,
        "declared_clock": 18,
        "cache_generation": 8,
        "effective_relative_seconds": "5.618253",
        "time_format": "relative_seconds_since_declared_clock",
        "flat_records": dict(sorted(flat_records.items())),
    }

    v2_specs = [
        ("alpha/report.bin", by_seq[8], 4, False, 0),
        ("beta/image.dat", by_seq[5], 5, False, 0),
        ("gamma/table.sqlite", by_seq[6], 6, True, 1),
        ("delta/log.ndjson", by_seq[10], 8, False, 1),
        ("zeta/archive.part1", by_seq[13], 9, False, 1),
    ]
    shard_records: list[list[dict[str, Any]]] = [[], []]
    v2_rows: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    history_id = 1
    for key, event, generation, tombstone, shard_index in v2_specs:
        record = {
            "key": key,
            "content_version": event["content_version"],
            "signature": signature(b"") if tombstone and event["op"] == "DELETE" else event["expected_signature"],
            "logical_clock": event["logical_clock"],
            "absolute_clock_ms": absolute_ms(event["logical_clock"]),
            "generation": generation,
            "tombstone": tombstone,
            "shard_id": f"shard-{shard_index:03d}",
        }
        if tombstone:
            record["signature"] = event["expected_signature"]
        shard_records[shard_index].append(record)
        previous_generation = max(1, generation - 2)
        for hist_generation, version in (
            (previous_generation, max(1, event["content_version"] - 1)),
            (generation, event["content_version"]),
        ):
            history_rows.append(
                {
                    "history_id": history_id,
                    "key": key,
                    "content_version": version,
                    "generation": hist_generation,
                    "absolute_clock_ms": absolute_ms(event["logical_clock"] - (1 if hist_generation < generation else 0)),
                    "tombstone": 1 if hist_generation == generation and tombstone else 0,
                }
            )
            history_id += 1
        v2_rows.append(
            {
                "key": key,
                "content_version": record["content_version"],
                "signature": record["signature"],
                "absolute_clock_ms": record["absolute_clock_ms"],
                "generation": generation,
                "tombstone": 1 if tombstone else 0,
            }
        )

    shards: list[dict[str, Any]] = []
    for index, records in enumerate(shard_records):
        shard: dict[str, Any] = {
            "format": "metadata-snapshot-v2-shard",
            "shard_id": f"shard-{index:03d}",
            "index": index,
            "generation": max(record["generation"] for record in records),
            "declared_clock": 30 + index * 10,
            "records": sorted(records, key=lambda item: item["key"]),
        }
        shard["body_sha256"] = sha256_hex(canonical_dumps(shard_hash_payload(shard)).encode("utf-8"))
        shards.append(shard)

    v2_manifest = {
        "format": "metadata-snapshot-v2-manifest",
        "seed": FIXED_SEED,
        "database": "metadata.sqlite",
        "hash_algorithm": "sha256",
        "shards": [
            {"index": shard["index"], "shard_id": shard["shard_id"], "path": f"shards/shard-{shard['index']:03d}.json", "body_sha256": shard["body_sha256"]}
            for shard in shards
        ],
    }
    return v1, v2_manifest, shards, {"current": v2_rows, "history": history_rows}


def _injection_case(
    injection_id: str,
    event_seq: int,
    policy: str,
    stage: str,
    fault_type: str,
    description: str,
    *,
    hard: bool = False,
) -> dict[str, Any]:
    return {
        "injection_id": injection_id,
        "event_seq": event_seq,
        "policy": policy,
        "stage": stage,
        "fault_type": fault_type,
        "error_code": FAULT_ERROR_CODES[fault_type],
        "description": description,
        "hard": hard,
    }


def _build_injections() -> dict[str, Any]:
    cases = [
        _injection_case("INJ-MF-DATA-INTERRUPT", 1, "metadata_first", "data_staging", "write_interrupt", "partial temp payload during data staging"),
        _injection_case("INJ-MF-RENAME-KILL", 3, "metadata_first", "data_rename", "force_kill", "hard stop after atomic data rename but before metadata commit", hard=True),
        _injection_case("INJ-MF-META-KILL", 5, "metadata_first", "metadata_commit", "force_kill", "hard stop at metadata commit boundary", hard=True),
        _injection_case("INJ-MF-META-ROLLBACK", 8, "metadata_first", "metadata_commit", "generation_rollback", "reject stale cache generation in direct metadata commit"),
        _injection_case("INJ-REV-META-KILL", 2, "direct_reversed", "metadata_commit", "force_kill", "metadata visible before data; hard stop creates dangling pointer", hard=True),
        _injection_case("INJ-REV-META-INTERRUPT", 7, "direct_reversed", "metadata_commit", "write_interrupt", "interrupt early metadata visibility write"),
        _injection_case("INJ-LOG-APPEND-HALF", 4, "write_log", "log_append", "write_interrupt", "partial JSONL checksum line creates half-written log"),
        _injection_case("INJ-LOG-APPEND-KILL", 8, "write_log", "log_append", "force_kill", "durable complete log entry followed by hard kill before data", hard=True),
        _injection_case("INJ-LOG-DATA-KILL", 9, "write_log", "data_write", "force_kill", "log committed; hard kill during actual data write", hard=True),
        _injection_case("INJ-LOG-REPLAY-KILL", 10, "write_log", "log_replay", "force_kill", "hard kill during idempotent replay", hard=True),
        _injection_case("INJ-LOG-REPLAY-INTERRUPT", 11, "write_log", "log_replay", "write_interrupt", "interrupt replay watermark progression"),
        _injection_case("INJ-LOG-REPLAY-ROLLBACK", 12, "write_log", "log_replay", "generation_rollback", "reject cache generation rollback during replay"),
        _injection_case("INJ-LOG-REPLAY-LOST", 13, "write_log", "log_replay", "log_lost", "simulate disappearance of the append-only log"),
    ]
    return {"schema": "fault-injections-v1", "seed": FIXED_SEED, "cases": cases}


def _write_v2_database(path: Path, rows: dict[str, list[dict[str, Any]]]) -> None:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                content_version INTEGER NOT NULL,
                signature TEXT NOT NULL,
                absolute_clock_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                tombstone INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE metadata_history (
                history_id INTEGER PRIMARY KEY,
                key TEXT NOT NULL,
                content_version INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                absolute_clock_ms INTEGER NOT NULL,
                tombstone INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (:key,:content_version,:signature,:absolute_clock_ms,:generation,:tombstone)",
            rows["current"],
        )
        connection.executemany(
            "INSERT INTO metadata_history VALUES (:history_id,:key,:content_version,:generation,:absolute_clock_ms,:tombstone)",
            rows["history"],
        )
        connection.commit()
    finally:
        connection.close()


def generate_fixtures(output_dir: str | Path, seed: int = FIXED_SEED, *, clean: bool = True) -> Path:
    root = Path(output_dir)
    if clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    clock_profile = dict(DEFAULT_CLOCK_PROFILE)
    clock_profile["seed"] = seed
    events = _build_events(seed)
    manifest = _build_manifest(seed, events)
    v1, v2_manifest, shards, v2_db_rows = _build_snapshots(events, clock_profile)
    injections = _build_injections()

    write_stable_json(root / "clock_profile.json", clock_profile)
    write_stable_json(root / "manifest" / "object_manifest.json", manifest)
    write_stable_json(root / "events" / "write_events.json", {"schema": "write-events-v1", "seed": seed, "events": events})
    write_stable_json(root / "snapshots" / "v1" / "metadata-v1.json", v1)
    write_stable_json(root / "snapshots" / "v2" / "v2_manifest.json", v2_manifest)
    for shard in shards:
        write_stable_json(root / "snapshots" / "v2" / "shards" / f"shard-{shard['index']:03d}.json", shard)
    _write_v2_database(root / "snapshots" / "v2" / "metadata.sqlite", v2_db_rows)
    write_stable_json(root / "injections" / "fault_injections.json", injections)
    return root
