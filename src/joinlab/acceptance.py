"""End-to-end deterministic acceptance harness for the local fixture."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from .catalog import load_catalog
from .cost import CostParams
from .errors import JoinLabError, PlanError, StaleVersionError
from .injection import create_injection
from .logging_utils import BusinessLogger
from .queries import load_query_file
from .report import SESSION_RECORD_FORMAT, build_report, write_report
from .session import ExperimentSession, SessionVersions
from .snapshot import restore_snapshot, save_snapshot
from .typesys import json_dumps_stable


def _operator_stats_without_timing(evaluation) -> list[dict[str, Any]]:
    return [dict(stat) for stat in evaluation.execution.selected.stats]


def _query_record(evaluation) -> dict[str, Any]:
    selected = evaluation.selected_plan
    selected_dict = selected.to_dict(evaluation.plans.params, selected=True)
    return {
        "name": evaluation.name,
        "query_id": evaluation.query.query_id,
        "canonical_sql": evaluation.query.canonical_sql,
        "candidate_count": len(evaluation.plans.candidates),
        "selected_plan": {
            "plan_id": selected.plan_id,
            "plan_signature": selected.plan_signature,
            "relation_order": list(selected.relation_order),
            "operators": list(selected.operators),
            "quantized_cost": selected_dict["quantized_cost"],
            "total_cost": selected_dict["total_cost"],
            "final_rows_est": selected_dict["final_rows_est"],
        },
        "row_count": evaluation.execution.selected.row_count,
        "result_digest": evaluation.execution.selected.digest,
        "stats_fingerprint": evaluation.execution.selected.stats_fingerprint,
        "cross_path": evaluation.execution.consistency.to_dict(),
        "operator_stats": _operator_stats_without_timing(evaluation),
        "attribution_chain": list(evaluation.execution.selected.attribution_chain),
        "versions": evaluation.versions.to_dict(),
        "sample_scale": evaluation.sample_scale,
    }


def run_acceptance(project_root: str | Path, output_dir: str | Path) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    logs_dir = output / "logs"
    logger = BusinessLogger(logs_dir / "business.jsonl")

    catalog = load_catalog(root / "samples" / "catalog.json")
    named_queries = load_query_file(root / "samples" / "queries.json", catalog)
    params = CostParams()
    session = ExperimentSession(catalog, params=params, sample_scale=1.0)
    evaluations = [session.evaluate(item.sql, name=item.name) for item in named_queries]

    if any(not evaluation.execution.consistency.consistent for evaluation in evaluations):
        raise PlanError("acceptance fixture failed cross-plan byte consistency", "acceptance.consistency")

    # A deliberate burst of parameter edits.  The old ticket must be rejected,
    # while the latest ticket must produce the only publishable result.
    refresh_session = ExperimentSession(catalog, params=CostParams(), sample_scale=1.0)
    initial_versions = refresh_session.versions
    version_sequence: list[dict[str, int]] = []
    for index in range(1, 13):
        updated = refresh_session.update_cost_params(
            {
                "nested_loop_pair_cost": round(0.20 + index * 0.005, 6),
                "cost_quantum": 0.01,
            }
        )
        version_sequence.append(updated.to_dict())
    final_versions = refresh_session.versions
    monotonic = all(
        version_sequence[index]["param_version"] < version_sequence[index + 1]["param_version"]
        for index in range(len(version_sequence) - 1)
    )
    stale_rejected = False
    stale_error: dict[str, Any] | None = None
    try:
        refresh_session.evaluate(named_queries[0].sql, expected_versions=initial_versions)
    except StaleVersionError as exc:
        stale_rejected = True
        stale_error = exc.to_dict()
    latest_eval = refresh_session.evaluate(named_queries[0].sql, expected_versions=final_versions)
    refresh_converged = (
        stale_rejected
        and monotonic
        and latest_eval.versions == final_versions
        and refresh_session.versions == final_versions
        and refresh_session.is_current(latest_eval.versions)
    )
    if not refresh_converged:
        raise PlanError(
            "rapid refresh did not converge to the latest version",
            "acceptance.refresh",
            stale_rejected=stale_rejected,
            monotonic=monotonic,
            final_versions=final_versions.to_dict(),
            latest_versions=latest_eval.versions.to_dict(),
        )

    snapshot_path = output / "snapshot.json"
    save_result = save_snapshot(session, snapshot_path)
    saved_versions = session.versions
    # Make the live session intentionally different so restore must replace all
    # data-view/parameter state rather than merge incrementally.
    session.update_cost_params({"nested_loop_pair_cost": 0.91})
    session.update_sample_scale(0.75)
    mutated_versions = session.versions
    restore = restore_snapshot(snapshot_path, current_session=session)
    restored = restore.session
    restored_latest = {entry.query.query_id: entry for entry in restored.latest_evaluations()}
    replay_matches = []
    for original in evaluations:
        replay = restored_latest.get(original.query.query_id)
        replay_matches.append(
            replay is not None
            and replay.selected_plan.plan_id == original.selected_plan.plan_id
            and replay.execution.selected.digest == original.execution.selected.digest
            and replay.execution.selected.stats_fingerprint == original.execution.selected.stats_fingerprint
        )
    snapshot_restored = (
        restore.mode == "full_replace"
        and restore.version_comparison.get("same_versions") is False
        and restore.replayed_queries == len(evaluations)
        and all(replay_matches)
    )
    if not snapshot_restored:
        raise PlanError(
            "snapshot full-replacement replay did not reproduce the recorded session",
            "acceptance.snapshot",
            restore=restore.to_dict(),
            replay_matches=replay_matches,
        )

    injected_dir = output / "injected"
    anomalies: list[dict[str, Any]] = []
    for kind in ("skew", "type", "snapshot-version"):
        artifact = create_injection(
            kind,
            root / "samples",
            injected_dir / kind,
            baseline_snapshot=snapshot_path if kind == "snapshot-version" else None,
        )
        anomalies.append(artifact.to_dict())
        logger.write(
            "injection_detected",
            {
                "operation": "acceptance",
                "code": artifact.detected_code,
                "expected_code": artifact.expected_code,
                "path": artifact.detected_path,
                "injection_kind": kind,
                "input_only": True,
            },
        )

    verification = {
        "refresh_converged": refresh_converged,
        "final_param_version": final_versions.param_version,
        "refresh": {
            "initial_versions": initial_versions.to_dict(),
            "version_sequence": version_sequence,
            "monotonic_param_versions": monotonic,
            "stale_result_rejected": stale_rejected,
            "stale_error": stale_error,
            "accepted_versions": latest_eval.versions.to_dict(),
            "session_versions_after_burst": refresh_session.versions.to_dict(),
            "converged": refresh_converged,
        },
        "snapshot_restored": snapshot_restored,
        "snapshot": {
            "save_versions": save_result["versions"],
            "mutated_live_versions_before_restore": mutated_versions.to_dict(),
            "restore": {
                key: value
                for key, value in restore.to_dict().items()
                if key not in {"snapshot_path"}
            },
            "replay_matches": replay_matches,
        },
    }
    record = {
        "format": SESSION_RECORD_FORMAT,
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
        },
        "catalog": {
            "name": catalog.name,
            "schema_version": catalog.schema_version,
            "data_version": catalog.data_version,
            "tables": [
                {"name": table.name, "row_count": table.row_count, "skew": table.skew}
                for table in catalog.tables
            ],
        },
        "versions": saved_versions.to_dict(),
        "sample_scale": 1.0,
        "cost_params": params.to_dict(),
        "queries": [_query_record(evaluation) for evaluation in evaluations],
        "verification": verification,
        "anomalies": anomalies,
    }
    record_path = output / "session_record.json"
    record_path.write_text(json_dumps_stable(record, pretty=True), encoding="utf-8")
    report = build_report(record)
    report_path = output / "acceptance_report.json"
    write_report(report, report_path)
    return {
        "ok": True,
        "report_path": str(report_path),
        "session_record_path": str(record_path),
        "snapshot_path": str(snapshot_path),
        "report": report,
    }
