"""SQLite persistence for policies, closures, requests, and ledgers."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError
from .model import ServiceTemplate, Topology
from .util import canonical_json, now_ms, sha256_json

SCHEMA_VERSION = 1
CLOCK_TOLERANCE_MS = 100


SCHEMA_SQL = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_watermark (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    logical_clock_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS template_manifests (
    manifest_version TEXT PRIMARY KEY,
    input_digest TEXT NOT NULL,
    loaded_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS template_manifest_entries (
    manifest_version TEXT NOT NULL,
    template_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    dependencies_json TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    logical_clock INTEGER NOT NULL,
    resources_json TEXT NOT NULL,
    PRIMARY KEY (manifest_version, template_name),
    FOREIGN KEY (manifest_version) REFERENCES template_manifests(manifest_version)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS policy_versions (
    policy_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_key TEXT NOT NULL,
    manifest_version TEXT NOT NULL,
    policy_generation INTEGER NOT NULL,
    logical_clock INTEGER NOT NULL,
    source_bundle_digest TEXT NOT NULL,
    dependency_digest TEXT NOT NULL,
    dependency_digest_set_json TEXT NOT NULL,
    mode TEXT NOT NULL,
    parent_version_id INTEGER,
    state TEXT NOT NULL CHECK (state IN ('staging', 'committed')),
    source_summary_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (policy_key, policy_generation, source_bundle_digest)
);

CREATE TABLE IF NOT EXISTS policy_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    policy_version_id INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (policy_version_id) REFERENCES policy_versions(policy_version_id)
);

CREATE TABLE IF NOT EXISTS policy_nodes (
    policy_version_id INTEGER NOT NULL,
    template_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    dependencies_json TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    logical_clock INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    source_format TEXT NOT NULL,
    source_version TEXT NOT NULL,
    original_time_json TEXT NOT NULL,
    conversion_json TEXT NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    decision_basis TEXT NOT NULL,
    decision_detail TEXT NOT NULL,
    tombstone INTEGER NOT NULL DEFAULT 0 CHECK (tombstone IN (0, 1)),
    tombstone_reason TEXT,
    PRIMARY KEY (policy_version_id, template_name),
    FOREIGN KEY (policy_version_id) REFERENCES policy_versions(policy_version_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS policy_edges (
    policy_version_id INTEGER NOT NULL,
    from_template TEXT NOT NULL,
    to_template TEXT NOT NULL,
    source_format TEXT NOT NULL,
    source_version TEXT NOT NULL,
    logical_clock INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    original_time_json TEXT NOT NULL,
    conversion_json TEXT NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    decision_basis TEXT NOT NULL,
    decision_detail TEXT NOT NULL,
    PRIMARY KEY (policy_version_id, from_template, to_template),
    FOREIGN KEY (policy_version_id, from_template)
        REFERENCES policy_nodes(policy_version_id, template_name)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS template_closures (
    policy_version_id INTEGER NOT NULL,
    template_name TEXT NOT NULL,
    closure_json TEXT NOT NULL,
    closure_digest TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    dependency_digest TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (policy_version_id, template_name),
    FOREIGN KEY (policy_version_id, template_name)
        REFERENCES policy_nodes(policy_version_id, template_name)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS policy_tombstones (
    policy_version_id INTEGER NOT NULL,
    template_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    logical_clock INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (policy_version_id, template_name),
    FOREIGN KEY (policy_version_id) REFERENCES policy_versions(policy_version_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS topology_invalidations (
    policy_version_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    logical_clock INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (policy_version_id, scope),
    FOREIGN KEY (policy_version_id) REFERENCES policy_versions(policy_version_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS merge_decisions (
    policy_key TEXT NOT NULL,
    logical_clock INTEGER NOT NULL,
    template_name TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (policy_key, logical_clock, template_name)
);

CREATE TABLE IF NOT EXISTS invalidation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_version_id INTEGER NOT NULL,
    trigger_template TEXT NOT NULL,
    trigger_version INTEGER NOT NULL,
    trigger_generation INTEGER NOT NULL,
    trigger_clock INTEGER NOT NULL,
    reason TEXT NOT NULL,
    changed_json TEXT NOT NULL,
    affected_json TEXT NOT NULL,
    chains_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (policy_version_id) REFERENCES policy_versions(policy_version_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS policy_apply_jobs (
    job_id TEXT PRIMARY KEY,
    package_path TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    policy_generation INTEGER NOT NULL,
    manifest_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'applied', 'noop', 'failed')
    ),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS schedule_runs (
    run_id TEXT PRIMARY KEY,
    policy_version_id INTEGER NOT NULL,
    round_budget_ms INTEGER NOT NULL,
    worker_pid INTEGER NOT NULL,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    status TEXT NOT NULL,
    recovered INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (policy_version_id) REFERENCES policy_versions(policy_version_id)
);

CREATE TABLE IF NOT EXISTS request_state (
    request_seq INTEGER PRIMARY KEY,
    instance_id TEXT NOT NULL UNIQUE,
    template_name TEXT NOT NULL,
    target_topology_namespace TEXT NOT NULL,
    desired_state TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    logical_cost_ms INTEGER NOT NULL,
    policy_version_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    reason_code TEXT,
    reason_detail TEXT,
    decision_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    heartbeat_ms INTEGER,
    started_at_ms INTEGER,
    updated_at_ms INTEGER NOT NULL,
    committed_at_ms INTEGER,
    FOREIGN KEY (policy_version_id) REFERENCES policy_versions(policy_version_id)
);

CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    request_seq INTEGER NOT NULL UNIQUE,
    policy_version_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    template_name TEXT NOT NULL,
    target_topology_namespace TEXT NOT NULL,
    status TEXT NOT NULL,
    committed_seq INTEGER NOT NULL,
    committed_at_ms INTEGER NOT NULL,
    FOREIGN KEY (request_seq) REFERENCES request_state(request_seq),
    FOREIGN KEY (policy_version_id) REFERENCES policy_versions(policy_version_id)
);

CREATE TABLE IF NOT EXISTS instance_resources (
    instance_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    template_name TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (instance_id, node_id, template_name, resource_name),
    FOREIGN KEY (instance_id) REFERENCES instances(instance_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resource_ledger (
    node_id TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity >= 0),
    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
    PRIMARY KEY (node_id, resource_name)
);

CREATE TABLE IF NOT EXISTS ledger_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    before_value INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    after_value INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (instance_id) REFERENCES instances(instance_id)
);

CREATE TABLE IF NOT EXISTS template_concurrency (
    node_id TEXT NOT NULL,
    template_name TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
    limit_value INTEGER NOT NULL CHECK (limit_value > 0),
    PRIMARY KEY (node_id, template_name)
);

CREATE TABLE IF NOT EXISTS recovery_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_versions_generation
    ON policy_versions(policy_key, policy_generation);
CREATE INDEX IF NOT EXISTS idx_invalidation_events_policy
    ON invalidation_events(policy_version_id, event_id);
CREATE INDEX IF NOT EXISTS idx_request_outcomes
    ON request_state(status, outcome, request_seq);
CREATE INDEX IF NOT EXISTS idx_ledger_events_instance
    ON ledger_events(instance_id, node_id, resource_name, event_id);
"""


