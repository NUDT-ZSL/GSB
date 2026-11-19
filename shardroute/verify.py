"""Read-only consistency verification for persistent state."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import Any

from .errors import canonical_json, digest
from .rules import RuleSnapshot
from .state import StateStore, UNFINISHED_STATUSES


def _issue(issues: list[dict[str, Any]], code: str, message: str, **details: Any) -> None:
    issues.append({"code": code, "details": details, "message": message})


def verify_state(state: StateStore) -> dict[str, Any]:
    connection = state.connect()
    issues: list[dict[str, Any]] = []
    try:
        connection.execute("BEGIN")
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM meta ORDER BY key")
        }
        if meta.get("schema_version") != "1":
            _issue(
                issues,
                "E_VERIFY_SCHEMA",
                "schema version is missing or unsupported",
                schema_version=meta.get("schema_version"),
            )
        pointer = connection.execute(
            "SELECT p.version, p.generation, r.digest, r.snapshot_json "
            "FROM rule_pointer p JOIN rule_versions r ON r.version = p.version "
            "WHERE p.singleton = 1"
        ).fetchone()
        if pointer is None:
            _issue(issues, "E_VERIFY_POINTER", "current rule pointer is missing")
        rule_versions = connection.execute(
            "SELECT version, digest, snapshot_json FROM rule_versions ORDER BY version"
        ).fetchall()
        rule_digests: dict[str, str] = {}
        for row in rule_versions:
            version = str(row["version"])
            try:
                snapshot = RuleSnapshot.from_dict(json.loads(str(row["snapshot_json"])))
                actual = snapshot.content_digest()
                rule_digests[version] = actual
                if actual != row["digest"]:
                    _issue(
                        issues,
                        "E_VERIFY_RULE_DIGEST",
                        "rule snapshot digest does not match",
                        actual=actual,
                        version=version,
                    )
            except Exception as exc:
                _issue(
                    issues,
                    "E_VERIFY_RULE_JSON",
                    "stored rule snapshot is invalid",
                    reason=str(exc),
                    version=version,
                )
        if pointer is not None and rule_digests.get(str(pointer["version"])) != pointer["digest"]:
            _issue(
                issues,
                "E_VERIFY_POINTER_DIGEST",
                "current pointer does not reference the verified current digest",
                version=str(pointer["version"]),
            )
        activation_rows = connection.execute(
            "SELECT version, digest, generation, clock_ms FROM rule_activations "
            "ORDER BY generation"
        ).fetchall()
        if pointer is not None:
            if not activation_rows:
                _issue(issues, "E_VERIFY_ACTIVATION", "no rule activation is recorded")
            else:
                latest = activation_rows[-1]
                if str(latest["version"]) != str(pointer["version"]):
                    _issue(
                        issues,
                        "E_VERIFY_ACTIVATION",
                        "latest activation does not match the current pointer",
                        activation_version=str(latest["version"]),
                        pointer_version=str(pointer["version"]),
                    )
                if int(latest["generation"]) != int(pointer["generation"]):
                    _issue(
                        issues,
                        "E_VERIFY_GENERATION",
                        "latest activation generation does not match the pointer",
                    )
        queries = connection.execute("SELECT * FROM queries ORDER BY query_id").fetchall()
        query_by_id = {str(row["query_id"]): row for row in queries}
        results = connection.execute(
            "SELECT * FROM query_results ORDER BY query_id"
        ).fetchall()
        result_by_id = {str(row["query_id"]): row for row in results}
        committed_results = 0
        for query_id, row in query_by_id.items():
            status = str(row["status"])
            if status not in UNFINISHED_STATUSES | {"COMMITTED", "FAILED"}:
                _issue(
                    issues,
                    "E_VERIFY_QUERY_STATUS",
                    "query has an unknown status",
                    query_id=query_id,
                    status=status,
                )
            if status == "COMMITTED":
                committed_results += 1
                result = result_by_id.get(query_id)
                if result is None:
                    _issue(
                        issues,
                        "E_VERIFY_RESULT_MISSING",
                        "committed query has no result row",
                        query_id=query_id,
                    )
                else:
                    try:
                        document = json.loads(str(result["result_json"]))
                        stored_digest = str(result["result_digest"])
                        if document.get("result_digest") != stored_digest:
                            _issue(
                                issues,
                                "E_VERIFY_RESULT_DIGEST",
                                "result document digest field differs from result row",
                                query_id=query_id,
                            )
                        material = dict(document)
                        material.pop("result_digest", None)
                        actual_digest = digest(material)
                        if actual_digest != stored_digest:
                            _issue(
                                issues,
                                "E_VERIFY_RESULT_DIGEST",
                                "result payload digest does not match",
                                actual=actual_digest,
                                expected=stored_digest,
                                query_id=query_id,
                            )
                        if document.get("rule_version") != row["rule_version"]:
                            _issue(
                                issues,
                                "E_VERIFY_RESULT_VERSION",
                                "result rule version differs from query audit",
                                query_id=query_id,
                            )
                        if document.get("query_key") != row["query_key"]:
                            _issue(
                                issues,
                                "E_VERIFY_QUERY_KEY",
                                "result query key differs from query audit",
                                query_id=query_id,
                            )
                        if row["result_digest"] != stored_digest:
                            _issue(
                                issues,
                                "E_VERIFY_QUERY_RESULT_DIGEST",
                                "query audit result digest differs from committed result",
                                query_id=query_id,
                            )
                        if row["result_json"] is not None:
                            try:
                                if json.loads(str(row["result_json"])) != document:
                                    _issue(
                                        issues,
                                        "E_VERIFY_QUERY_RESULT_JSON",
                                        "query audit result JSON differs from committed result",
                                        query_id=query_id,
                                    )
                            except json.JSONDecodeError:
                                _issue(
                                    issues,
                                    "E_VERIFY_QUERY_RESULT_JSON",
                                    "query audit result JSON is invalid",
                                    query_id=query_id,
                                )
                        if result["query_key"] != row["query_key"]:
                            _issue(
                                issues,
                                "E_VERIFY_RESULT_QUERY_KEY",
                                "result row query key differs from query audit",
                                query_id=query_id,
                            )
                        if result["rule_version"] != row["rule_version"]:
                            _issue(
                                issues,
                                "E_VERIFY_RESULT_RULE_VERSION",
                                "result row rule version differs from query audit",
                                query_id=query_id,
                            )
                    except (json.JSONDecodeError, TypeError) as exc:
                        _issue(
                            issues,
                            "E_VERIFY_RESULT_JSON",
                            "committed result JSON is invalid",
                            query_id=query_id,
                            reason=str(exc),
                        )
            elif status != "FAILED":
                if query_id in result_by_id:
                    _issue(
                        issues,
                        "E_VERIFY_PREMATURE_RESULT",
                        "unfinished query has a committed result row",
                        query_id=query_id,
                    )
            if status == "FAILED" and row["error_json"] is None:
                _issue(
                    issues,
                    "E_VERIFY_FAILURE_DETAIL",
                    "failed query has no diagnostic error",
                    query_id=query_id,
                )
        for query_id, row in result_by_id.items():
            query = query_by_id.get(query_id)
            if query is None:
                _issue(
                    issues,
                    "E_VERIFY_ORPHAN_RESULT",
                    "result row has no query audit",
                    query_id=query_id,
                )
            elif query["status"] != "COMMITTED":
                _issue(
                    issues,
                    "E_VERIFY_ORPHAN_RESULT",
                    "result row belongs to a non-committed query",
                    query_id=query_id,
                    status=query["status"],
                )
        outcomes = connection.execute(
            "SELECT query_id, shard_id, status, payload_json FROM shard_outcomes "
            "ORDER BY query_id, shard_id"
        ).fetchall()
        for row in outcomes:
            query_id = str(row["query_id"])
            query = query_by_id.get(query_id)
            if query is None:
                _issue(
                    issues,
                    "E_VERIFY_ORPHAN_OUTCOME",
                    "shard outcome refers to an unknown query",
                    query_id=query_id,
                    shard_id=int(row["shard_id"]),
                )
                continue
            if str(query["status"]) == "COMMITTED":
                try:
                    payload = json.loads(str(row["payload_json"]))
                    if payload.get("result_digest") is not None:
                        result_material = payload.get("result")
                        if digest(result_material) != payload["result_digest"]:
                            _issue(
                                issues,
                                "E_VERIFY_OUTCOME_DIGEST",
                                "shard result digest does not match its payload",
                                query_id=query_id,
                                shard_id=int(row["shard_id"]),
                            )
                except (json.JSONDecodeError, TypeError) as exc:
                    _issue(
                        issues,
                        "E_VERIFY_OUTCOME_JSON",
                        "shard outcome JSON is invalid",
                        query_id=query_id,
                        reason=str(exc),
                    )
        staging = connection.execute(
            "SELECT query_id, result_digest, payload_json FROM query_staging ORDER BY query_id"
        ).fetchall()
        for row in staging:
            query_id = str(row["query_id"])
            query = query_by_id.get(query_id)
            if query is None:
                _issue(
                    issues,
                    "E_VERIFY_ORPHAN_STAGING",
                    "staging row refers to an unknown query",
                    query_id=query_id,
                )
            elif str(query["status"]) not in UNFINISHED_STATUSES:
                _issue(
                    issues,
                    "E_VERIFY_STALE_STAGING",
                    "terminal query still has a staging row",
                    query_id=query_id,
                    status=str(query["status"]),
                )
            try:
                payload = json.loads(str(row["payload_json"]))
                material = dict(payload)
                material.pop("result_digest", None)
                if digest(material) != row["result_digest"]:
                    _issue(
                        issues,
                        "E_VERIFY_STAGING_DIGEST",
                        "staging result digest does not match its payload",
                        query_id=query_id,
                    )
            except (json.JSONDecodeError, TypeError):
                _issue(
                    issues,
                    "E_VERIFY_STAGING_JSON",
                    "staging payload JSON is invalid",
                    query_id=query_id,
                )
        audit_count = int(
            connection.execute("SELECT COUNT(*) AS count FROM audit_log").fetchone()["count"]
        )
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        _issue(issues, "E_VERIFY_SQLITE", "state database query failed", reason=str(exc))
        meta = {}
        pointer = None
        activation_rows = []
        queries = []
        results = []
        rule_digests = {}
        outcomes = []
        staging = []
        audit_count = 0
    finally:
        connection.close()
    issues.sort(key=lambda item: (item["code"], canonical_json(item)))
    result_records = [
        {
            "query_id": str(row["query_id"]),
            "result_digest": str(row["result_digest"]),
        }
        for row in results
    ]
    state_digest = digest(
        {
            "current_rule_version": str(pointer["version"]) if pointer is not None else None,
            "generation": int(pointer["generation"]) if pointer is not None else None,
            "query_results": result_records,
            "rule_digests": dict(sorted(rule_digests.items())),
        }
    )
    checks = {
        "audit_records": audit_count,
        "activation_records": len(activation_rows),
        "committed_query_audits": sum(
            1 for row in queries if str(row["status"]) == "COMMITTED"
        ),
        "committed_results": len(results),
        "current_rule_version": str(pointer["version"]) if pointer is not None else None,
        "loaded_rule_versions": len(rule_digests),
        "queries": len(queries),
        "shard_outcomes": len(outcomes),
        "staging_records": len(staging),
        "unfinished_queries": sum(
            1 for row in queries if str(row["status"]) in UNFINISHED_STATUSES
        ),
    }
    return {
        "checks": checks,
        "issues": issues,
        "ok": not issues,
        "state_digest": state_digest,
    }

