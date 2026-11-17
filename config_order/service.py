"""Application services shared by the CLI and tests."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .canonical import sha256_canonical
from .engine import ALGORITHM, diagnose_scenario, input_fingerprint, rule_fingerprint
from .errors import E_CHAIN, E_ENV, E_REPLAY, Issue, LoadRejected
from .loader import load_scenario, validate_scenario
from .logging_utils import log_issues, log_result_issues
from .replay import compare_results
from .repository import Repository, make_session_id


def diagnosis_parameters(injection: str) -> dict[str, Any]:
    return {
        "injection": injection,
        "ordering_algorithm": ALGORITHM,
        "chain_integrity_policy": "strict",
        "replay_policy": "snapshot-authoritative-on-version-mismatch",
    }


def run_diagnose(
    input_path: str | Path,
    db_path: str | Path,
    log_path: str | Path,
    injection: str = "none",
) -> tuple[dict[str, Any], int]:
    parameters = diagnosis_parameters(injection)
    try:
        scenario = load_scenario(input_path, injection=injection)
    except LoadRejected as exc:
        report = exc.as_report(parameters)
        report["input_path"] = str(input_path)
        log_issues(
            log_path,
            phase="load",
            business_code=exc.business_code,
            issues=exc.issues,
            extra={"input_path": str(input_path), "injection": injection},
        )
        return report, 2
    except Exception as exc:
        issue = Issue(
            code=E_ENV,
            field_path="$",
            message=f"environment loading failed: {exc}",
        )
        report = {
            "schema_version": "1.0",
            "status": "REJECTED",
            "business_code": E_ENV,
            "diagnosis_parameters": parameters,
            "input_path": str(input_path),
            "errors": [issue.as_dict()],
        }
        log_issues(
            log_path,
            phase="load",
            business_code=E_ENV,
            issues=[issue],
            extra={"input_path": str(input_path), "injection": injection},
        )
        return report, 2

    rule_fp = rule_fingerprint(scenario)
    repo = Repository(db_path)
    try:
        rule_version, _created = repo.get_or_create_rule_version(
            rule_fp, None
        )
        session_id = make_session_id(scenario, parameters, rule_version)
        result = diagnose_scenario(scenario)
        result["session_id"] = session_id
        result["rule_version"] = rule_version
        result["rule_fingerprint"] = rule_fp
        result["diagnosis_parameters"] = parameters
        result["input_fingerprint"] = input_fingerprint(scenario)
        result = repo.persist_diagnosis(
            scenario,
            result,
            parameters,
            rule_version,
            rule_fp,
        )
    finally:
        repo.close()

    log_result_issues(log_path, result, phase="diagnose")
    exit_code = 3 if result.get("overall_code") == E_CHAIN else 0
    return result, exit_code


def _snapshot_load_failure(
    session_id: str,
    snapshot: dict[str, Any] | None,
    message: str,
    source: str | None = None,
) -> tuple[dict[str, Any], int]:
    environment_id = None
    config_key = None
    rule_version = None
    if snapshot is not None:
        rule_version = snapshot.get("rule_version")
        environments = snapshot.get("scenario", {}).get("environments", [])
        if environments:
            environment_id = environments[0].get("id")
    issue = Issue(
        code=E_REPLAY,
        field_path="snapshot",
        message=message,
        environment_id=environment_id,
        config_key=config_key,
        source_id=source,
        detail={"rule_version": rule_version},
    )
    return (
        {
            "schema_version": "1.0",
            "status": "REPLAY_FAILED",
            "business_code": E_REPLAY,
            "session_id": session_id,
            "replay": {
                "status": "FAIL",
                "mismatches": [issue.as_dict()],
            },
            "errors": [issue.as_dict()],
        },
        4,
    )


def run_restore(
    db_path: str | Path,
    session_id: str,
    log_path: str | Path,
) -> tuple[dict[str, Any], int]:
    repo = Repository(db_path)
    try:
        loaded = repo.load_session(session_id)
        latest_rule_version = repo.latest_rule_version()
        if loaded is None:
            report, exit_code = _snapshot_load_failure(
                session_id, None, "session snapshot not found"
            )
            log_issues(
                log_path,
                phase="restore",
                business_code=E_REPLAY,
                issues=[
                    Issue(
                        code=E_REPLAY,
                        field_path="session_id",
                        message="session snapshot not found",
                    )
                ],
                session_id=session_id,
            )
            return report, exit_code

        snapshot = loaded["snapshot"]
        try:
            scenario = validate_scenario(copy.deepcopy(snapshot.get("scenario")))
        except LoadRejected as exc:
            report, exit_code = _snapshot_load_failure(
                session_id,
                snapshot,
                f"snapshot input no longer validates as {exc.business_code}",
                source=exc.issues[0].source_id if exc.issues else None,
            )
            log_issues(
                log_path,
                phase="restore",
                business_code=E_REPLAY,
                issues=[
                    Issue(
                        code=E_REPLAY,
                        field_path=issue.field_path,
                        message=issue.message,
                        environment_id=issue.environment_id,
                        config_key=issue.config_key,
                        source_id=issue.source_id,
                        detail=issue.detail,
                    )
                    for issue in exc.issues
                ],
                session_id=session_id,
                extra={"snapshot_validation_code": exc.business_code},
            )
            return report, exit_code

        recomputed = diagnose_scenario(scenario)
        mismatches = compare_results(snapshot.get("result", {}), recomputed)
        snapshot_rule_version = int(snapshot.get("rule_version", loaded["rule_version"]))
        version_mismatch = snapshot_rule_version != latest_rule_version
        base_report = {
            "schema_version": "1.0",
            "environment_set_version": scenario["environment_set_version"],
            "session_id": session_id,
            "snapshot_rule_version": snapshot_rule_version,
            "latest_rule_version": latest_rule_version,
            "version_mismatch": version_mismatch,
            "replay": {
                "status": "PASS" if not mismatches else "FAIL",
                "checked_keys": recomputed["statistics"]["keys"],
                "mismatch_count": len(mismatches),
                "mismatches": [issue.as_dict() for issue in mismatches],
            },
        }
        if mismatches:
            repo.set_replay_status(session_id, "FAIL")
            base_report["status"] = "REPLAY_FAILED"
            base_report["business_code"] = E_REPLAY
            base_report["errors"] = [issue.as_dict() for issue in mismatches]
            log_issues(
                log_path,
                phase="restore",
                business_code=E_REPLAY,
                issues=mismatches,
                session_id=session_id,
            )
            return base_report, 4

        if version_mismatch:
            replacement = repo.restore_current_view(
                session_id, scenario, recomputed, snapshot_rule_version
            )
            replacement["reason"] = "RULE_VERSION_MISMATCH"
        else:
            replacement = {
                "strategy": "no-op",
                "reason": "RULE_VERSION_MATCH",
                "environment_set_version": scenario["environment_set_version"],
                "restored_rule_version": snapshot_rule_version,
                "changes": [],
                "changed_key_count": 0,
                "current_statistics": repo.current_statistics(
                    scenario["environment_set_version"]
                ),
            }
            repo.set_replay_status(session_id, "PASS")

        base_report["status"] = "RESTORED"
        base_report["business_code"] = "OK"
        base_report["replacement"] = replacement
        return base_report, 0
    finally:
        repo.close()


def run_list_sessions(db_path: str | Path) -> dict[str, Any]:
    repo = Repository(db_path)
    try:
        sessions = repo.list_sessions()
        return {
            "schema_version": "1.0",
            "status": "OK",
            "sessions": sessions,
        }
    finally:
        repo.close()
