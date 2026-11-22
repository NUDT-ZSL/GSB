from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, sha256_json
from .errors import WarbiterError
from .model import ArbiterConfig, Schema


DDL = """
CREATE TABLE IF NOT EXISTS engine_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scripts (
    script_hash TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    tx_count INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    created_generation INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_versions (
    version INTEGER PRIMARY KEY,
    policy_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    created_generation INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tx (
    tx_id INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    start_clock_ms INTEGER NOT NULL,
    commit_clock_ms INTEGER NOT NULL,
    isolation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    script_hash TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    status_detail TEXT NOT NULL DEFAULT '',
    adjudication_policy_version INTEGER,
    created_generation INTEGER NOT NULL,
    FOREIGN KEY (script_hash) REFERENCES scripts(script_hash),
    FOREIGN KEY (policy_version) REFERENCES policy_versions(version)
);

CREATE TABLE IF NOT EXISTS commit_log (
    seq INTEGER PRIMARY KEY,
    tx_id INTEGER NOT NULL UNIQUE,
    commit_clock_ms INTEGER NOT NULL,
    order_key TEXT NOT NULL,
    script_hash TEXT NOT NULL,
    FOREIGN KEY (tx_id) REFERENCES tx(tx_id),
    FOREIGN KEY (script_hash) REFERENCES scripts(script_hash)
);

CREATE TABLE IF NOT EXISTS tx_writes (
    tx_id INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key_json TEXT NOT NULL,
    final_kind TEXT NOT NULL,
    whole_row INTEGER NOT NULL,
    coverage_json TEXT NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY (tx_id, tenant, table_name, row_key_json),
    FOREIGN KEY (tx_id) REFERENCES tx(tx_id)
);

CREATE TABLE IF NOT EXISTS tx_audit (
    tx_id INTEGER NOT NULL,
    op_index INTEGER NOT NULL,
    op_type TEXT NOT NULL,
    tenant TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key_json TEXT NOT NULL,
    column_name TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    assertion_text TEXT,
    recorded_observation_json TEXT,
    PRIMARY KEY (tx_id, op_index, column_name),
    FOREIGN KEY (tx_id) REFERENCES tx(tx_id)
);

CREATE TABLE IF NOT EXISTS tx_assertions (
    tx_id INTEGER NOT NULL,
    op_index INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key_json TEXT NOT NULL,
    assertion_text TEXT NOT NULL,
    recorded_observation_json TEXT,
    PRIMARY KEY (tx_id, op_index),
    FOREIGN KEY (tx_id) REFERENCES tx(tx_id)
);

CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id INTEGER PRIMARY KEY,
    generation INTEGER NOT NULL,
    left_tx_id INTEGER NOT NULL,
    right_tx_id INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key_json TEXT NOT NULL,
    kind TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    replay_count INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    evidence_hash TEXT NOT NULL DEFAULT '',
    UNIQUE (left_tx_id, right_tx_id, tenant, table_name, row_key_json),
    FOREIGN KEY (left_tx_id) REFERENCES tx(tx_id),
    FOREIGN KEY (right_tx_id) REFERENCES tx(tx_id),
    FOREIGN KEY (policy_version) REFERENCES policy_versions(version)
);

CREATE TABLE IF NOT EXISTS assertion_evidence (
    tx_id INTEGER NOT NULL,
    op_index INTEGER NOT NULL,
    assertion_text TEXT NOT NULL,
    result INTEGER NOT NULL,
    action TEXT NOT NULL,
    record_version INTEGER NOT NULL,
    record_values_json TEXT NOT NULL,
    recorded_observation_json TEXT,
    evaluated_at_clock INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    PRIMARY KEY (tx_id, op_index),
    FOREIGN KEY (tx_id) REFERENCES tx(tx_id)
);

CREATE TABLE IF NOT EXISTS suspensions (
    tenant TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key_json TEXT NOT NULL,
    conflict_id INTEGER,
    tx_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    assertion_text TEXT,
    created_generation INTEGER NOT NULL,
    PRIMARY KEY (tenant, table_name, row_key_json),
    FOREIGN KEY (conflict_id) REFERENCES conflicts(conflict_id),
    FOREIGN KEY (tx_id) REFERENCES tx(tx_id)
);

CREATE TABLE IF NOT EXISTS row_state (
    tenant TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key_json TEXT NOT NULL,
    values_json TEXT NOT NULL,
    deleted INTEGER NOT NULL,
    version INTEGER NOT NULL,
    last_tx_id INTEGER,
    PRIMARY KEY (tenant, table_name, row_key_json),
    FOREIGN KEY (last_tx_id) REFERENCES tx(tx_id)
);

CREATE TABLE IF NOT EXISTS effective_writes (
    event_seq INTEGER PRIMARY KEY,
    tx_id INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key_json TEXT NOT NULL,
    column_name TEXT NOT NULL,
    action TEXT NOT NULL,
    value_json TEXT,
    conflict_ids_json TEXT NOT NULL,
    FOREIGN KEY (tx_id) REFERENCES tx(tx_id)
);

CREATE TABLE IF NOT EXISTS ledger_versions (
    generation INTEGER PRIMARY KEY,
    ledger_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_clock INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_order
    ON tx(commit_clock_ms, tx_id);
CREATE INDEX IF NOT EXISTS idx_tx_status
    ON tx(status);
CREATE INDEX IF NOT EXISTS idx_writes_row
    ON tx_writes(tenant, table_name, row_key_json);
CREATE INDEX IF NOT EXISTS idx_conflicts_right
    ON conflicts(right_tx_id, status);
CREATE INDEX IF NOT EXISTS idx_conflicts_left
    ON conflicts(left_tx_id, status);
CREATE INDEX IF NOT EXISTS idx_conflicts_order
    ON conflicts(right_tx_id, left_tx_id, tenant, table_name, row_key_json);
"""


