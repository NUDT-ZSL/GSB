from __future__ import annotations

import fcntl
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import LedgerError
from .jsonutil import (
    canonical_json,
    hash_json,
)
from .model import Snapshot, snapshot_from_dict


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS base_sample (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    signature TEXT NOT NULL,
    seed INTEGER NOT NULL,
    base_version TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    version TEXT PRIMARY KEY,
    parent_version TEXT,
    clock INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    patch_id TEXT,
    patch_hash TEXT,
    changed_sources_json TEXT NOT NULL,
    created_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    clock INTEGER NOT NULL,
    parent_round_id TEXT,
    reuse_base_round TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMMITTED', 'FAILED')),
    invalidations_json TEXT NOT NULL,
    result_hash TEXT,
    UNIQUE (version, clock)
);

CREATE TABLE IF NOT EXISTS current_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version TEXT NOT NULL,
    round_id TEXT,
    logical_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applied_patches (
    patch_id TEXT PRIMARY KEY,
    patch_hash TEXT NOT NULL,
    version TEXT NOT NULL,
    clock INTEGER NOT NULL,
    patch_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_results (
    round_id TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMMITTED')),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (round_id, service)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    round_id TEXT NOT NULL,
    service TEXT NOT NULL,
    registration_no INTEGER NOT NULL,
    registration_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STAGED', 'COMMITTED')),
    entry_json TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    PRIMARY KEY (round_id, service, registration_no),
    UNIQUE (round_id, service, registration_key)
);

CREATE INDEX IF NOT EXISTS ledger_entries_key_idx
    ON ledger_entries (round_id, service, registration_key);

CREATE TABLE IF NOT EXISTS sensitive_pairs (
    round_id TEXT NOT NULL,
    pair_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STAGED', 'COMMITTED')),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (round_id, pair_key)
);

CREATE TABLE IF NOT EXISTS param_carries (
    service TEXT NOT NULL,
    parameter TEXT NOT NULL,
    carry_num INTEGER NOT NULL,
    carry_den INTEGER NOT NULL,
    last_version TEXT NOT NULL,
    PRIMARY KEY (service, parameter)
);
"""


class FileLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def lock_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".lock")


def connect_rw(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        timeout=60.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 60000")
    return connection


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise LedgerError("E_NO_STATE", "ledger database does not exist")
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=60.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 60000")
    return connection


@contextmanager
def immediate(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def _snapshot_from_row(row: sqlite3.Row | None) -> Snapshot:
    if row is None:
        raise LedgerError("E_NO_STATE", "ledger database has no configuration")
    import json

    payload = json.loads(row["snapshot_json"])
    return snapshot_from_dict(payload, seed=int(row["seed"]) if "seed" in row.keys() else 0)


def load_base_snapshot(connection: sqlite3.Connection) -> Snapshot:
    row = connection.execute(
        "SELECT snapshot_json, seed FROM base_sample WHERE id = 1"
    ).fetchone()
    if row is None:
        raise LedgerError("E_NO_STATE", "base sample is not loaded")
    import json

    return snapshot_from_dict(
        json.loads(row["snapshot_json"]), seed=int(row["seed"])
    )


def base_signature(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT signature FROM base_sample WHERE id = 1"
    ).fetchone()
    return None if row is None else str(row["signature"])


def current_state(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM current_state WHERE id = 1").fetchone()
    if row is None:
        raise LedgerError("E_NO_STATE", "ledger has no current state")
    return {
        "logical_clock": int(row["logical_clock"]),
        "round_id": row["round_id"],
        "version": row["version"],
    }


def get_meta_int(connection: sqlite3.Connection, key: str, default: int = 0) -> int:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else int(row["value"])


def set_meta_int(connection: sqlite3.Connection, key: str, value: int) -> None:
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def ensure_base_loaded(
    connection: sqlite3.Connection, snapshot: Snapshot
) -> tuple[bool, str]:
    with immediate(connection):
        existing = connection.execute(
            "SELECT signature, base_version FROM base_sample WHERE id = 1"
        ).fetchone()
        if existing is not None:
            if existing["signature"] != snapshot.signature:
                raise LedgerError(
                    "E_SAMPLE_CONFLICT",
                    "database was loaded from a different base sample",
                    {
                        "database_version": existing["base_version"],
                        "sample_version": snapshot.version,
                    },
                )
            return False, str(existing["base_version"])

        snapshot_json = canonical_json(snapshot.to_dict())
        connection.execute(
            "INSERT INTO base_sample(id, signature, seed, base_version, snapshot_json) "
            "VALUES(1, ?, ?, ?, ?)",
            (
                snapshot.signature,
                snapshot.seed,
                snapshot.version,
                snapshot_json,
            ),
        )
        connection.execute(
            "INSERT INTO config_versions("
            "version, parent_version, clock, snapshot_json, patch_id, patch_hash, "
            "changed_sources_json, created_clock"
            ") VALUES(?, NULL, 0, ?, NULL, NULL, '[]', 0)",
            (snapshot.version, snapshot_json),
        )
        connection.execute(
            "INSERT INTO current_state(id, version, round_id, logical_clock) "
            "VALUES(1, ?, NULL, 0)",
            (snapshot.version,),
        )
        set_meta_int(connection, "logical_clock", 0)
        return True, snapshot.version


def snapshot_for_version(
    connection: sqlite3.Connection, version: str
) -> Snapshot:
    row = connection.execute(
        "SELECT snapshot_json FROM config_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        raise LedgerError("E_NO_STATE", "configuration version is missing")
    import json

    return snapshot_from_dict(json.loads(row["snapshot_json"]))


def pending_round(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM rounds WHERE status = 'PENDING' "
        "ORDER BY clock DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    import json

    return {
        "clock": int(row["clock"]),
        "invalidations": json.loads(row["invalidations_json"]),
        "parent_round_id": row["parent_round_id"],
        "reuse_base_round": row["reuse_base_round"],
        "round_id": row["round_id"],
        "status": row["status"],
        "version": row["version"],
    }


def round_info(connection: sqlite3.Connection, round_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM rounds WHERE round_id = ?", (round_id,)
    ).fetchone()
    if row is None:
        raise LedgerError("E_NO_STATE", "round does not exist")
    import json

    return {
        "clock": int(row["clock"]),
        "invalidations": json.loads(row["invalidations_json"]),
        "parent_round_id": row["parent_round_id"],
        "reuse_base_round": row["reuse_base_round"],
        "result_hash": row["result_hash"],
        "round_id": row["round_id"],
        "status": row["status"],
        "version": row["version"],
    }


def patch_record(connection: sqlite3.Connection, patch_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM applied_patches WHERE patch_id = ?", (patch_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "clock": int(row["clock"]),
        "patch_hash": row["patch_hash"],
        "patch_id": row["patch_id"],
        "patch_json": row["patch_json"],
        "version": row["version"],
    }


def create_pending_round(
    connection: sqlite3.Connection,
    *,
    snapshot: Snapshot,
    parent_version: str | None,
    parent_round_id: str | None,
    reuse_base_round: str | None,
    invalidations: dict[str, Any],
    patches: list[Any] | None,
    step: int,
) -> dict[str, Any]:
    patches = patches or []
    with immediate(connection):
        if pending_round(connection) is not None:
            raise LedgerError(
                "E_PENDING_ROUND",
                "a diagnostic round is already pending; run resume first",
            )
        clock = get_meta_int(connection, "logical_clock", 0) + step
        round_id = "rnd-" + hash_json(
            {
                "clock": clock,
                "parent_round": parent_round_id,
                "version": snapshot.version,
            }
        )[:32]
        snapshot_json = canonical_json(snapshot.to_dict())
        connection.execute(
            "INSERT INTO config_versions("
            "version, parent_version, clock, snapshot_json, patch_id, patch_hash, "
            "changed_sources_json, created_clock"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(version) DO NOTHING",
            (
                snapshot.version,
                parent_version,
                clock,
                snapshot_json,
                ",".join(patch.patch_id for patch in patches) if patches else None,
                hash_json(
                    [patch.patch_hash for patch in patches]
                )
                if patches
                else None,
                canonical_json(invalidations.get("trigger_sources", [])),
                clock,
            ),
        )
        connection.execute(
            "INSERT INTO rounds("
            "round_id, version, clock, parent_round_id, reuse_base_round, status, "
            "invalidations_json"
            ") VALUES(?, ?, ?, ?, ?, 'PENDING', ?)",
            (
                round_id,
                snapshot.version,
                clock,
                parent_round_id,
                reuse_base_round,
                canonical_json(invalidations),
            ),
        )
        set_meta_int(connection, "logical_clock", clock)
        for patch in patches:
            connection.execute(
                "INSERT INTO applied_patches("
                "patch_id, patch_hash, version, clock, patch_json"
                ") VALUES(?, ?, ?, ?, ?)",
                (
                    patch.patch_id,
                    patch.patch_hash,
                    snapshot.version,
                    clock,
                    canonical_json(
                        {
                            "description": patch.description,
                            "operations": [
                                _patch_operation_json(operation)
                                for operation in patch.operations
                            ],
                            "patch_id": patch.patch_id,
                        }
                    ),
                ),
            )
        return {
            "clock": clock,
            "round_id": round_id,
            "version": snapshot.version,
        }


def _patch_operation_json(operation: dict[str, Any]) -> dict[str, Any]:
    from .jsonutil import canonical_decimal

    result = {
        "layer": operation["layer"],
        "op": operation["op"],
        "parameter": operation["parameter"],
    }
    if "value" in operation:
        result["value"] = canonical_decimal(operation["value"])
    return result


def record_noop_patch(
    connection: sqlite3.Connection,
    *,
    patch: Any,
    version: str,
    clock: int,
) -> None:
    with immediate(connection):
        existing = patch_record(connection, patch.patch_id)
        if existing is not None:
            if existing["patch_hash"] != patch.patch_hash:
                raise LedgerError(
                    "E_PATCH_CONFLICT",
                    "patch_id was already used with different contents",
                    {"patch_id": patch.patch_id},
                )
            return
        connection.execute(
            "INSERT INTO applied_patches("
            "patch_id, patch_hash, version, clock, patch_json"
            ") VALUES(?, ?, ?, ?, ?)",
            (
                patch.patch_id,
                patch.patch_hash,
                version,
                clock,
                canonical_json(
                    {
                        "description": patch.description,
                        "operations": [
                            _patch_operation_json(operation)
                            for operation in patch.operations
                        ],
                        "patch_id": patch.patch_id,
                    }
                ),
            ),
        )


def service_result(
    connection: sqlite3.Connection, round_id: str, service: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM service_results "
        "WHERE round_id = ? AND service = ? AND status = 'COMMITTED'",
        (round_id, service),
    ).fetchone()
    if row is None:
        return None
    import json

    return {
        "decision": json.loads(row["decision_json"]),
        "payload": json.loads(row["payload_json"]),
        "payload_hash": row["payload_hash"],
    }


def committed_services(connection: sqlite3.Connection, round_id: str) -> list[str]:
    rows = connection.execute(
        "SELECT service FROM service_results "
        "WHERE round_id = ? AND status = 'COMMITTED' ORDER BY service",
        (round_id,),
    ).fetchall()
    return [str(row["service"]) for row in rows]


def staged_entry_count(connection: sqlite3.Connection, round_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM ledger_entries "
        "WHERE round_id = ? AND status = 'STAGED'",
        (round_id,),
    ).fetchone()
    return int(row["count"]) if row else 0


def ledger_entries(
    connection: sqlite3.Connection, round_id: str, service: str
) -> list[dict[str, Any]]:
    import json

    rows = connection.execute(
        "SELECT * FROM ledger_entries WHERE round_id = ? AND service = ? "
        "ORDER BY registration_no",
        (round_id, service),
    ).fetchall()
    return [
        {
            "entry": json.loads(row["entry_json"]),
            "entry_hash": row["entry_hash"],
            "registration_key": row["registration_key"],
            "registration_no": int(row["registration_no"]),
            "status": row["status"],
        }
        for row in rows
    ]


def entries_by_key(
    connection: sqlite3.Connection, round_id: str, service: str
) -> dict[str, dict[str, Any]]:
    return {
        item["registration_key"]: item["entry"]
        for item in ledger_entries(connection, round_id, service)
    }


def stage_entries(
    connection: sqlite3.Connection,
    *,
    round_id: str,
    service: str,
    entries: dict[str, dict[str, Any]],
) -> dict[str, int]:
    inserted = 0
    reused = 0
    with immediate(connection):
        for key in sorted(entries, key=lambda item: int(entries[item]["registration_no"])):
            entry = entries[key]
            encoded = canonical_json(entry)
            digest = hash_json(entry)
            existing_by_key = connection.execute(
                "SELECT * FROM ledger_entries "
                "WHERE round_id = ? AND service = ? AND registration_key = ?",
                (round_id, service, key),
            ).fetchone()
            if existing_by_key is not None:
                if (
                    existing_by_key["entry_json"] != encoded
                    or existing_by_key["entry_hash"] != digest
                ):
                    raise LedgerError(
                        "E_LEDGER_CONFLICT",
                        "existing registration differs from recomputation",
                        {"registration_key": key, "service": service},
                    )
                reused += 1
                continue
            existing_by_no = connection.execute(
                "SELECT registration_key FROM ledger_entries "
                "WHERE round_id = ? AND service = ? AND registration_no = ?",
                (round_id, service, int(entry["registration_no"])),
            ).fetchone()
            if existing_by_no is not None:
                raise LedgerError(
                    "E_REGISTRATION_CONFLICT",
                    "registration number is already assigned to another key",
                    {
                        "existing_key": existing_by_no["registration_key"],
                        "registration_no": int(entry["registration_no"]),
                        "service": service,
                    },
                )
            connection.execute(
                "INSERT INTO ledger_entries("
                "round_id, service, registration_no, registration_key, status, "
                "entry_json, entry_hash"
                ") VALUES(?, ?, ?, ?, 'STAGED', ?, ?)",
                (
                    round_id,
                    service,
                    int(entry["registration_no"]),
                    key,
                    encoded,
                    digest,
                ),
            )
            inserted += 1
    return {"inserted": inserted, "reused": reused}


def commit_service(
    connection: sqlite3.Connection,
    *,
    round_id: str,
    version: str,
    service: str,
    payload: dict[str, Any],
    decision: dict[str, Any],
    new_carries: dict[str, Any],
) -> dict[str, int]:
    payload_json = canonical_json(payload)
    payload_hash = hash_json(payload)
    decision_json = canonical_json(decision)
    committed_entries = 0
    with immediate(connection):
        existing = connection.execute(
            "SELECT * FROM service_results WHERE round_id = ? AND service = ?",
            (round_id, service),
        ).fetchone()
        if existing is not None:
            if (
                existing["payload_json"] != payload_json
                or existing["payload_hash"] != payload_hash
                or existing["decision_json"] != decision_json
            ):
                raise LedgerError(
                    "E_RESULT_CONFLICT",
                    "existing service result differs from recomputation",
                    {"service": service},
                )
            if existing["status"] == "COMMITTED":
                return {"committed_entries": 0, "result": "REUSED"}

        connection.execute(
            "INSERT INTO service_results("
            "round_id, service, status, payload_json, payload_hash, decision_json"
            ") VALUES(?, ?, 'COMMITTED', ?, ?, ?) "
            "ON CONFLICT(round_id, service) DO UPDATE SET "
            "status = 'COMMITTED', payload_json = excluded.payload_json, "
            "payload_hash = excluded.payload_hash, decision_json = excluded.decision_json",
            (round_id, service, payload_json, payload_hash, decision_json),
        )
        cursor = connection.execute(
            "UPDATE ledger_entries SET status = 'COMMITTED' "
            "WHERE round_id = ? AND service = ?",
            (round_id, service),
        )
        committed_entries = int(cursor.rowcount)
        for parameter, carry in sorted(new_carries.items()):
            connection.execute(
                "INSERT INTO param_carries("
                "service, parameter, carry_num, carry_den, last_version"
                ") VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(service, parameter) DO UPDATE SET "
                "carry_num = excluded.carry_num, carry_den = excluded.carry_den, "
                "last_version = excluded.last_version",
                (
                    service,
                    parameter,
                    carry.numerator,
                    carry.denominator,
                    version,
                ),
            )
        return {
            "committed_entries": committed_entries,
            "result": "COMMITTED",
        }


def carries_for_service(
    connection: sqlite3.Connection, service: str
) -> dict[str, Any]:
    from fractions import Fraction

    rows = connection.execute(
        "SELECT parameter, carry_num, carry_den FROM param_carries "
        "WHERE service = ? ORDER BY parameter",
        (service,),
    ).fetchall()
    return {
        str(row["parameter"]): Fraction(
            int(row["carry_num"]), int(row["carry_den"])
        )
        for row in rows
    }


def store_sensitive_pairs(
    connection: sqlite3.Connection,
    *,
    round_id: str,
    pairs: list[dict[str, Any]],
) -> int:
    inserted = 0
    with immediate(connection):
        for pair in pairs:
            pair_json = canonical_json(pair)
            pair_hash = hash_json(pair)
            existing = connection.execute(
                "SELECT * FROM sensitive_pairs "
                "WHERE round_id = ? AND pair_key = ?",
                (round_id, pair["pair_key"]),
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_json"] != pair_json
                    or existing["payload_hash"] != pair_hash
                ):
                    raise LedgerError(
                        "E_SENSITIVE_CONFLICT",
                        "existing sensitive comparison differs from recomputation",
                        {"pair_key": pair["pair_key"]},
                    )
                continue
            connection.execute(
                "INSERT INTO sensitive_pairs("
                "round_id, pair_key, status, payload_json, payload_hash"
                ") VALUES(?, ?, 'COMMITTED', ?, ?)",
                (round_id, pair["pair_key"], pair_json, pair_hash),
            )
            inserted += 1
    return inserted


def sensitive_pairs(
    connection: sqlite3.Connection, round_id: str
) -> list[dict[str, Any]]:
    import json

    rows = connection.execute(
        "SELECT payload_json FROM sensitive_pairs "
        "WHERE round_id = ? AND status = 'COMMITTED' ORDER BY pair_key",
        (round_id,),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def finalize_round(
    connection: sqlite3.Connection,
    *,
    round_id: str,
    result_hash: str,
    invalidations: dict[str, Any] | None = None,
) -> None:
    with immediate(connection):
        row = connection.execute(
            "SELECT * FROM rounds WHERE round_id = ?", (round_id,)
        ).fetchone()
        if row is None:
            raise LedgerError("E_NO_STATE", "round does not exist")
        if row["status"] == "COMMITTED":
            if row["result_hash"] != result_hash:
                raise LedgerError(
                    "E_RESULT_CONFLICT",
                    "round was committed with a different result hash",
                )
            return
        if invalidations is not None:
            connection.execute(
                "UPDATE rounds SET invalidations_json = ? WHERE round_id = ?",
                (canonical_json(invalidations), round_id),
            )
        connection.execute(
            "UPDATE rounds SET status = 'COMMITTED', result_hash = ? "
            "WHERE round_id = ?",
            (result_hash, round_id),
        )
        connection.execute(
            "UPDATE current_state SET version = ?, round_id = ?, logical_clock = ? "
            "WHERE id = 1",
            (row["version"], round_id, int(row["clock"])),
        )


def round_result_hash(
    *,
    service_hashes: dict[str, str],
    pair_hashes: dict[str, str],
    invalidations: dict[str, Any],
) -> str:
    return "h-" + hash_json(
        {
            "invalidations": invalidations,
            "pairs": dict(sorted(pair_hashes.items())),
            "services": dict(sorted(service_hashes.items())),
        }
    )