def runtime_db_path(workdir: Path) -> Path:
    return workdir / "runtime.sqlite3"


def connect(workdir: Path) -> sqlite3.Connection:
    workdir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(runtime_db_path(workdir)),
        timeout=30.0,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        """
        INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )


@contextlib.contextmanager
def transaction(
    connection: sqlite3.Connection,
    *,
    immediate: bool = True,
) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def get_runtime_state(
    connection: sqlite3.Connection,
    key: str,
    default: Any = None,
) -> Any:
    row = connection.execute(
        "SELECT value_json FROM runtime_state WHERE key=?",
        (key,),
    ).fetchone()
    if row is None:
        return default
    return json.loads(row["value_json"])


def set_runtime_state(
    connection: sqlite3.Connection,
    key: str,
    value: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO runtime_state(key, value_json) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
        """,
        (key, canonical_json(value)),
    )


def initialize_runtime(
    connection: sqlite3.Connection,
    *,
    manifest_version: str,
    templates: dict[str, ServiceTemplate],
    topology: Topology,
    injected_clock_ms: int,
    input_digest: str,
    clock_tolerance_ms: int = CLOCK_TOLERANCE_MS,
    allow_clock_advance: bool = False,
) -> dict[str, Any]:
    init_schema(connection)
    with transaction(connection):
        previous_rows = connection.execute(
            "SELECT manifest_version FROM template_manifests"
        ).fetchall()
        previous_versions = [row["manifest_version"] for row in previous_rows]
        from .util import compare_semver, parse_semver

        parse_semver(manifest_version)
        if previous_versions:
            latest = max(previous_versions, key=parse_semver)
            if compare_semver(manifest_version, latest) < 0:
                raise BusinessError(
                    "MANIFEST_VERSION_ROLLBACK",
                    f"Template manifest version rolled back from {latest} to {manifest_version}",
                    version=manifest_version,
                    previous_version=latest,
                )
        manifest_row = connection.execute(
            """
            SELECT input_digest FROM template_manifests
            WHERE manifest_version=?
            """,
            (manifest_version,),
        ).fetchone()
        if manifest_row and manifest_row["input_digest"] != input_digest:
            raise BusinessError(
                "MANIFEST_DIGEST_MISMATCH",
                "The same template manifest version was supplied with different content",
                version=manifest_version,
                expected_digest=manifest_row["input_digest"],
                actual_digest=input_digest,
            )
        connection.execute(
            """
            INSERT INTO template_manifests(
                manifest_version, input_digest, loaded_at_ms
            ) VALUES (?, ?, ?)
            ON CONFLICT(manifest_version) DO NOTHING
            """,
            (manifest_version, input_digest, now_ms()),
        )
        for template in sorted(templates.values(), key=lambda item: item.name):
            connection.execute(
                """
                INSERT INTO template_manifest_entries(
                    manifest_version, template_name, version,
                    dependencies_json, labels_json, logical_clock,
                    resources_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(manifest_version, template_name) DO NOTHING
                """,
                (
                    manifest_version,
                    template.name,
                    template.version,
                    canonical_json(list(template.dependencies)),
                    canonical_json(list(template.labels)),
                    template.logical_clock,
                    canonical_json(template.resources_dict()),
                ),
            )
        clock_row = connection.execute(
            "SELECT logical_clock_ms FROM clock_watermark WHERE singleton=1"
        ).fetchone()
        if clock_row is None:
            connection.execute(
                """
                INSERT INTO clock_watermark(
                    singleton, logical_clock_ms, updated_at_ms
                ) VALUES (1, ?, ?)
                """,
                (injected_clock_ms, now_ms()),
            )
        effective_clock = injected_clock_ms
        if clock_row is not None:
            current_clock = int(clock_row["logical_clock_ms"])
            if injected_clock_ms + clock_tolerance_ms < current_clock:
                if allow_clock_advance:
                    effective_clock = current_clock
                else:
                    raise BusinessError(
                        "CLOCK_ROLLBACK",
                        "Injected logical clock is behind the persisted clock watermark",
                        logical_clock=injected_clock_ms,
                        watermark=current_clock,
                        tolerance_ms=clock_tolerance_ms,
                    )
            else:
                effective_clock = max(injected_clock_ms, current_clock)
            connection.execute(
                """
                UPDATE clock_watermark
                SET logical_clock_ms=?, updated_at_ms=?
                WHERE singleton=1
                """,
                (effective_clock, now_ms()),
            )
        for node_id, node in sorted(topology.nodes.items()):
            for resource_name, capacity in sorted(node["resources"].items()):
                existing = connection.execute(
                    """
                    SELECT used FROM resource_ledger
                    WHERE node_id=? AND resource_name=?
                    """,
                    (node_id, resource_name),
                ).fetchone()
                if existing is not None and existing["used"] > capacity:
                    raise BusinessError(
                        "RESOURCE_CAPACITY_ROLLBACK",
                        "New resource capacity is below already committed usage",
                        node_id=node_id,
                        resource_name=resource_name,
                        capacity=capacity,
                        used=existing["used"],
                    )
                connection.execute(
                    """
                    INSERT INTO resource_ledger(
                        node_id, resource_name, capacity, used
                    ) VALUES (?, ?, ?, 0)
                    ON CONFLICT(node_id, resource_name) DO UPDATE
                    SET capacity=excluded.capacity
                    """,
                    (node_id, resource_name, capacity),
                )
            for template_name, limit in sorted(
                node["template_concurrency"].items()
            ):
                connection.execute(
                    """
                    INSERT INTO template_concurrency(
                        node_id, template_name, used, limit_value
                    ) VALUES (?, ?, 0, ?)
                    ON CONFLICT(node_id, template_name) DO UPDATE
                    SET limit_value=excluded.limit_value
                    """,
                    (node_id, template_name, limit),
                )
        set_runtime_state(
            connection,
            "topology",
            {
                "max_depth": topology.namespaces[
                    sorted(topology.namespaces)[0]
                ].max_depth,
                "namespaces": {
                    name: {
                        "node_id": namespace.node_id,
                        "max_depth": namespace.max_depth,
                    }
                    for name, namespace in sorted(topology.namespaces.items())
                },
            },
        )
    return {
        "manifest_version": manifest_version,
        "injected_clock_ms": effective_clock,
    }


