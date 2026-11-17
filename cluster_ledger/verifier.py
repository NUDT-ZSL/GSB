"""Authoritative local recomputation and view/ledger consistency checks."""

from __future__ import annotations

from typing import Any, Mapping

from .clock import mono_ns
from .config import Declaration
from .dashboard import Dashboard
from .deterministic import AggregationDataError, compute_aggregate, first_difference
from .errors import E_LEDGER, E_VERIFY, E_VERSION, BusinessError
from .ledger import Ledger, result_checksum
from .logging_utils import EventLogger
from .metrics import compute_latency_stats


class AuthorityVerifier:
    def __init__(self, declaration: Declaration, sample_data: Mapping[str, Any],
                 ledger: Ledger, logger: EventLogger | None = None) -> None:
        self.declaration = declaration
        self.sample_data = sample_data
        self.ledger = ledger
        self.logger = logger or EventLogger(None)

    def _verify_result(self, result: dict[str, Any],
                       tasks: dict[str, Any], clusters: dict[str, Any]) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []

        def add(code: str, message: str, *, first_diff_key: str, expected: Any = None,
                actual: Any = None, details: dict[str, Any] | None = None) -> None:
            error = {
                "code": code,
                "message": message,
                "task_id": result["task_id"],
                "cluster_id": result["cluster_id"],
                "config_version": result["config_version"],
                "first_diff_key": first_diff_key,
            }
            if expected is not None:
                error["expected"] = expected
            if actual is not None:
                error["actual"] = actual
            if details:
                error["details"] = details
            errors.append(error)

        try:
            expected_checksum = result_checksum(result)
        except Exception as exc:
            add(
                E_LEDGER,
                "ledger result cannot be canonicalized",
                first_diff_key="$ledger_checksum",
                details={"exception": str(exc)},
            )
            return errors
        if expected_checksum != result["ledger_checksum"]:
            add(
                E_LEDGER,
                "ledger result checksum mismatch (record damaged)",
                first_diff_key="$ledger_checksum",
                expected=expected_checksum,
                actual=result["ledger_checksum"],
            )
            return errors

        task = tasks.get(result["task_id"])
        cluster = clusters.get(result["cluster_id"])
        if task is None or cluster is None:
            add(
                E_VERIFY,
                "result references a task or cluster absent from the current declaration",
                first_diff_key="$declaration",
            )
            return errors
        if result["config_version"] not in cluster.version_history:
            add(
                E_VERSION,
                "result config version is absent from the cluster version history",
                first_diff_key="$config_version",
                details={"known_versions": list(cluster.version_history)},
            )
            return errors

        try:
            recomputed = compute_aggregate(
                cluster_id=cluster.cluster_id,
                nodes=cluster.nodes,
                config_version=result["config_version"],
                config_keys=task.config_keys,
                aggregation={"mode": task.aggregation_mode},
                sample_data=self.sample_data,
            )
        except AggregationDataError as exc:
            add(
                E_VERIFY,
                f"authoritative recomputation failed: {exc}",
                first_diff_key=exc.field_path,
            )
            return errors

        if recomputed.aggregation_mode != result["aggregation_mode"]:
            add(
                E_VERIFY,
                "aggregation mode differs from authoritative recomputation",
                first_diff_key="$aggregation_mode",
                expected=recomputed.aggregation_mode,
                actual=result["aggregation_mode"],
            )
            return errors
        if recomputed.components != result["components"]:
            key = first_difference(recomputed.components, result["components"])
            add(
                E_VERIFY,
                "aggregate component differs from authoritative recomputation",
                first_diff_key=key,
                expected=recomputed.components.get(key),
                actual=result["components"].get(key),
            )
            return errors
        if recomputed.summary != result["summary"]:
            add(
                E_VERIFY,
                "aggregate summary differs from authoritative recomputation",
                first_diff_key="$summary",
                expected=recomputed.summary,
                actual=result["summary"],
            )
            return errors

        current_version = cluster.config_version
        if result["config_version"] == current_version and result["is_stale"]:
            add(
                E_LEDGER,
                "current-version result is incorrectly marked stale",
                first_diff_key="$is_stale",
            )
        if result["config_version"] != current_version and not result["is_stale"]:
            add(
                E_LEDGER,
                "old-version result is incorrectly visible as current",
                first_diff_key="$is_stale",
            )
        return errors

    def _verify_view(self, view: Mapping[str, Any], all_results: list[dict[str, Any]],
                     tasks: dict[str, Any], clusters: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        errors: list[dict[str, Any]] = []
        visible = view.get("results", [])
        visible_index: dict[tuple[str, str], Mapping[str, Any]] = {}
        for row in visible:
            key = (row.get("task_id"), row.get("cluster_id"))
            if key in visible_index:
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "duplicate result in displayed view",
                        "task_id": key[0],
                        "cluster_id": key[1],
                        "first_diff_key": "$view.duplicate",
                    }
                )
            visible_index[key] = row
            cluster = clusters.get(row.get("cluster_id"))
            if cluster is None or row.get("config_version") != cluster.config_version:
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "displayed view mixes a non-current config version",
                        "task_id": key[0],
                        "cluster_id": key[1],
                        "first_diff_key": "$config_version",
                        "actual": row.get("config_version"),
                    }
                )

        expected_current = [
            row for row in all_results
            if not row["is_stale"]
            and row["task_id"] in tasks
            and row["cluster_id"] in clusters
            and row["config_version"] == clusters[row["cluster_id"]].config_version
        ]
        view_clusters = {
            row.get("cluster_id"): row
            for row in view.get("clusters", [])
            if isinstance(row, dict)
        }
        for cluster_id, cluster in clusters.items():
            shown = view_clusters.get(cluster_id)
            if shown is None:
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "displayed view is missing a declared cluster",
                        "task_id": "*",
                        "cluster_id": cluster_id,
                        "first_diff_key": "$view.cluster.missing",
                    }
                )
            elif shown.get("config_version") != cluster.config_version:
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "displayed cluster config version differs from declaration",
                        "task_id": "*",
                        "cluster_id": cluster_id,
                        "first_diff_key": "$view.cluster.config_version",
                        "expected": cluster.config_version,
                        "actual": shown.get("config_version"),
                    }
                )
        for cluster_id in sorted(set(view_clusters) - set(clusters)):
            errors.append(
                {
                    "code": E_VERIFY,
                    "message": "displayed view contains an undeclared cluster",
                    "task_id": "*",
                    "cluster_id": cluster_id,
                    "first_diff_key": "$view.cluster.unexpected",
                }
            )

        expected_counts: dict[str, int] = {task_id: 0 for task_id in tasks}
        for row in expected_current:
            expected_counts[row["task_id"]] += 1
        view_tasks = {
            row.get("task_id"): row
            for row in view.get("tasks", [])
            if isinstance(row, dict)
        }
        for task_id, task in tasks.items():
            shown = view_tasks.get(task_id)
            if shown is None:
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "displayed view is missing a declared task",
                        "task_id": task_id,
                        "cluster_id": "*",
                        "first_diff_key": "$view.task.missing",
                    }
                )
                continue
            if int(shown.get("completed_units", -1)) != expected_counts[task_id]:
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "displayed task completion count differs from ledger",
                        "task_id": task_id,
                        "cluster_id": "*",
                        "first_diff_key": "$view.task.completed_units",
                        "expected": expected_counts[task_id],
                        "actual": shown.get("completed_units"),
                    }
                )
            if int(shown.get("total_units", -1)) != len(task.target_clusters):
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "displayed task total count differs from declaration",
                        "task_id": task_id,
                        "cluster_id": "*",
                        "first_diff_key": "$view.task.total_units",
                        "expected": len(task.target_clusters),
                        "actual": shown.get("total_units"),
                    }
                )
        for task_id in sorted(set(view_tasks) - set(tasks)):
            errors.append(
                {
                    "code": E_VERIFY,
                    "message": "displayed view contains an undeclared task",
                    "task_id": task_id,
                    "cluster_id": "*",
                    "first_diff_key": "$view.task.unexpected",
                }
            )
        for row in expected_current:
            key = (row["task_id"], row["cluster_id"])
            shown = visible_index.get(key)
            if shown is None:
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "current ledger result is missing from displayed view",
                        "task_id": row["task_id"],
                        "cluster_id": row["cluster_id"],
                        "first_diff_key": "$view.missing",
                    }
                )
                continue
            if shown.get("summary") != row["summary"]:
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "displayed summary differs from ledger",
                        "task_id": row["task_id"],
                        "cluster_id": row["cluster_id"],
                        "first_diff_key": "$view.summary",
                        "expected": row["summary"],
                        "actual": shown.get("summary"),
                    }
                )
            elif shown.get("components") != row["components"]:
                key_name = first_difference(row["components"], shown.get("components", {}))
                errors.append(
                    {
                        "code": E_VERIFY,
                        "message": "displayed components differ from ledger",
                        "task_id": row["task_id"],
                        "cluster_id": row["cluster_id"],
                        "first_diff_key": key_name,
                    }
                )
        expected_keys = {(row["task_id"], row["cluster_id"]) for row in expected_current}
        for key in sorted(set(visible_index) - expected_keys):
            errors.append(
                {
                    "code": E_VERIFY,
                    "message": "displayed view contains an unexpected result",
                    "task_id": key[0],
                    "cluster_id": key[1],
                    "first_diff_key": "$view.unexpected",
                }
            )
        return errors, len(visible)

    def verify(self, view: Mapping[str, Any] | None = None, *,
               dashboard: Dashboard | None = None) -> dict[str, Any]:
        if view is None and dashboard is not None:
            view = dashboard.query_view()
        command_started_ns = mono_ns()
        command_started_ms = command_started_ns // 1_000_000
        all_results = self.ledger.fetch_results(include_stale=True)
        tasks = self.declaration.task_map()
        clusters = self.declaration.cluster_map()
        errors: list[dict[str, Any]] = []
        for result in all_results:
            started_ns = mono_ns()
            started_ms = started_ns // 1_000_000
            row_errors = self._verify_result(result, tasks, clusters)
            ended_ns = mono_ns()
            ended_ms = ended_ns // 1_000_000
            errors.extend(row_errors)
            code = row_errors[0]["code"] if row_errors else None
            self.ledger.record_interaction(
                kind="verify",
                task_id=result["task_id"],
                cluster_id=result["cluster_id"],
                config_version=result["config_version"],
                started_mono_ms=started_ms,
                ended_mono_ms=ended_ms,
                started_mono_ns=started_ns,
                ended_mono_ns=ended_ns,
                data_version=f"{self.declaration.declaration_version}:v{result['config_version']}",
                status="error" if row_errors else "ok",
                business_code=code,
                detail={"first_diff_key": row_errors[0].get("first_diff_key") if row_errors else None},
            )
            if row_errors:
                self.logger.emit(
                    "authority_mismatch",
                    code=code,
                    task_id=result["task_id"],
                    cluster_id=result["cluster_id"],
                    config_version=result["config_version"],
                    first_diff_key=row_errors[0].get("first_diff_key"),
                    errors=row_errors,
                    started_mono_ms=started_ms,
                    ended_mono_ms=ended_ms,
                )

        view_errors: list[dict[str, Any]] = []
        visible_count = 0
        if view is not None:
            view_errors, visible_count = self._verify_view(view, all_results, tasks, clusters)
            for error in view_errors:
                self.logger.emit(
                    "authority_mismatch",
                    code=error["code"],
                    task_id=error.get("task_id"),
                    cluster_id=error.get("cluster_id"),
                    first_diff_key=error.get("first_diff_key"),
                    errors=[error],
                    scope="view",
                )
        errors.extend(view_errors)
        command_ended_ns = mono_ns()
        command_ended_ms = command_ended_ns // 1_000_000
        command_code = errors[0]["code"] if errors else None
        self.ledger.record_interaction(
            kind="verify_all",
            task_id=None,
            cluster_id=None,
            config_version=None,
            started_mono_ms=command_started_ms,
            ended_mono_ms=command_ended_ms,
            started_mono_ns=command_started_ns,
            ended_mono_ns=command_ended_ns,
            data_version=(
                str(view.get("data_version"))
                if view is not None
                else f"{self.declaration.declaration_version}:r{self.ledger.revision()}"
            ),
            status="error" if errors else "ok",
            business_code=command_code,
            detail={
                "checked_ledger_results": len(all_results),
                "checked_view_results": visible_count,
                "error_count": len(errors),
            },
        )
        report = {
            "ok": not errors,
            "checked_ledger_results": len(all_results),
            "checked_view_results": visible_count,
            "first_diff_key": errors[0].get("first_diff_key") if errors else None,
            "errors": errors,
            "latency_stats": self._latency_report(),
        }
        event = "verification_passed" if not errors else "verification_failed"
        self.logger.emit(
            event,
            code=None if not errors else errors[0]["code"],
            checked_ledger_results=len(all_results),
            checked_view_results=visible_count,
            error_count=len(errors),
        )
        return report

    def _latency_report(self) -> dict[str, Any]:
        interactions = self.ledger.fetch_interactions()
        current = [row for row in interactions if not row.get("is_stale", False)]
        return {
            "current": compute_latency_stats(current),
            "history": compute_latency_stats(interactions),
            "stale_interaction_count": len(interactions) - len(current),
            "caliber_version": self.ledger.latency_caliber_version(),
        }
