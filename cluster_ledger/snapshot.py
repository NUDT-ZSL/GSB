"""Atomic session snapshots, recovery, and deterministic replay."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .clock import (
    CLIENT_CLOCK_BASIS,
    LATENCY_CALIBER_VERSION,
    SERVER_CLOCK_BASIS,
    mono_ms,
)
from .config import Declaration, parse_declaration
from .deterministic import (
    AggregationDataError,
    canonical_json,
    compute_aggregate,
    digest_text,
    first_difference,
    task_result_digest,
)
from .errors import E_LEDGER, E_REPLAY, E_VERSION, BusinessError
from .ledger import Ledger, result_checksum
from .logging_utils import EventLogger
from .metrics import compute_latency_stats


SNAPSHOT_SCHEMA_VERSION = 1


def _snapshot_checksum(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "snapshot_checksum"}
    return "sha256:" + digest_text(canonical_json(body))


def _task_digests(declaration: Declaration, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cluster_versions = {cluster.cluster_id: cluster.config_version for cluster in declaration.clusters}
    result_index = {
        (row["task_id"], row["cluster_id"]): row
        for row in results
        if not row.get("is_stale", False)
        and row["config_version"] == cluster_versions.get(row["cluster_id"])
    }
    report: list[dict[str, Any]] = []
    for task in declaration.tasks:
        selected = [
            result_index[(task.task_id, cluster_id)]
            for cluster_id in task.target_clusters
            if (task.task_id, cluster_id) in result_index
        ]
        complete = len(selected) == len(task.target_clusters)
        report.append(
            {
                "task_id": task.task_id,
                "expected_clusters": list(task.target_clusters),
                "complete": complete,
                "aggregate_digest": task_result_digest(selected) if complete else None,
            }
        )
    return report


def _completed_and_incomplete(declaration: Declaration,
                              results: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    cluster_versions = {cluster.cluster_id: cluster.config_version for cluster in declaration.clusters}
    complete_keys = {
        (row["task_id"], row["cluster_id"])
        for row in results
        if not row.get("is_stale", False)
        and row["config_version"] == cluster_versions.get(row["cluster_id"])
    }
    incomplete = []
    for task in declaration.tasks:
        for cluster_id in task.target_clusters:
            if (task.task_id, cluster_id) not in complete_keys:
                incomplete.append(
                    {
                        "task_id": task.task_id,
                        "cluster_id": cluster_id,
                        "config_version": cluster_versions[cluster_id],
                        "status": "unknown",
                        "rerun_required": True,
                    }
                )
    completed_clusters = []
    for cluster in declaration.clusters:
        cluster_tasks = [task for task in declaration.tasks if cluster.cluster_id in task.target_clusters]
        if all((task.task_id, cluster.cluster_id) in complete_keys for task in cluster_tasks):
            completed_clusters.append(cluster.cluster_id)
    return sorted(completed_clusters), incomplete


def build_snapshot(declaration: Declaration, ledger: Ledger) -> dict[str, Any]:
    state = ledger.read_consistent_state()
    interactions = ledger.fetch_interactions()
    completed, incomplete = _completed_and_incomplete(declaration, state["results"])
    payload: dict[str, Any] = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "app_version": __version__,
        "created_mono_ms": mono_ms(),
        "clock_basis": {
            "client": CLIENT_CLOCK_BASIS,
            "server": SERVER_CLOCK_BASIS,
            "persisted_interaction_times": "integer milliseconds from the originating process monotonic clock",
        },
        "termination_state": "snapshot-only; no clean-exit report is implied",
        "declaration_version": declaration.declaration_version,
        "declaration_fingerprint": declaration.fingerprint,
        "declaration": declaration.to_dict(),
        "tasks": [task.to_dict() for task in declaration.tasks],
        "clusters": [cluster.to_dict() for cluster in declaration.clusters],
        "ledger_revision": state["revision"],
        "results": state["results"],
        "interactions": interactions,
        "declaration_state": state["declaration_state"],
        "transitions": state["transitions"],
        "recovery_state": state["recovery"],
        "latency_caliber_version": ledger.latency_caliber_version(),
        "latency_stats": compute_latency_stats(interactions),
        "completed_clusters": completed,
        "incomplete_units": incomplete,
        "task_digests": _task_digests(declaration, state["results"]),
    }
    payload["snapshot_checksum"] = _snapshot_checksum(payload)
    return payload


def save_snapshot(path: str | Path, declaration: Declaration, ledger: Ledger,
                  logger: EventLogger | None = None) -> dict[str, Any]:
    snap = build_snapshot(declaration, ledger)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + f".tmp-{os.getpid()}")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(snap, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)
    try:
        directory_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass
    if logger:
        logger.emit(
            "snapshot_saved",
            path=str(target),
            declaration_version=declaration.declaration_version,
            ledger_revision=snap["ledger_revision"],
            completed_clusters=snap["completed_clusters"],
            incomplete_unit_count=len(snap["incomplete_units"]),
        )
    return snap


def load_snapshot(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as handle:
            snap = json.load(handle)
    except FileNotFoundError as exc:
        raise BusinessError(
            E_LEDGER, "snapshot file not found", details={"path": str(target)}
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_LEDGER, f"snapshot cannot be read: {exc}", details={"path": str(target)}
        ) from exc
    if not isinstance(snap, dict):
        raise BusinessError(E_LEDGER, "snapshot root must be an object", details={"path": str(target)})
    if snap.get("snapshot_schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise BusinessError(
            E_LEDGER,
            "unsupported snapshot schema version",
            details={"expected": SNAPSHOT_SCHEMA_VERSION, "actual": snap.get("snapshot_schema_version")},
        )
    checksum = snap.get("snapshot_checksum")
    if checksum != _snapshot_checksum(snap):
        raise BusinessError(
            E_LEDGER,
            "snapshot checksum mismatch",
            details={"expected": _snapshot_checksum(snap), "actual": checksum},
        )
    return snap


def open_snapshot(path: str | Path, ledger: Ledger, *,
                  current_declaration: Declaration | None = None,
                  logger: EventLogger | None = None) -> dict[str, Any]:
    """Open a snapshot by wholesale replacement, never by incremental merge."""

    snap = load_snapshot(path)
    snapshot_declaration = parse_declaration(snap["declaration"])
    mismatch = False
    differences: list[str] = []
    if current_declaration is not None:
        if snap.get("declaration_version") != current_declaration.declaration_version:
            mismatch = True
            differences.append("declaration_version")
        if snap.get("declaration_fingerprint") != current_declaration.fingerprint:
            mismatch = True
            differences.append("declaration_fingerprint")
    ledger.replace_from_snapshot(snap)
    report = {
        "ok": True,
        "replacement_mode": "wholesale",
        "merged": False,
        "declaration_mismatch": mismatch,
        "mismatch_fields": sorted(set(differences)),
        "snapshot_declaration_version": snap.get("declaration_version"),
        "current_declaration_version": (
            current_declaration.declaration_version if current_declaration is not None else None
        ),
        "snapshot_declaration": snapshot_declaration.to_dict(),
        "imported_results": len(snap.get("results", [])),
        "imported_interactions": len(snap.get("interactions", [])),
        "ledger_revision": snap.get("ledger_revision"),
        "completed_clusters": snap.get("completed_clusters", []),
    }
    if logger:
        logger.emit(
            "snapshot_opened",
            code=E_VERSION if mismatch else None,
            path=str(path),
            declaration_mismatch=mismatch,
            mismatch_fields=report["mismatch_fields"],
            replacement_mode="wholesale",
            snapshot_declaration_version=report["snapshot_declaration_version"],
            current_declaration_version=report["current_declaration_version"],
        )
    return report


def _current_result_index(declaration: Declaration,
                          results: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    versions = {cluster.cluster_id: cluster.config_version for cluster in declaration.clusters}
    return {
        (row["task_id"], row["cluster_id"]): row
        for row in results
        if not row.get("is_stale", False)
        and row["config_version"] == versions.get(row["cluster_id"])
    }


def recover_session(declaration: Declaration, ledger: Ledger, snapshot_path: str | Path,
                    logger: EventLogger | None = None,
                    sample_data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Recover from committed ledger rows only; incomplete units are unknown."""

    snap = load_snapshot(snapshot_path)
    replaced = False
    if snap.get("declaration_fingerprint") != declaration.fingerprint:
        # Opening a snapshot with a different declaration is a wholesale
        # replacement, not a merge with the live ledger.
        ledger.replace_from_snapshot(snap)
        working_declaration = parse_declaration(snap["declaration"])
        replaced = True
        warning_code = E_VERSION
    else:
        working_declaration = declaration
        warning_code = None

    state = ledger.read_consistent_state()
    result_index = _current_result_index(working_declaration, state["results"])
    expected_units = [
        (task.task_id, cluster_id, working_declaration.cluster_map()[cluster_id].config_version)
        for task in working_declaration.tasks
        for cluster_id in task.target_clusters
    ]
    unknown = [
        (task_id, cluster_id, version, "no complete ledger result after restart")
        for task_id, cluster_id, version in expected_units
        if (task_id, cluster_id) not in result_index
    ]
    marked = ledger.mark_recovery_unknown(unknown)
    completed_clusters, incomplete_units = _completed_and_incomplete(
        working_declaration, ledger.fetch_results(include_stale=True)
    )
    snapshot_claimed = set(snap.get("completed_clusters", []))
    ledger_completed = set(completed_clusters)
    report = {
        "ok": True,
        "recovery_ok": True,
        "clean_shutdown": False,
        "exit_report": None,
        "recovery_basis": "complete SQLite ledger rows only",
        "snapshot_used_for": ["declaration", "tasks", "latency caliber", "expected units"],
        "declaration_mismatch": replaced,
        "replacement_mode": "wholesale" if replaced else "none_ledger_is_authoritative",
        "marked_unknown_count": marked,
        "unknown_units": [
            {
                "task_id": task_id,
                "cluster_id": cluster_id,
                "config_version": version,
                "status": "unknown",
                "rerun_required": True,
            }
            for task_id, cluster_id, version, _ in unknown
        ],
        "completed_clusters": sorted(ledger_completed),
        "snapshot_claimed_completed_clusters": sorted(snapshot_claimed),
        "snapshot_claims_not_backed_by_ledger": sorted(snapshot_claimed - ledger_completed),
        "incomplete_units": incomplete_units,
        "latency_caliber_version": snap.get("latency_caliber_version"),
    }
    if sample_data is not None:
        replay = replay_snapshot(snapshot_path, sample_data, logger=logger)
        report["replay"] = replay
        report["replay_ok"] = replay["ok"]
        report["ok"] = replay["ok"]
    if logger:
        logger.emit(
            "session_recovered",
            code=warning_code,
            clean_shutdown=False,
            recovery_basis=report["recovery_basis"],
            declaration_mismatch=replaced,
            marked_unknown_count=marked,
            completed_clusters=report["completed_clusters"],
            replay_ok=report.get("replay_ok"),
        )
    return report