def clock_watermark(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT logical_clock_ms FROM clock_watermark WHERE singleton=1"
    ).fetchone()
    return 0 if row is None else int(row["logical_clock_ms"])


def advance_clock(
    connection: sqlite3.Connection,
    value: int,
    *,
    tolerance_ms: int = CLOCK_TOLERANCE_MS,
    request_seq: int | None = None,
) -> None:
    row = connection.execute(
        "SELECT logical_clock_ms FROM clock_watermark WHERE singleton=1"
    ).fetchone()
    current = 0 if row is None else int(row["logical_clock_ms"])
    if value + tolerance_ms < current:
        raise BusinessError(
            "CLOCK_ROLLBACK",
            "Logical clock rolled back beyond tolerance",
            logical_clock=value,
            watermark=current,
            tolerance_ms=tolerance_ms,
            request_seq=request_seq,
        )
    connection.execute(
        """
        INSERT INTO clock_watermark(
            singleton, logical_clock_ms, updated_at_ms
        ) VALUES (1, ?, ?)
        ON CONFLICT(singleton) DO UPDATE
        SET logical_clock_ms=excluded.logical_clock_ms,
            updated_at_ms=excluded.updated_at_ms
        """,
        (max(current, value), now_ms()),
    )


def active_policy_version_id(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT policy_version_id FROM policy_pointer WHERE singleton=1"
    ).fetchone()
    return None if row is None else int(row["policy_version_id"])