DEFAULT_STATE = {
    "schema_version": "1",
    "physical_schema_hash": "",
    "schema_hash": "",
    "schema_doc": "",
    "config_doc": "",
    "current_policy_version": "1",
    "commit_seq_high_water": "0",
    "clock_high_water": "0",
    "detection_generation": "0",
    "resolution_generation": "0",
    "ledger_pointer_generation": "0",
    "pending_conflict_digest": (
        "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    ),
}


def connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=60.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection, immediate: bool = True) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def is_initialized(connection: sqlite3.Connection) -> bool:
    return _table_exists(connection, "engine_state") and _table_exists(
        connection, "policy_versions"
    )


def open_initialized(path: str) -> sqlite3.Connection:
    if path != ":memory:" and not Path(path).exists():
        raise WarbiterError(
            "E_NOT_INITIALIZED",
            "database does not exist; run init or apply with --schema/--config",
            {"db": path},
        )
    connection = connect(path)
    if not is_initialized(connection):
        connection.close()
        raise WarbiterError(
            "E_NOT_INITIALIZED",
            "database is not initialized; run init or apply with --schema/--config",
            {"db": path},
        )
    return connection


def get_state(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute(
        "SELECT value FROM engine_state WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        raise WarbiterError("E_DB", f"missing engine state key {key}")
    return str(row["value"])


def get_json_state(connection: sqlite3.Connection, key: str) -> Any:
    import json

    return json.loads(get_state(connection, key))


def set_state(connection: sqlite3.Connection, key: str, value: Any) -> None:
    text = value if isinstance(value, str) else canonical_json(value)
    connection.execute(
        """
        INSERT INTO engine_state(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, text),
    )


def next_generation(connection: sqlite3.Connection, key: str) -> int:
    current = int(get_state(connection, key))
    value = current + 1
    set_state(connection, key, str(value))
    return value


def current_policy_version(connection: sqlite3.Connection) -> int:
    return int(get_state(connection, "current_policy_version"))


def get_policy_version_document(
    connection: sqlite3.Connection, version: int
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT policy_json FROM policy_versions WHERE version=?", (version,)
    ).fetchone()
    if row is None:
        raise WarbiterError("E_DB", f"unknown policy version {version}")
    import json

    return json.loads(row["policy_json"])


def policy_table_map(policy_document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in policy_document.get("tables", []):
        result[(item["tenant"], item["table"])] = item["policy"]
    return result


def _stage_policy_version_locked(
    connection: sqlite3.Connection,
    schema: Schema,
) -> int:
    document = schema.policy_document()
    text = canonical_json(document)
    policy_hash = sha256_json(document)
    current = connection.execute(
        "SELECT version, policy_hash FROM policy_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if current is not None and current["policy_hash"] == policy_hash:
        set_state(connection, "current_policy_version", str(current["version"]))
        return int(current["version"])
    version = 1 if current is None else int(current["version"]) + 1
    labels = sorted(
        {
            item["policy"]["version_label"]
            for item in document.get("tables", [])
            if item.get("policy", {}).get("version_label")
        }
    )
    label = "+".join(labels) if labels else "policy"
    generation = int(get_state(connection, "detection_generation"))
    connection.execute("DROP TABLE IF EXISTS temp.policy_stage")
    connection.execute(
        """
        CREATE TEMP TABLE policy_stage (
            version INTEGER,
            policy_json TEXT,
            policy_hash TEXT,
            label TEXT,
            created_generation INTEGER
        )
        """
    )
    connection.execute(
        """
        INSERT INTO policy_stage(version, policy_json, policy_hash, label, created_generation)
        VALUES(?, ?, ?, ?, ?)
        """,
        (version, text, policy_hash, label, generation),
    )
    connection.execute(
        """
        INSERT INTO policy_versions(version, policy_json, policy_hash, label, created_generation)
        SELECT version, policy_json, policy_hash, label, created_generation
        FROM policy_stage
        """
    )
    set_state(connection, "current_policy_version", str(version))
    connection.execute("DROP TABLE temp.policy_stage")
    return version


def initialize_database(
    path: str,
    schema: Schema,
    config: ArbiterConfig,
) -> dict[str, Any]:
    connection = connect(path)
    try:
        connection.executescript(DDL)
        with transaction(connection):
            for key, value in DEFAULT_STATE.items():
                connection.execute(
                    "INSERT OR IGNORE INTO engine_state(key, value) VALUES(?, ?)",
                    (key, value),
                )
            physical_document = schema.physical_document()
            physical_hash = sha256_json(physical_document)
            stored_physical = get_state(connection, "physical_schema_hash")
            stored_doc = get_state(connection, "schema_doc")
            if stored_doc == "":
                set_state(connection, "physical_schema_hash", physical_hash)
                set_state(connection, "schema_hash", sha256_json(schema.as_dict()))
                set_state(connection, "schema_doc", canonical_json(schema.as_dict()))
                set_state(connection, "config_doc", canonical_json(config.as_dict()))
                set_state(connection, "clock_high_water", "0")
                policy_version = _stage_policy_version_locked(connection, schema)
            else:
                if stored_physical != physical_hash:
                    raise WarbiterError(
                        "E_SCHEMA_CHANGED",
                        "physical schema cannot change after initialization",
                        {
                            "stored_physical_hash": stored_physical,
                            "incoming_physical_hash": physical_hash,
                        },
                    )
                set_state(connection, "schema_hash", sha256_json(schema.as_dict()))
                set_state(connection, "schema_doc", canonical_json(schema.as_dict()))
                set_state(connection, "config_doc", canonical_json(config.as_dict()))
                policy_version = _stage_policy_version_locked(connection, schema)
            return {
                "schema_hash": get_state(connection, "schema_hash"),
                "physical_schema_hash": get_state(connection, "physical_schema_hash"),
                "policy_version": policy_version,
                "policy_digest": sha256_json(
                    get_policy_version_document(connection, policy_version)
                ),
            }
    finally:
        connection.close()


def database_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute("SELECT key, value FROM engine_state ORDER BY key").fetchall()
    state = {row["key"]: row["value"] for row in rows}
    policy = connection.execute(
        "SELECT version, policy_hash, label FROM policy_versions WHERE version=?",
        (int(state["current_policy_version"]),),
    ).fetchone()
    ledger = None
    pointer = int(state.get("ledger_pointer_generation", "0"))
    if pointer:
        ledger = connection.execute(
            "SELECT generation, digest FROM ledger_versions WHERE generation=?",
            (pointer,),
        ).fetchone()
    return {
        "schema_hash": state.get("schema_hash", ""),
        "physical_schema_hash": state.get("physical_schema_hash", ""),
        "current_policy_version": int(state.get("current_policy_version", "0")),
        "current_policy_hash": policy["policy_hash"] if policy else "",
        "current_policy_label": policy["label"] if policy else "",
        "commit_seq_high_water": int(state.get("commit_seq_high_water", "0")),
        "clock_high_water": int(state.get("clock_high_water", "0")),
        "detection_generation": int(state.get("detection_generation", "0")),
        "resolution_generation": int(state.get("resolution_generation", "0")),
        "ledger_pointer_generation": pointer,
        "ledger_digest": ledger["digest"] if ledger else "",
        "pending_conflict_digest": state.get("pending_conflict_digest", ""),
    }
