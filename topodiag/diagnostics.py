"""Stable status rendering and exhaustive on-disk consistency verification."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .closures import (
    closure_semantic_rows,
    verify_closure_records,
)
from .engine import candidates_from_rows, load_active_view
from .errors import BusinessError
from .policy import dependency_digest, dependency_digest_set
from .storage import (
    active_policy_row,
    active_policy_version_id,
    clock_watermark,
    connect,
    init_schema,
    policy_rows,
    request_outcome_rows,
    semantic_resource_rows,
)
from .util import now_ms, sha256_json


def _parse_invalidations(connection: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM invalidation_events
        WHERE policy_version_id=?
        ORDER BY event_id
        """,
        (version_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "trigger": {
                    "template_name": row["trigger_template"],
                    "version": row["trigger_version"],
                    "policy_generation": row["trigger_generation"],
                    "logical_clock": row["trigger_clock"],
                },
                "reason": row["reason"],
                "changed_templates": json.loads(row["changed_json"]),
                "affected_downstream": json.loads(row["affected_json"]),
                "chains": json.loads(row["chains_json"]),
                "versions_before": json.loads(row["before_json"]),
                "versions_after": json.loads(row["after_json"]),
            }
        )
    return result


def _topology_invalidations(
    connection: sqlite3.Connection,
    version_id: int,
) -> list[dict[str, Any]]:
    return [
        {
            "scope": row["scope"],
            "logical_clock": row["logical_clock"],
            "reason": row["reason"],
        }
        for row in connection.execute(
            """
            SELECT scope, logical_clock, reason
            FROM topology_invalidations
            WHERE policy_version_id=?
            ORDER BY scope
            """,
            (version_id,),
        ).fetchall()
    ]


def _instances(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "instance_id": row["instance_id"],
            "request_seq": row["request_seq"],
            "policy_version_id": row["policy_version_id"],
            "node_id": row["node_id"],
            "template_name": row["template_name"],
            "target_topology_namespace": row["target_topology_namespace"],
            "status": row["status"],
            "committed_seq": row["committed_seq"],
        }
        for row in connection.execute(
            """
            SELECT * FROM instances
            ORDER BY request_seq, instance_id
            """
        ).fetchall()
    ]


def _policy_jobs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "bundle_digest": row["bundle_digest"],
            "policy_generation": row["policy_generation"],
            "manifest_version": row["manifest_version"],
            "status": row["status"],
        }
        for row in connection.execute(
            """
            SELECT job_id, package_path, bundle_digest, policy_generation,
                   manifest_version, status
            FROM policy_apply_jobs
            ORDER BY policy_generation, job_id
            """
        ).fetchall()
    ]


def _template_concurrency(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "node_id": row["node_id"],
            "template_name": row["template_name"],
            "used": row["used"],
            "limit": row["limit_value"],
            "available": row["limit_value"] - row["used"],
        }
        for row in connection.execute(
            """
            SELECT node_id, template_name, used, limit_value
            FROM template_concurrency
            ORDER BY node_id, template_name
            """
        ).fetchall()
    ]


def _watermarks(connection: sqlite3.Connection) -> dict[str, Any]:
    committed_row = connection.execute(
        "SELECT value_json FROM runtime_state WHERE key='committed_instance_watermark'"
    ).fetchone()
    instance_row = connection.execute(
        "SELECT MAX(committed_seq) AS value FROM instances"
    ).fetchone()
    completed_row = connection.execute(
        "SELECT COUNT(*) AS value FROM request_state WHERE outcome='completed'"
    ).fetchone()
    terminal_row = connection.execute(
        """
        SELECT COUNT(*) AS value FROM request_state
        WHERE outcome IS NOT NULL
        """
    ).fetchone()
    return {
        "committed_instance_watermark": (
            0
            if committed_row is None
            else int(json.loads(committed_row["value_json"]))
        ),
        "max_committed_instance_seq": (
            0 if instance_row["value"] is None else int(instance_row["value"])
        ),
        "completed_requests": int(completed_row["value"]),
        "terminal_requests": int(terminal_row["value"]),
        "logical_clock_ms": clock_watermark(connection),
    }