def active_policy_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
    version_id = active_policy_version_id(connection)
    if version_id is None:
        return None
    return connection.execute(
        """
        SELECT * FROM policy_versions
        WHERE policy_version_id=? AND state='committed'
        """,
        (version_id,),
    ).fetchone()


def policy_rows(
    connection: sqlite3.Connection,
    policy_version_id: int,
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM policy_nodes
        WHERE policy_version_id=?
        ORDER BY template_name
        """,
        (policy_version_id,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result[row["template_name"]] = {
            "template_name": row["template_name"],
            "version": row["version"],
            "dependencies": tuple(json.loads(row["dependencies_json"])),
            "labels": tuple(json.loads(row["labels_json"])),
            "logical_clock": row["logical_clock"],
            "policy_generation": row["policy_generation"],
            "source_format": row["source_format"],
            "source_version": row["source_version"],
            "original_time": json.loads(row["original_time_json"]),
            "conversion": json.loads(row["conversion_json"]),
            "effective_at_ms": row["effective_at_ms"],
            "decision_basis": row["decision_basis"],
            "decision_detail": row["decision_detail"],
            "tombstone": bool(row["tombstone"]),
            "tombstone_reason": row["tombstone_reason"],
        }
    return result


def closure_rows(
    connection: sqlite3.Connection,
    policy_version_id: int,
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM template_closures
        WHERE policy_version_id=?
        ORDER BY template_name
        """,
        (policy_version_id,),
    ).fetchall()
    return {
        row["template_name"]: {
            "template_name": row["template_name"],
            "closure": tuple(json.loads(row["closure_json"])),
            "closure_digest": row["closure_digest"],
            "config_fingerprint": row["config_fingerprint"],
            "dependency_digest": row["dependency_digest"],
            "revision": row["revision"],
        }
        for row in rows
    }