def replay_snapshot(snapshot_path: str | Path, sample_data: Mapping[str, Any], *,
                    logger: EventLogger | None = None) -> dict[str, Any]:
    """Recompute snapshot summaries and latency caliber without trusting results."""

    snap = load_snapshot(snapshot_path)
    declaration = parse_declaration(snap["declaration"])
    tasks = declaration.task_map()
    clusters = declaration.cluster_map()
    results = snap.get("results", [])
    errors: list[dict[str, Any]] = []

    def add(message: str, *, task_id: str, cluster_id: str, key: str,
            expected: Any = None, actual: Any = None) -> None:
        error = {
            "code": E_REPLAY,
            "message": message,
            "task_id": task_id,
            "cluster_id": cluster_id,
            "first_diff_key": key,
        }
        if expected is not None:
            error["expected"] = expected
        if actual is not None:
            error["actual"] = actual
        errors.append(error)

    for result in results:
        task = tasks.get(result.get("task_id"))
        cluster = clusters.get(result.get("cluster_id"))
        if task is None or cluster is None:
            add(
                "snapshot result references an undeclared task or cluster",
                task_id=result.get("task_id", "<missing>"),
                cluster_id=result.get("cluster_id", "<missing>"),
                key="$declaration",
            )
            continue
        if int(result.get("config_version", -1)) not in cluster.version_history:
            add(
                "snapshot result config version is not reproducible",
                task_id=task.task_id,
                cluster_id=cluster.cluster_id,
                key="$config_version",
                actual=result.get("config_version"),
            )
            continue
        current_version = cluster.config_version
        if int(result["config_version"]) == current_version and result.get("is_stale", False):
            add(
                "snapshot current-version result is marked stale",
                task_id=task.task_id,
                cluster_id=cluster.cluster_id,
                key="$is_stale",
                actual=True,
            )
        elif int(result["config_version"]) != current_version and not result.get("is_stale", False):
            add(
                "snapshot old-version result is visible as current",
                task_id=task.task_id,
                cluster_id=cluster.cluster_id,
                key="$is_stale",
                actual=False,
            )
        try:
            checksum = result_checksum(result)
        except Exception as exc:
            add(
                "snapshot ledger row cannot be canonicalized",
                task_id=task.task_id,
                cluster_id=cluster.cluster_id,
                key="$ledger_checksum",
                actual=str(exc),
            )
            continue
        if checksum != result.get("ledger_checksum"):
            add(
                "snapshot ledger checksum mismatch",
                task_id=task.task_id,
                cluster_id=cluster.cluster_id,
                key="$ledger_checksum",
                expected=checksum,
                actual=result.get("ledger_checksum"),
            )
            continue
        try:
            recomputed = compute_aggregate(
                cluster_id=cluster.cluster_id,
                nodes=cluster.nodes,
                config_version=int(result["config_version"]),
                config_keys=task.config_keys,
                aggregation={"mode": task.aggregation_mode},
                sample_data=sample_data,
            )
        except AggregationDataError as exc:
            add(
                f"snapshot result is not reproducible: {exc}",
                task_id=task.task_id,
                cluster_id=cluster.cluster_id,
                key=exc.field_path,
            )
            continue
        if recomputed.components != result.get("components"):
            key = first_difference(recomputed.components, result.get("components", {}))
            add(
                "snapshot aggregate component is not reproducible",
                task_id=task.task_id,
                cluster_id=cluster.cluster_id,
                key=key,
                expected=recomputed.components.get(key),
                actual=result.get("components", {}).get(key),
            )
        elif recomputed.summary != result.get("summary"):
            add(
                "snapshot aggregate summary is not reproducible",
                task_id=task.task_id,
                cluster_id=cluster.cluster_id,
                key="$summary",
                expected=recomputed.summary,
                actual=result.get("summary"),
            )

    # Rebuild task-level aggregate digests from the snapshot's own rows.
    computed_task_digests = {
        item["task_id"]: item for item in _task_digests(declaration, results)
    }
    recorded_task_digests = {
        item["task_id"]: item for item in snap.get("task_digests", [])
    }
    for task in declaration.tasks:
        computed = computed_task_digests.get(task.task_id)
        recorded = recorded_task_digests.get(task.task_id)
        if recorded is None:
            add(
                "snapshot lacks the task aggregate digest",
                task_id=task.task_id,
                cluster_id="*",
                key="$task_digest.missing",
            )
        elif computed is None or computed.get("aggregate_digest") != recorded.get("aggregate_digest"):
            add(
                "snapshot task-level aggregate digest is not reproducible",
                task_id=task.task_id,
                cluster_id="*",
                key="$task_digest",
                expected=computed.get("aggregate_digest") if computed else None,
                actual=recorded.get("aggregate_digest"),
            )

    recorded_caliber = snap.get("latency_caliber_version")
    if recorded_caliber != LATENCY_CALIBER_VERSION:
        add(
            "snapshot latency caliber version is not reproducible",
            task_id="*",
            cluster_id="*",
            key="$latency_caliber_version",
            expected=LATENCY_CALIBER_VERSION,
            actual=recorded_caliber,
        )
    recomputed_stats = compute_latency_stats(snap.get("interactions", []))
    recorded_stats = snap.get("latency_stats", {})
    if recomputed_stats != recorded_stats:
        add(
            "snapshot latency statistics are not reproducible",
            task_id="*",
            cluster_id="*",
            key="$latency_stats",
            expected=recomputed_stats.get("overall"),
            actual=recorded_stats.get("overall"),
        )

    if logger:
        for error in errors:
            logger.emit(
                "replay_mismatch",
                code=E_REPLAY,
                task_id=error["task_id"],
                cluster_id=error["cluster_id"],
                first_diff_key=error["first_diff_key"],
                errors=[error],
            )
        logger.emit(
            "replay_completed",
            code=None if not errors else E_REPLAY,
            checked_results=len(results),
            error_count=len(errors),
            latency_caliber_version=LATENCY_CALIBER_VERSION,
        )
    return {
        "ok": not errors,
        "checked_results": len(results),
        "checked_tasks": len(declaration.tasks),
        "latency_caliber_version": LATENCY_CALIBER_VERSION,
        "errors": errors,
        "first_diff_key": errors[0]["first_diff_key"] if errors else None,
    }