def semantic_projection(connection: sqlite3.Connection) -> dict[str, Any]:
    version_id = active_policy_version_id(connection)
    if version_id is None:
        return {
            "policy_pointer": None,
            "closures": [],
            "resources": semantic_resource_rows(connection),
        }
    version = active_policy_row(connection)
    candidates = candidates_from_rows(policy_rows(connection, version_id))
    closures = load_active_view(connection)[2]
    return {
        "policy_pointer": {
            "policy_version_id": version_id,
            "policy_key": version["policy_key"],
            "manifest_version": version["manifest_version"],
            "policy_generation": version["policy_generation"],
            "logical_clock": version["logical_clock"],
            "source_bundle_digest": version["source_bundle_digest"],
            "dependency_digest": version["dependency_digest"],
            "mode": version["mode"],
        },
        "policy_nodes": [
            {
                "template_name": name,
                "version": candidate.version,
                "dependencies": list(candidate.dependencies),
                "labels": list(candidate.labels),
                "source_format": candidate.source_format,
                "source_version": candidate.source_version,
                "effective_at_ms": candidate.effective_at_ms,
                "tombstone": candidate.tombstone,
            }
            for name, candidate in sorted(candidates.items())
        ],
        "closures": closure_semantic_rows(closures),
        "resources": semantic_resource_rows(connection),
        "template_concurrency": _template_concurrency(connection),
        "request_outcomes": request_outcome_rows(connection),
        "instances": _instances(connection),
        "watermarks": _watermarks(connection),
        "invalidations": _parse_invalidations(connection, version_id),
        "topology_invalidations": _topology_invalidations(connection, version_id),
        "policy_apply_jobs": _policy_jobs(connection),
    }


def semantic_digest(connection: sqlite3.Connection) -> str:
    return sha256_json(semantic_projection(connection))


def status_workspace(workdir: Path) -> dict[str, Any]:
    connection = connect(workdir)
    try:
        init_schema(connection)
        version_id = active_policy_version_id(connection)
        if version_id is None:
            raise BusinessError(
                "POLICY_NOT_BUILT",
                "No active policy version exists; run build first",
            )
        projection = semantic_projection(connection)
        version = active_policy_row(connection)
        return {
            "ok": True,
            "command": "status",
            "policy_pointer": projection["policy_pointer"],
            "policy": {
                "manifest_version": version["manifest_version"],
                "policy_generation": version["policy_generation"],
                "logical_clock": version["logical_clock"],
                "dependency_digest": version["dependency_digest"],
                "state": version["state"],
            },
            "closures": projection["closures"],
            "resources": projection["resources"],
            "template_concurrency": projection["template_concurrency"],
            "watermarks": projection["watermarks"],
            "instances": projection["instances"],
            "request_outcomes": projection["request_outcomes"],
            "invalidations": projection["invalidations"],
            "topology_invalidations": projection["topology_invalidations"],
            "policy_apply_jobs": projection["policy_apply_jobs"],
            "semantic_digest": sha256_json(projection),
            "generated_at_ms": now_ms(),
        }
    finally:
        connection.close()


def _check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    details: Any = None,
) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details})