def read_merge_decision(
    connection: sqlite3.Connection,
    policy_key: str,
    logical_clock: int,
    template_name: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT decision_hash, decision_json FROM merge_decisions
        WHERE policy_key=? AND logical_clock=? AND template_name=?
        """,
        (policy_key, logical_clock, template_name),
    ).fetchone()
    if row is None:
        return None
    return {
        "decision_hash": row["decision_hash"],
        "decision": json.loads(row["decision_json"]),
    }


def write_merge_decision(
    connection: sqlite3.Connection,
    policy_key: str,
    logical_clock: int,
    template_name: str,
    decision: dict[str, Any],
) -> str:
    decision_hash = sha256_json(decision)
    existing = read_merge_decision(
        connection,
        policy_key,
        logical_clock,
        template_name,
    )
    if existing is not None:
        if existing["decision_hash"] != decision_hash:
            raise BusinessError(
                "MERGE_DECISION_DRIFT",
                "Policy merge decision changed for the same logical clock",
                template_name=template_name,
                logical_clock=logical_clock,
                expected_hash=existing["decision_hash"],
                actual_hash=decision_hash,
            )
        return existing["decision_hash"]
    connection.execute(
        """
        INSERT INTO merge_decisions(
            policy_key, logical_clock, template_name,
            decision_hash, decision_json, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            policy_key,
            logical_clock,
            template_name,
            decision_hash,
            canonical_json(decision),
            now_ms(),
        ),
    )
    return decision_hash


def semantic_resource_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT node_id, resource_name, capacity, used
        FROM resource_ledger
        ORDER BY node_id, resource_name
        """
    ).fetchall()
    return [
        {
            "node_id": row["node_id"],
            "resource_name": row["resource_name"],
            "capacity": row["capacity"],
            "used": row["used"],
            "available": row["capacity"] - row["used"],
        }
        for row in rows
    ]


def request_outcome_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT request_seq, instance_id, template_name,
               target_topology_namespace, status, outcome,
               reason_code, policy_version_id, logical_clock_ms
        FROM request_state
        ORDER BY request_seq
        """
    ).fetchall()
    return [
        {
            "request_seq": row["request_seq"],
            "instance_id": row["instance_id"],
            "template_name": row["template_name"],
            "namespace": row["target_topology_namespace"],
            "target_topology_namespace": row["target_topology_namespace"],
            "status": row["status"],
            "outcome": row["outcome"],
            "reason_code": row["reason_code"],
            "policy_version_id": row["policy_version_id"],
            "logical_clock_ms": row["logical_clock_ms"],
        }
        for row in rows
    ]


def record_recovery_event(
    connection: sqlite3.Connection,
    action: str,
    details: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO recovery_events(action, details_json, created_at_ms)
        VALUES (?, ?, ?)
        """,
        (action, canonical_json(details), now_ms()),
    )
