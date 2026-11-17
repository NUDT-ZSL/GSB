"""Independent read-only verifier for persisted diagnosis sessions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest_without_field(chain: dict[str, Any]) -> str:
    payload = {key: value for key, value in chain.items() if key != "digest"}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _env_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        environment["id"]: environment
        for environment in snapshot["scenario"]["environments"]
    }


def verify_session(
    db_path: str | Path, session_id: str | None = None
) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {
            "status": "FAIL",
            "session_id": session_id,
            "summary": {"checks": 0, "errors": 1},
            "errors": [{"check": "database", "message": "database not found"}],
        }
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        if session_id is None:
            row = connection.execute(
                "SELECT session_id FROM sessions ORDER BY created_at DESC, session_id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {
                    "status": "FAIL",
                    "session_id": None,
                    "summary": {"checks": 0, "errors": 1},
                    "errors": [{"check": "session", "message": "no session found"}],
                }
            session_id = row["session_id"]

        session = connection.execute(
            "SELECT snapshot_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            return {
                "status": "FAIL",
                "session_id": session_id,
                "summary": {"checks": 0, "errors": 1},
                "errors": [{"check": "session", "message": "session not found"}],
            }

        snapshot = json.loads(session["snapshot_json"])
        saved_result = snapshot["result"]
        environments = _env_map(snapshot)
        chain_rows = {
            (row["environment_id"], row["config_key"]): row
            for row in connection.execute(
                "SELECT * FROM session_chains WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        }
        decision_rows = {
            (row["environment_id"], row["config_key"]): row
            for row in connection.execute(
                "SELECT * FROM session_decisions WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        }

        errors: list[dict[str, Any]] = []
        checks = 0
        checked_keys = 0

        for environment_report in sorted(
            saved_result["environments"],
            key=lambda item: item["environment_id"],
        ):
            env_id = environment_report["environment_id"]
            environment = environments[env_id]
            bindings = environment.get("bindings", {})
            for key_report in sorted(
                environment_report["keys"], key=lambda item: item["key"]
            ):
                key = key_report["key"]
                checked_keys += 1
                chain_row = chain_rows.get((env_id, key))
                decision_row = decision_rows.get((env_id, key))
                if chain_row is None:
                    errors.append(
                        {
                            "check": "chain_row",
                            "environment_id": env_id,
                            "key": key,
                            "message": "missing persisted order chain",
                        }
                    )
                    continue
                if decision_row is None:
                    errors.append(
                        {
                            "check": "decision_row",
                            "environment_id": env_id,
                            "key": key,
                            "message": "missing persisted decision",
                        }
                    )
                    continue
                chain = json.loads(chain_row["chain_json"])
                coverage = json.loads(decision_row["coverage_json"])
                effective_value = json.loads(decision_row["effective_value_json"])
                trace = json.loads(decision_row["trace_json"])
                saved_decision = key_report["decision"]
                checks += 1
                if chain.get("digest") != chain_row["digest"]:
                    errors.append(
                        {
                            "check": "chain_digest_field",
                            "environment_id": env_id,
                            "key": key,
                            "source": None,
                            "message": "chain digest field differs from relational digest",
                        }
                    )
                for field_name, row_value, saved_value in (
                    ("status", decision_row["status"], key_report.get("status")),
                    ("code", decision_row["code"], key_report.get("code")),
                    (
                        "selected_source",
                        decision_row["selected_source"],
                        saved_decision.get("selected_source"),
                    ),
                ):
                    checks += 1
                    if row_value != saved_value:
                        errors.append(
                            {
                                "check": f"decision_{field_name}",
                                "environment_id": env_id,
                                "key": key,
                                "source": decision_row["selected_source"],
                                "expected": saved_value,
                                "actual": row_value,
                                "message": "relational decision differs from snapshot decision",
                            }
                        )
                checks += 1
                if _canonical(effective_value) != _canonical(
                    saved_decision.get("effective_value")
                ):
                    errors.append(
                        {
                            "check": "decision_effective_value",
                            "environment_id": env_id,
                            "key": key,
                            "source": decision_row["selected_source"],
                            "message": "relational effective value differs from snapshot",
                        }
                    )
                checks += 1
                if _canonical(coverage) != _canonical(
                    saved_decision.get("coverage")
                ):
                    errors.append(
                        {
                            "check": "decision_coverage",
                            "environment_id": env_id,
                            "key": key,
                            "source": decision_row["selected_source"],
                            "message": "relational coverage differs from snapshot",
                        }
                    )
                checks += 1
                if _canonical(trace) != _canonical(saved_decision.get("trace")):
                    errors.append(
                        {
                            "check": "decision_trace",
                            "environment_id": env_id,
                            "key": key,
                            "source": decision_row["selected_source"],
                            "message": "relational trace differs from snapshot",
                        }
                    )
                recomputed_digest = _digest_without_field(chain)
                checks += 1
                if recomputed_digest != chain_row["digest"]:
                    errors.append(
                        {
                            "check": "chain_digest_recompute",
                            "environment_id": env_id,
                            "key": key,
                            "source": None,
                            "expected": recomputed_digest,
                            "actual": chain_row["digest"],
                            "message": "canonical chain digest does not match",
                        }
                    )

                if decision_row["status"] == "RESOLVED":
                    steps = chain.get("steps", [])
                    binding = bindings.get(key, {"require": [], "forbid": []})
                    require = binding["require"][0] if binding["require"] else None
                    forbid = set(binding["forbid"])
                    eligible = [
                        step
                        for step in steps
                        if step["source"] not in forbid
                        and (require is None or step["source"] == require)
                    ]
                    expected_selected = eligible[0]["source"] if eligible else None
                    checks += 1
                    if decision_row["selected_source"] != expected_selected:
                        errors.append(
                            {
                                "check": "selected_source",
                                "environment_id": env_id,
                                "key": key,
                                "source": decision_row["selected_source"],
                                "expected": expected_selected,
                                "message": "selected source is not first binding-eligible source",
                            }
                        )
                    selected_step = next(
                        (
                            step
                            for step in steps
                            if step["source"] == decision_row["selected_source"]
                        ),
                        None,
                    )
                    checks += 1
                    if selected_step is None:
                        errors.append(
                            {
                                "check": "selected_step",
                                "environment_id": env_id,
                                "key": key,
                                "source": decision_row["selected_source"],
                                "message": "selected source is absent from order chain",
                            }
                        )
                    elif _canonical(selected_step.get("value")) != _canonical(
                        effective_value
                    ):
                        errors.append(
                            {
                                "check": "effective_value",
                                "environment_id": env_id,
                                "key": key,
                                "source": decision_row["selected_source"],
                                "message": "effective value differs from selected chain step",
                            }
                        )

                    covered_sources = sorted(
                        item["source"] for item in coverage
                    )
                    expected_covered = sorted(
                        step["source"]
                        for step in steps
                        if step["source"] != decision_row["selected_source"]
                    )
                    checks += 1
                    if covered_sources != expected_covered:
                        errors.append(
                            {
                                "check": "coverage_set",
                                "environment_id": env_id,
                                "key": key,
                                "source": decision_row["selected_source"],
                                "expected": expected_covered,
                                "actual": covered_sources,
                                "message": "coverage does not account for every non-selected source",
                            }
                        )
                    for item in coverage:
                        expected_reason = (
                            "BINDING_FORBIDDEN"
                            if item["source"] in forbid
                            else "LOWER_PRIORITY"
                        )
                        checks += 1
                        if item.get("reason") != expected_reason:
                            errors.append(
                                {
                                    "check": "coverage_reason",
                                    "environment_id": env_id,
                                    "key": key,
                                    "source": item["source"],
                                    "expected": expected_reason,
                                    "actual": item.get("reason"),
                                    "message": "coverage reason is inconsistent with binding",
                                }
                            )
                else:
                    checks += 1
                    if (
                        decision_row["selected_source"] is not None
                        or effective_value is not None
                        or coverage
                    ):
                        errors.append(
                            {
                                "check": "error_convergence",
                                "environment_id": env_id,
                                "key": key,
                                "source": decision_row["selected_source"],
                                "message": "error row contains a convergence result",
                            }
                        )

        expected_rows = {
            (environment_report["environment_id"], key_report["key"])
            for environment_report in saved_result.get("environments", [])
            for key_report in environment_report.get("keys", [])
        }
        for row_kind, rows in (
            ("chain", chain_rows),
            ("decision", decision_rows),
        ):
            for environment_id, key in sorted(set(rows) - expected_rows):
                errors.append(
                    {
                        "check": f"orphan_{row_kind}_row",
                        "environment_id": environment_id,
                        "key": key,
                        "message": "persisted row has no corresponding snapshot key",
                    }
                )

        return {
            "schema_version": "1.0",
            "status": "PASS" if not errors else "FAIL",
            "session_id": session_id,
            "environment_set_version": saved_result.get(
                "environment_set_version"
            ),
            "rule_version": snapshot.get("rule_version"),
            "summary": {
                "environments": len(saved_result.get("environments", [])),
                "keys": checked_keys,
                "checks": checks,
                "errors": len(errors),
            },
            "errors": sorted(
                errors,
                key=lambda item: (
                    item.get("environment_id") or "",
                    item.get("key") or "",
                    item.get("check") or "",
                    item.get("source") or "",
                ),
            ),
        }
    finally:
        connection.close()
