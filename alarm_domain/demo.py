"""Deterministic local demo for all three required scenarios."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .changes import load_changeset
from .diagnostics import build_diagnostic_model, render_diagnostic
from .errors import DomainError
from .logging_utils import close_json_logging, configure_json_logging, log_business_error
from .session import AlarmSession
from .snapshot import SnapshotStore, restore_snapshot
from .util import pretty_json


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _sample_paths(samples_dir: str | Path | None) -> tuple[Path, Path, Path, Path]:
    base = Path(samples_dir) if samples_dir is not None else _project_root() / "samples"
    return (
        base / "rules.json",
        base / "topology.json",
        base / "events.ndjson",
        base / "changeset-pay-severity.json",
    )


def _prepare_work_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name in (
        "demo.sqlite3",
        "demo.sqlite3-wal",
        "demo.sqlite3-shm",
        "demo-e-snap.sqlite3",
        "demo-e-snap.sqlite3-wal",
        "demo-e-snap.sqlite3-shm",
        "structured.jsonl",
        "anomaly-structured.jsonl",
        "demo-output.json",
        "diagnostic.json",
        "events.time-regression.ndjson",
    ):
        candidate = path / name
        if candidate.exists():
            candidate.unlink()


def _cluster_brief(analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "cluster_id": analysis["cluster_id"],
        "members": list(analysis["cluster"]["members"]),
        "services": list(analysis["cluster"]["services"]),
        "start_ts": analysis["cluster"]["start_ts"],
        "end_ts": analysis["cluster"]["end_ts"],
        "root_candidate": analysis["root_cause"]["selected_service"],
        "candidate_order": [
            candidate["service"] for candidate in analysis["root_cause"]["candidates"]
        ],
        "retained_alerts": [
            item["event_id"] for item in analysis["deduplicated"]["retained_alerts"]
        ],
        "folded_alerts": [
            {
                "event_id": item["event_id"],
                "service": item["service"],
                "folded_into": item["folded_into"],
            }
            for item in analysis["deduplicated"]["folded_alerts"]
        ],
    }


def _cold_summary(session: AlarmSession, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "rules_version": session.rules_version,
        "topology_version": session.topology_version,
        "window_ms": session.window_ms,
        "event_count": len(session.events),
        "event_cursor": session.event_cursor,
        "cluster_count": len(session.analyses),
        "cluster_summary_hash": session.cluster_summary_hash,
        "analysis_hash": session.analysis_hash,
        "clusters": [_cluster_brief(item) for item in session.analyses],
        "snapshot": snapshot,
    }


def _make_time_regression_file(events_path: Path, target: Path) -> tuple[Path, int]:
    lines = events_path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 4:
        raise RuntimeError("sample stream is too short for deterministic regression")
    document = json.loads(lines[3])
    previous = json.loads(lines[2])["timestamp"]
    document["timestamp"] = max(0, int(previous) - 1)
    lines[3] = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target, 4


def _run_time_anomaly(
    rules_path: Path,
    topology_path: Path,
    events_path: Path,
    work_dir: Path,
    logger: logging.Logger,
) -> dict[str, Any]:
    bad_path, line = _make_time_regression_file(
        events_path, work_dir / "events.time-regression.ndjson"
    )
    try:
        AlarmSession.from_files(rules_path, topology_path, bad_path, window_ms=500)
    except DomainError as exc:
        log_business_error(
            logger,
            exc,
            event="injected_time_regression",
            context={"expected_code": "E_TIME", "injected_line": line},
        )
        if exc.code != "E_TIME":
            raise
        return {
            "scenario": "injected-time-regression",
            "status": "rejected-as-expected",
            "code": exc.code,
            "file": exc.file,
            "line": exc.line,
            "detail": exc.detail,
        }
    raise RuntimeError("time-regression injection was not rejected")


def _run_snapshot_anomaly(
    rules_path: Path,
    topology_path: Path,
    events_path: Path,
    work_dir: Path,
    logger: logging.Logger,
) -> dict[str, Any]:
    session = AlarmSession.from_files(rules_path, topology_path, events_path, window_ms=500)
    store = SnapshotStore(work_dir / "demo-e-snap.sqlite3")
    try:
        info = store.save(
            session.snapshot_payload(),
            label="injected-missing-version",
            omit_versions=True,
        )
        try:
            restore_snapshot(store, info.snapshot_id, current_session=session)
        except DomainError as exc:
            log_business_error(
                logger,
                exc,
                event="injected_missing_snapshot_version",
                context={"expected_code": "E_SNAP", "snapshot_id": info.snapshot_id},
            )
            if exc.code != "E_SNAP":
                raise
            return {
                "scenario": "injected-missing-snapshot-version",
                "status": "rejected-as-expected",
                "code": exc.code,
                "snapshot": info.to_dict(),
                "error_file": exc.file,
                "error_line": exc.line,
                "detail": exc.detail,
            }
        raise RuntimeError("missing snapshot version injection was not rejected")
    finally:
        store.close()


def run_demo(
    *,
    samples_dir: str | Path | None = None,
    work_dir: str | Path | None = None,
    inject_time_regression: bool = False,
    inject_missing_snapshot_version: bool = False,
) -> dict[str, Any]:
    rules_path, topology_path, events_path, changeset_path = _sample_paths(samples_dir)
    output_dir = Path(work_dir) if work_dir is not None else _project_root() / "artifacts" / "demo"
    _prepare_work_dir(output_dir)
    anomaly_mode = inject_time_regression or inject_missing_snapshot_version
    log_name = "anomaly-structured.jsonl" if anomaly_mode else "structured.jsonl"
    logger = configure_json_logging(output_dir / log_name)

    if anomaly_mode:
        scenarios: list[dict[str, Any]] = []
        if inject_time_regression:
            scenarios.append(
                _run_time_anomaly(rules_path, topology_path, events_path, output_dir, logger)
            )
        if inject_missing_snapshot_version:
            scenarios.append(
                _run_snapshot_anomaly(
                    rules_path, topology_path, events_path, output_dir, logger
                )
            )
        result = {
            "schema": "alarm-demo-result-v1",
            "mode": "anomaly-injection",
            "scenarios": scenarios,
            "log_file": log_name,
        }
        (output_dir / "demo-output.json").write_text(
            pretty_json(result) + "\n", encoding="utf-8"
        )
        close_json_logging(logger)
        return result

    session = AlarmSession.from_files(
        rules_path, topology_path, events_path, window_ms=500
    )
    logger.info(
        "cold load complete",
        extra={
            "event": "cold_load",
            "context": {
                "rules_version": session.rules_version,
                "topology_version": session.topology_version,
                "clusters": len(session.analyses),
            },
        },
    )
    store = SnapshotStore(output_dir / "demo.sqlite3")
    try:
        cold_snapshot = store.save(session.snapshot_payload(), label="cold-load")
        cold_scenario = {
            "scenario": "cold-load",
            "status": "ok",
            "summary": _cold_summary(session, cold_snapshot.to_dict()),
        }

        before_roots = {
            str(item["cluster_id"]): item["root_cause"]["selected_service"]
            for item in session.analyses
        }
        changeset = load_changeset(changeset_path)
        increment = session.apply_changeset(changeset)
        after_roots = {
            str(item["cluster_id"]): item["root_cause"]["selected_service"]
            for item in session.analyses
        }
        logger.info(
            "incremental update complete",
            extra={
                "event": "incremental_update",
                "context": {
                    "changeset_id": changeset.changeset_id,
                    "invalidated_clusters": increment["invalidated_clusters"],
                    "recomputed_count": increment["recomputed_count"],
                    "reused_count": increment["reused_count"],
                },
            },
        )
        increment_snapshot = store.save(session.snapshot_payload(), label="after-increment")
        snapshots_before_restore = [info.to_dict() for info in store.list_snapshots()]
        incremental_scenario = {
            "scenario": "incremental-rule-update",
            "status": "ok",
            "summary": {
                "changeset_id": changeset.changeset_id,
                "before_roots": before_roots,
                "after_roots": after_roots,
                "invalidated_trace": increment,
                "snapshot": increment_snapshot.to_dict(),
                "clusters": [_cluster_brief(item) for item in session.analyses],
            },
        }

        restored, restore_report = restore_snapshot(
            store,
            cold_snapshot.snapshot_id,
            current_session=session,
        )
        snapshots_after_restore = [info.to_dict() for info in store.list_snapshots()]
        logger.info(
            "snapshot restore and replay complete",
            extra={
                "event": "snapshot_restore",
                "context": {
                    "snapshot_id": cold_snapshot.snapshot_id,
                    "version_diff": restore_report["version_diff"],
                    "replay_match": restore_report["replay"]["match"],
                },
            },
        )
        snapshot_scenario = {
            "scenario": "snapshot-restore-replay",
            "status": "ok",
            "summary": {
                "before_restore_snapshots": snapshots_before_restore,
                "after_restore_snapshots": snapshots_after_restore,
                "restore": restore_report,
                "restored_analysis_hash": restored.analysis_hash,
                "restored_cluster_count": len(restored.analyses),
            },
        }

        diagnostic = build_diagnostic_model(
            restored,
            increment=increment,
            restore=restore_report,
            snapshots=snapshots_after_restore,
        )
        result = {
            "schema": "alarm-demo-result-v1",
            "mode": "normal",
            "scenarios": [cold_scenario, incremental_scenario, snapshot_scenario],
            "diagnostic": diagnostic,
            "log_file": log_name,
            "snapshot_file": "demo.sqlite3",
        }
        (output_dir / "diagnostic.json").write_text(
            render_diagnostic(diagnostic) + "\n", encoding="utf-8"
        )
        (output_dir / "demo-output.json").write_text(
            pretty_json(result) + "\n", encoding="utf-8"
        )
        close_json_logging(logger)
        return result
    finally:
        store.close()


def print_result(result: dict[str, Any]) -> None:
    print(pretty_json(result))
