"""Deterministic sample generator used by tests and local acceptance runs."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path

from .util import atomic_write_json


DEFAULT_SEED = 20261125
OBSERVATION_CLOCK_MS = 1_700_000_000_000


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _v1_rule(
    key: str,
    effective_after_s: int,
    window_s: int,
    matches: list[str],
    enabled: bool = True,
) -> dict:
    return {
        "rule_key": key,
        "effective_after_s": effective_after_s,
        "dedup_window_s": window_s,
        "match_strings": matches,
        "enabled": enabled,
    }


def _v2_rule(
    key: str,
    effective_ms: int,
    generation: int,
    window_ms: int,
    matches: list[str],
    *,
    enabled: bool = True,
    tombstone: bool = False,
    parents: list[str] | None = None,
) -> dict:
    return {
        "rule_key": key,
        "effective_ms": effective_ms,
        "generation": generation,
        "dedup_window_ms": window_ms,
        "match_strings": matches,
        "enabled": enabled,
        "tombstone": tombstone,
        "parent_rule_keys": parents or [],
    }


def _alert(
    alert_id: str,
    host_id: str,
    offset_ms: int,
    rule_key: str,
    digest_label: str,
    process_guid: str,
    parent_guid: str | None,
    severity: int,
) -> dict:
    return {
        "alert_id": alert_id,
        "host_id": host_id,
        "alert_ts_ms": OBSERVATION_CLOCK_MS + offset_ms,
        "rule_key": rule_key,
        "severity": severity,
        "process_guid": process_guid,
        "parent_process_guid": parent_guid,
        "payload_digest": _digest(digest_label),
    }


def _patch_rule(
    key: str,
    effective_ms: int,
    generation: int,
    window_ms: int,
    matches: list[str],
    *,
    enabled: bool = True,
    tombstone: bool = False,
    parents: list[str] | None = None,
) -> dict:
    return {
        "rule_key": key,
        "effective_ms": effective_ms,
        "generation": generation,
        "dedup_window_ms": window_ms,
        "match_strings": matches,
        "enabled": enabled,
        "tombstone": tombstone,
        "parent_rule_keys": parents or [],
    }


def generate_sample(output_dir: Path, *, seed: int = DEFAULT_SEED) -> dict:
    output_dir = Path(output_dir)
    shard_dir = output_dir / "v2_shards"
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_dir.mkdir(parents=True, exist_ok=True)
    for path in output_dir.glob("alerts.jsonl"):
        path.unlink()
    for path in output_dir.glob("patches.jsonl"):
        path.unlink()
    for path in shard_dir.glob("*.json"):
        path.unlink()
    meta_path = output_dir / "v2_meta.sqlite"
    meta_path.unlink(missing_ok=True)

    v1_document = {
        "schema_version": 1,
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "severity_range": {"min": 1, "max": 5},
        "rules": [
            _v1_rule(
                "R-POWERSHELL",
                0,
                60,
                ["powershell", "-enc"],
            ),
            _v1_rule("R-CRED", 5, 30, ["lsass", "dump"]),
            _v1_rule("R-EDGE", 20, 2, ["edge"]),
            _v1_rule("R-OVERLAP", 40, 5, ["overlap"]),
            _v1_rule("R-TOMB", 0, 60, ["tomb"]),
            _v1_rule("R-PARENT", 0, 120, ["parent"]),
        ],
    }
    atomic_write_json(
        output_dir / "rules_v1.json",
        v1_document,
        acquire_lock=False,
    )

    v2_rules = [
        _v2_rule(
            "R-POWERSHELL",
            OBSERVATION_CLOCK_MS,
            2,
            60_000,
            ["powershell", "-enc"],
        ),
        _v2_rule(
            "R-CRED",
            OBSERVATION_CLOCK_MS + 5_000,
            2,
            45_000,
            ["lsass", "dump"],
            enabled=False,
        ),
        _v2_rule(
            "R-OVERLAP",
            OBSERVATION_CLOCK_MS + 40_000,
            2,
            5_000,
            ["overlap"],
        ),
        _v2_rule(
            "R-PATCH",
            OBSERVATION_CLOCK_MS + 25_000,
            2,
            5_000,
            ["patch"],
        ),
        _v2_rule(
            "R-PARENT",
            OBSERVATION_CLOCK_MS,
            2,
            120_000,
            ["parent"],
        ),
        _v2_rule(
            "R-CHILD",
            OBSERVATION_CLOCK_MS,
            2,
            60_000,
            ["child"],
            parents=["R-PARENT"],
        ),
        _v2_rule(
            "R-TOMB",
            OBSERVATION_CLOCK_MS,
            2,
            60_000,
            ["tomb"],
        ),
    ]
    shards = [v2_rules[:3], v2_rules[3:]]
    shard_records: list[tuple[str, int, dict]] = []
    global_index = 0
    for shard_number, rules in enumerate(shards):
        shard_id = f"v2-{shard_number:03d}"
        for local_index, record in enumerate(rules, start=1):
            global_index += 1
            shard_records.append((shard_id, global_index, record))
        shard_document = {
            "schema_version": 2,
            "shard_id": shard_id,
            "rules": rules,
        }
        atomic_write_json(
            shard_dir / f"{shard_id}.json",
            shard_document,
            acquire_lock=False,
        )

    connection = sqlite3.connect(meta_path)
    try:
        connection.execute(
            """
            CREATE TABLE v2_rule_meta (
                rule_key TEXT NOT NULL,
                effective_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                dedup_window_ms INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                parent_rule_keys_json TEXT NOT NULL,
                shard_id TEXT NOT NULL,
                record_index INTEGER NOT NULL,
                PRIMARY KEY (rule_key, effective_ms, generation)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO v2_rule_meta(
                rule_key, effective_ms, generation, dedup_window_ms,
                tombstone, parent_rule_keys_json, shard_id, record_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record["rule_key"],
                    record["effective_ms"],
                    record["generation"],
                    record["dedup_window_ms"],
                    1 if record["tombstone"] else 0,
                    json.dumps(
                        record["parent_rule_keys"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    shard_id,
                    record_index,
                )
                for shard_id, record_index, record in shard_records
            ],
        )
        connection.commit()
    finally:
        connection.close()

    valid_alerts = [
        _alert(
            "A-PS-01", "H-PS", 1_000, "R-POWERSHELL", "ps-main",
            "ps-1", "ps-0", 4,
        ),
        _alert(
            "A-PS-02", "H-PS", 5_000, "R-POWERSHELL", "ps-main",
            "ps-2", "ps-1", 4,
        ),
        _alert(
            "A-PS-03", "H-PS", 20_000, "R-POWERSHELL", "ps-main",
            "other-1", "other-0", 3,
        ),
        _alert(
            "A-PS-04", "H-PS", 62_000, "R-POWERSHELL", "ps-main",
            "ps-1", "ps-0", 4,
        ),
        _alert(
            "A-PS-05", "H-PS", 2_000, "R-POWERSHELL", "ps-other",
            "ps-1", "ps-0", 5,
        ),
        _alert(
            "A-EDGE-01", "H-EDGE", 20_000, "R-EDGE", "edge-main",
            "edge-1", "edge-0", 2,
        ),
        _alert(
            "A-EDGE-02", "H-EDGE", 22_500, "R-EDGE", "edge-main",
            "edge-2", "edge-1", 2,
        ),
        _alert(
            "A-EDGE-03", "H-EDGE", 22_600, "R-EDGE", "edge-main",
            "edge-3", "edge-2", 2,
        ),
        _alert(
            "A-OVER-01", "H-OVER", 40_500, "R-OVERLAP", "overlap-main",
            "over-1", "over-0", 3,
        ),
        _alert(
            "A-OVER-02", "H-OVER", 42_000, "R-OVERLAP", "overlap-main",
            "over-2", "over-1", 3,
        ),
        _alert(
            "A-OVER-03", "H-OVER", 44_000, "R-OVERLAP", "overlap-main",
            "over-3", "over-2", 3,
        ),
        _alert(
            "A-OVER-04", "H-OVER", 50_000, "R-OVERLAP", "overlap-main",
            "over-4", "over-3", 3,
        ),
        _alert(
            "A-TOMB-01", "H-TOMB", 1_000, "R-TOMB", "tomb-main",
            "tomb-1", "tomb-0", 2,
        ),
        _alert(
            "A-TOMB-02", "H-TOMB", 20_000, "R-TOMB", "tomb-main",
            "tomb-2", "tomb-1", 2,
        ),
        _alert(
            "A-TOMB-03", "H-TOMB", 31_000, "R-TOMB", "tomb-main",
            "tomb-3", "tomb-2", 2,
        ),
        _alert(
            "A-PATCH-01", "H-PATCH", 26_000, "R-PATCH", "patch-main",
            "patch-1", "patch-0", 4,
        ),
        _alert(
            "A-PATCH-02", "H-PATCH", 30_000, "R-PATCH", "patch-main",
            "patch-2", "patch-1", 4,
        ),
        _alert(
            "A-PATCH-03", "H-PATCH", 30_500, "R-PATCH", "patch-main",
            "patch-3", "patch-2", 4,
        ),
        _alert(
            "A-CHILD-01", "H-LINEAGE", 5_000, "R-CHILD", "child-main",
            "child-1", "parent-1", 3,
        ),
        _alert(
            "A-CHILD-02", "H-LINEAGE", 10_000, "R-CHILD", "child-main",
            "child-2", "child-1", 3,
        ),
        _alert(
            "A-PARENT-01", "H-LINEAGE", 1_000, "R-PARENT", "parent-main",
            "parent-1", "parent-0", 3,
        ),
        _alert(
            "A-PARENT-02", "H-LINEAGE", 50_000, "R-PARENT", "parent-main",
            "parent-2", "parent-1", 3,
        ),
        _alert(
            "A-CYC-01", "H-CYCLE", 1_000, "R-POWERSHELL", "cycle-main",
            "cyc-a", "cyc-b", 5,
        ),
        _alert(
            "A-CYC-02", "H-CYCLE", 2_000, "R-POWERSHELL", "cycle-main",
            "cyc-b", "cyc-a", 5,
        ),
    ]
    random.Random(seed).shuffle(valid_alerts)

    invalid_records = [
        "{this is not json",
        {
            "alert_id": "A-BAD-SEVERITY",
            "host_id": "H-BAD",
            "alert_ts_ms": OBSERVATION_CLOCK_MS + 1,
            "rule_key": "R-POWERSHELL",
            "severity": 9,
            "process_guid": "bad-1",
            "parent_process_guid": None,
            "payload_digest": _digest("bad-severity"),
        },
        {
            "alert_id": "A-BAD-DIGEST",
            "host_id": "H-BAD",
            "alert_ts_ms": OBSERVATION_CLOCK_MS + 1,
            "rule_key": "R-POWERSHELL",
            "severity": 3,
            "process_guid": "bad-2",
            "parent_process_guid": None,
            "payload_digest": "not-a-digest",
        },
        {
            "alert_id": "A-UNKNOWN-RULE",
            "host_id": "H-BAD",
            "alert_ts_ms": OBSERVATION_CLOCK_MS + 1,
            "rule_key": "R-NOT-DECLARED",
            "severity": 3,
            "process_guid": "bad-3",
            "parent_process_guid": None,
            "payload_digest": _digest("unknown-rule"),
        },
        {
            "alert_id": "A-BAD-TS",
            "host_id": "H-BAD",
            "alert_ts_ms": -1,
            "rule_key": "R-POWERSHELL",
            "severity": 3,
            "process_guid": "bad-4",
            "parent_process_guid": None,
            "payload_digest": _digest("bad-ts"),
        },
        {
            "alert_id": "A-PS-01",
            "host_id": "H-DUP",
            "alert_ts_ms": OBSERVATION_CLOCK_MS + 9_000,
            "rule_key": "R-POWERSHELL",
            "severity": 3,
            "process_guid": "dup-1",
            "parent_process_guid": None,
            "payload_digest": _digest("duplicate"),
        },
    ]
    records: list[object] = list(valid_alerts)
    # Invalid records are appended so the duplicate-ID case deterministically
    # exercises "first valid wins" rather than stealing a legitimate alert ID.
    records.extend(invalid_records)
    with (output_dir / "alerts.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            if isinstance(record, str):
                handle.write(record + "\n")
            else:
                handle.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )

    patch_documents = [
        {
            "batch_id": "P200",
            "logical_ts_ms": 200,
            "rules": [
                _patch_rule(
                    "R-PATCH",
                    OBSERVATION_CLOCK_MS + 25_000,
                    3,
                    10_000,
                    ["patch"],
                )
            ],
        },
        {
            "batch_id": "P100",
            "logical_ts_ms": 100,
            "rules": [
                _patch_rule(
                    "R-PATCH",
                    OBSERVATION_CLOCK_MS + 25_000,
                    4,
                    1_000,
                    ["patch"],
                )
            ],
        },
        {
            "batch_id": "P300",
            "logical_ts_ms": 300,
            "rules": [
                _patch_rule(
                    "R-TOMB",
                    OBSERVATION_CLOCK_MS + 30_000,
                    3,
                    0,
                    [],
                    enabled=False,
                    tombstone=True,
                )
            ],
        },
        {
            "batch_id": "P150",
            "logical_ts_ms": 150,
            "rules": [
                _patch_rule(
                    "R-PARENT",
                    OBSERVATION_CLOCK_MS,
                    3,
                    8_000,
                    ["parent"],
                )
            ],
        },
    ]
    with (output_dir / "patches.jsonl").open("w", encoding="utf-8") as handle:
        for document in patch_documents:
            handle.write(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )

    file_names = [
        "alerts.jsonl",
        "rules_v1.json",
        "patches.jsonl",
        "v2_meta.sqlite",
        "v2_shards/v2-000.json",
        "v2_shards/v2-001.json",
    ]
    sample_hashes = {
        name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        for name in file_names
    }
    truth = {
        "seed": seed,
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "expected": {
            "report_status": "OK",
            "recompute_full_match": True,
            "business_codes_present": [
                "E_ALERT_ID_DUP",
                "E_ALERT_TS",
                "E_DIGEST",
                "E_PROC_CYCLE",
                "E_RULE_CONFLICT",
                "E_RULE_UNKNOWN",
                "E_SEVERITY",
                "E_VERSION_AMBIGUOUS",
                "E_WINDOW_AMBIGUOUS",
            ],
            "patch_arrival_order": [
                item["batch_id"] for item in patch_documents
            ],
            "patch_logical_order": [
                item["batch_id"]
                for item in sorted(
                    patch_documents,
                    key=lambda value: (
                        value["logical_ts_ms"],
                        value["batch_id"],
                    ),
                )
            ],
            "out_of_order_arrival": True,
        },
        "sample_sha256": sample_hashes,
    }
    atomic_write_json(
        output_dir / "truth_sync.json",
        truth,
        acquire_lock=False,
    )
    (output_dir / ".engine.lock").unlink(missing_ok=True)
    (shard_dir / ".engine.lock").unlink(missing_ok=True)
    return truth