def verify_workspace(workdir: Path) -> dict[str, Any]:
    connection = connect(workdir)
    checks: list[dict[str, Any]] = []
    try:
        init_schema(connection)
        integrity = [
            row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()
        ]
        _check(checks, "sqlite_integrity", integrity == ["ok"], integrity)
        version_id = active_policy_version_id(connection)
        _check(checks, "active_policy_present", version_id is not None, version_id)
        if version_id is None:
            return {
                "ok": False,
                "command": "verify",
                "checks": sorted(checks, key=lambda item: item["name"]),
                "errors": ["no active policy version"],
            }
        version = connection.execute(
            """
            SELECT * FROM policy_versions WHERE policy_version_id=?
            """,
            (version_id,),
        ).fetchone()
        _check(
            checks,
            "active_policy_committed",
            version is not None and version["state"] == "committed",
            None if version is None else version["state"],
        )
        staging_count = connection.execute(
            "SELECT COUNT(*) AS count FROM policy_versions WHERE state='staging'"
        ).fetchone()["count"]
        _check(checks, "no_staging_policy_versions", staging_count == 0, staging_count)
        candidates, closures = load_active_view(connection)[1:]
        closure_errors = verify_closure_records(candidates, closures)
        _check(
            checks,
            "closure_fingerprints",
            not closure_errors,
            sorted(closure_errors),
        )
        actual_digest = dependency_digest(candidates.values())
        _check(
            checks,
            "dependency_digest",
            actual_digest == version["dependency_digest"],
            {
                "expected": version["dependency_digest"],
                "actual": actual_digest,
            },
        )
        stored_digest_set = json.loads(version["dependency_digest_set_json"])
        actual_digest_set = dependency_digest_set(candidates.values())
        _check(
            checks,
            "dependency_digest_set",
            stored_digest_set == actual_digest_set,
            {
                "expected": stored_digest_set,
                "actual": actual_digest_set,
            },
        )
        ledger_errors: list[str] = []
        ledger_rows = connection.execute(
            "SELECT * FROM resource_ledger ORDER BY node_id, resource_name"
        ).fetchall()
        for row in ledger_rows:
            if row["used"] < 0 or row["used"] > row["capacity"]:
                ledger_errors.append(
                    f"{row['node_id']}/{row['resource_name']} used={row['used']} "
                    f"capacity={row['capacity']}"
                )
        expected = {
            (row["node_id"], row["resource_name"]): int(row["amount"])
            for row in connection.execute(
                """
                SELECT ir.node_id, ir.resource_name, SUM(ir.amount) AS amount
                FROM instance_resources ir
                JOIN instances i ON i.instance_id=ir.instance_id
                WHERE i.status='committed'
                GROUP BY ir.node_id, ir.resource_name
                """
            ).fetchall()
        }
        for row in ledger_rows:
            key = (row["node_id"], row["resource_name"])
            if int(row["used"]) != expected.get(key, 0):
                ledger_errors.append(
                    f"{row['node_id']}/{row['resource_name']} ledger="
                    f"{row['used']} instance_sum={expected.get(key, 0)}"
                )
        event_chain_errors: list[str] = []
        resources_in_events = {
            (row["node_id"], row["resource_name"])
            for row in connection.execute(
                """
                SELECT DISTINCT node_id, resource_name FROM ledger_events
                """
            ).fetchall()
        }
        for key in sorted(resources_in_events):
            rows = connection.execute(
                """
                SELECT * FROM ledger_events
                WHERE node_id=? AND resource_name=?
                ORDER BY event_id
                """,
                key,
            ).fetchall()
            running = 0
            for row in rows:
                if row["before_value"] != running:
                    event_chain_errors.append(
                        f"{key} event {row['event_id']} before="
                        f"{row['before_value']} expected={running}"
                    )
                if row["after_value"] != row["before_value"] + row["delta"]:
                    event_chain_errors.append(
                        f"{key} event {row['event_id']} arithmetic mismatch"
                    )
                running = int(row["after_value"])
            ledger_row = connection.execute(
                """
                SELECT used FROM resource_ledger
                WHERE node_id=? AND resource_name=?
                """,
                key,
            ).fetchone()
            if ledger_row is not None and running != int(ledger_row["used"]):
                event_chain_errors.append(
                    f"{key} event final={running} ledger={ledger_row['used']}"
                )
        _check(
            checks,
            "resource_ledger",
            not ledger_errors,
            sorted(ledger_errors),
        )
        _check(
            checks,
            "ledger_event_chain",
            not event_chain_errors,
            sorted(event_chain_errors),
        )
        concurrency_errors: list[str] = []
        expected_concurrency = {
            (row["node_id"], row["template_name"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT node_id, template_name, COUNT(*) AS count
                FROM instances
                WHERE status='committed'
                GROUP BY node_id, template_name
                """
            ).fetchall()
        }
        for row in connection.execute(
            "SELECT * FROM template_concurrency"
        ).fetchall():
            key = (row["node_id"], row["template_name"])
            if int(row["used"]) != expected_concurrency.get(key, 0):
                concurrency_errors.append(
                    f"{key} used={row['used']} actual="
                    f"{expected_concurrency.get(key, 0)}"
                )
            if int(row["used"]) > int(row["limit_value"]):
                concurrency_errors.append(
                    f"{key} used={row['used']} exceeds limit={row['limit_value']}"
                )
        _check(
            checks,
            "template_concurrency",
            not concurrency_errors,
            sorted(concurrency_errors),
        )
        duplicate_instances = connection.execute(
            """
            SELECT request_seq, COUNT(*) AS count FROM instances
            GROUP BY request_seq HAVING count > 1
            """
        ).fetchall()
        _check(
            checks,
            "no_duplicate_instances",
            not duplicate_instances,
            [dict(row) for row in duplicate_instances],
        )
        completed_without_instance = connection.execute(
            """
            SELECT r.request_seq FROM request_state r
            LEFT JOIN instances i ON i.request_seq=r.request_seq
            WHERE r.outcome='completed' AND i.instance_id IS NULL
            ORDER BY r.request_seq
            """
        ).fetchall()
        instance_without_completed = connection.execute(
            """
            SELECT i.request_seq FROM instances i
            JOIN request_state r ON r.request_seq=i.request_seq
            WHERE r.outcome!='completed' OR r.status!='completed'
            ORDER BY i.request_seq
            """
        ).fetchall()
        _check(
            checks,
            "instance_request_consistency",
            not completed_without_instance and not instance_without_completed,
            {
                "completed_without_instance": [
                    row["request_seq"] for row in completed_without_instance
                ],
                "instance_without_completed": [
                    row["request_seq"] for row in instance_without_completed
                ],
            },
        )
        bad_policy_bindings = connection.execute(
            """
            SELECT r.request_seq
            FROM request_state r
            LEFT JOIN policy_versions p
              ON p.policy_version_id=r.policy_version_id
            WHERE p.policy_version_id IS NULL OR p.state!='committed'
            ORDER BY r.request_seq
            """
        ).fetchall()
        _check(
            checks,
            "request_policy_bindings",
            not bad_policy_bindings,
            [row["request_seq"] for row in bad_policy_bindings],
        )
        processing_count = connection.execute(
            "SELECT COUNT(*) AS count FROM request_state WHERE status='processing'"
        ).fetchone()["count"]
        _check(checks, "no_processing_requests", processing_count == 0, processing_count)
        pending_jobs = connection.execute(
            "SELECT COUNT(*) AS count FROM policy_apply_jobs WHERE status='pending'"
        ).fetchone()["count"]
        _check(checks, "no_pending_policy_jobs", pending_jobs == 0, pending_jobs)
        projection = semantic_projection(connection)
        _check(
            checks,
            "watermark_matches_instances",
            projection["watermarks"]["committed_instance_watermark"]
            == projection["watermarks"]["max_committed_instance_seq"],
            projection["watermarks"],
        )
        errors = [
            f"{check['name']}: {check['details']}"
            for check in checks
            if not check["ok"]
        ]
        return {
            "ok": not errors,
            "command": "verify",
            "checks": sorted(checks, key=lambda item: item["name"]),
            "errors": sorted(errors),
            "semantic_digest": sha256_json(projection),
            "generated_at_ms": now_ms(),
        }
    finally:
        connection.close()
