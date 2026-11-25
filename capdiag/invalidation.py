from __future__ import annotations

from decimal import Decimal
from typing import Any

from .aggregation import aggregate_trace
from .evaluation import evaluate_metric
from .jsonutil import dumps, loads
from .metrics import decimal_truth_metric, raw_metric
from .storage import has_window, write_transaction
from .thresholds import select_thresholds_at_clock
from .versions import metric_version, window_version


def _scope_ids_for_selector(scenario: Any, selector: str) -> set[str]:
    if selector == "default":
        ids = {"default"}
        for service in scenario.services:
            ids.add(service.service_name)
            ids.update(instance.instance_id for instance in service.instances)
        return ids
    service = next((s for s in scenario.services if s.service_name == selector), None)
    if service is not None:
        return {service.service_name, *(i.instance_id for i in service.instances)}
    if scenario.service_for_instance(selector) is not None:
        return {selector}
    return set()


def threshold_change_plan(
    *,
    conn: Any,
    scenario: Any,
    metrics_config: dict[str, Any],
    old_view: dict[str, Any] | None,
    new_view: dict[str, Any],
    old_threshold_version: str | None = None,
) -> dict[str, Any]:
    wv = window_version(metrics_config, scenario.origin_logical_ms)
    old_entries = {
        (e["scope"], e["metric"], e["level"]): e for e in (old_view or {}).get("entries", [])
    }
    new_entries = {(e["scope"], e["metric"], e["level"]): e for e in new_view.get("entries", [])}

    changed_keys: list[dict[str, Any]] = []
    affected_scopes_by_metric: dict[str, set[str]] = {}
    effective_by_key: dict[tuple[str, str, str], int] = {}
    for key, new_entry in sorted(new_entries.items()):
        old_entry = old_entries.get(key)
        if old_entry is None or old_entry.get("value") != new_entry.get("value"):
            changed_keys.append(
                {
                    "key": ":".join(key),
                    "old_value": None if old_entry is None else old_entry.get("value"),
                    "new_value": new_entry.get("value"),
                    "effective_logical_ms": new_entry["effective_logical_ms"],
                }
            )
            metric = key[1]
            affected_scopes_by_metric.setdefault(metric, set()).update(
                _scope_ids_for_selector(scenario, key[0])
            )
            effective_by_key[key] = int(new_entry["effective_logical_ms"])

    rows = conn.execute(
        "SELECT window_pk, scope_id, window_index, end_ms FROM windows WHERE window_version=?",
        (wv,),
    ).fetchall()
    recompute: list[dict[str, Any]] = []
    recompute_keys: set[tuple[int, str]] = set()
    for row in rows:
        for change in changed_keys:
            scope, metric_name, level = change["key"].split(":", 2)
            if row["scope_id"] not in affected_scopes_by_metric.get(metric_name, set()):
                continue
            effective = change["effective_logical_ms"]
            if int(row["end_ms"]) < effective:
                continue
            identity = (int(row["window_pk"]), metric_name)
            if identity in recompute_keys:
                continue
            recompute_keys.add(identity)
            recompute.append(
                {
                    "window_pk": int(row["window_pk"]),
                    "scope_id": row["scope_id"],
                    "window_index": int(row["window_index"]),
                    "metric": metric_name,
                    "reason": f"threshold_{change['key']}_effective_{effective}",
                }
            )

    current_versions = {
        metric_name: metric_version(definition)
        for metric_name, definition in metrics_config["metrics"].items()
    }
    reuse_sql = [
        "SELECT e.window_pk, e.metric, e.metric_version, w.scope_id, w.window_index",
        "FROM evaluations e JOIN windows w ON w.window_pk=e.window_pk",
        "WHERE w.window_version=?",
    ]
    reuse_params: list[Any] = [wv]
    if old_threshold_version is not None:
        reuse_sql.append("AND e.threshold_version=?")
        reuse_params.append(old_threshold_version)
    old_version_rows = conn.execute(
        "\n".join(reuse_sql), tuple(reuse_params)
    ).fetchall()
    reuse = []
    for row in old_version_rows:
        if row["metric"] not in current_versions:
            continue
        if row["metric_version"] != current_versions[row["metric"]]:
            continue
        if (int(row["window_pk"]), row["metric"]) not in recompute_keys:
            reuse.append(
                {
                    "window_pk": int(row["window_pk"]),
                    "scope_id": row["scope_id"],
                    "window_index": int(row["window_index"]),
                    "metric": row["metric"],
                }
            )

    affected_metrics = sorted(affected_scopes_by_metric)
    return {
        "change_type": "threshold_snapshot",
        "window_version": wv,
        "changed_threshold_keys": changed_keys,
        "dependency_closure": {
            "request_assignment": {"action": "reuse", "items": "all"},
            "window_summary_and_watermark": {"action": "reuse", "items": "all"},
            "metric_fixed_values": {"action": "reuse", "items": "all"},
            "remainder_ledgers": {"action": "reuse", "items": "all"},
            "error_source_chains": {"action": "reuse", "items": "all"},
            "decimal_truth_comparisons": {"action": "reuse", "items": "all"},
            "evaluations": {
                "recompute": recompute,
                "reuse_count": len(reuse),
                "reuse": reuse,
            },
            "precision_sensitivity": {
                "recompute_metrics": affected_metrics,
                "recompute_scope_ids": sorted(
                    {scope for values in affected_scopes_by_metric.values() for scope in values}
                ),
                "reuse": "all_other_metric_pairs_and_intrinsic_differences",
            },
        },
        "recompute_count": len(recompute),
        "reuse_evaluation_count": len(reuse),
        "recompute_keys": [[window_pk, metric] for window_pk, metric in sorted(recompute_keys)],
    }


def metric_config_change_plan(old_config: dict[str, Any], new_config: dict[str, Any]) -> dict[str, Any]:
    old_window = window_version(old_config, 0)
    new_window = window_version(new_config, 0)
    changed_metrics = []
    for name, definition in new_config["metrics"].items():
        old_definition = old_config.get("metrics", {}).get(name)
        if old_definition is None or metric_version(old_definition) != metric_version(definition):
            changed_metrics.append(name)

    window_changed = old_window != new_window
    if window_changed:
        return {
            "change_type": "window_partition_rule",
            "old_window_version": old_window,
            "new_window_version": new_window,
            "dependency_closure": {
                "request_assignment": "recompute_all",
                "window_summary_and_watermark": "recompute_all",
                "metric_fixed_values": "recompute_all",
                "remainder_ledgers": "recompute_all",
                "error_source_chains": "recompute_all",
                "truth_comparisons": "recompute_all",
                "evaluations_and_sensitivity": "recompute_all",
            },
        }

    return {
        "change_type": "metric_conversion_parameter",
        "window_version": new_window,
        "changed_metrics": changed_metrics,
        "dependency_closure": {
            "request_assignment": {"action": "reuse", "items": "all"},
            "window_summary_and_watermark": {"action": "reuse", "items": "all"},
            "metric_fixed_values": {"action": "recompute", "metrics": changed_metrics},
            "remainder_ledgers": {"action": "recompute", "metrics": changed_metrics},
            "error_source_chains": {"action": "recompute", "metrics": changed_metrics},
            "truth_comparisons": {"action": "recompute", "metrics": changed_metrics},
            "evaluations_and_sensitivity": {"action": "recompute", "metrics": changed_metrics},
            "other_metrics": {"action": "reuse"},
        },
    }


def recompute_evaluations_for_new_threshold(
    *,
    conn: Any,
    scenario: Any,
    records: list[Any],
    metrics_config: dict[str, Any],
    candidates: list[Any],
    new_threshold_version: str,
    old_threshold_version: str | None,
    plan: dict[str, Any],
) -> dict[str, int]:
    origin = scenario.origin_logical_ms
    definition0 = next(iter(metrics_config["metrics"].values()))
    aggregates_by_key = aggregate_trace(
        records,
        origin_ms=origin,
        length_ms=int(definition0["window_length_ms"]),
        step_ms=int(definition0["window_step_ms"]),
        expected_window_count=metrics_config.get("window_count"),
    )
    wv = window_version(metrics_config, origin)
    affected = {tuple(item) for item in plan.get("recompute_keys", [])}
    recomputed = 0
    reused = 0

    for scope_key, buckets in aggregates_by_key.items():
        scope_type, scope_id = scope_key.split(":", 1)
        for index, aggregate in buckets.items():
            window_pk = has_window(
                conn, scope_id=scope_id, window_index=index, window_version=wv
            )
            if window_pk is None:
                continue
            expected_rate = None
            if scope_type == "service":
                expected_rate = scenario.service(scope_id).expected_request_rate
            else:
                for service in scenario.services:
                    for instance in service.instances:
                        if instance.instance_id == scope_id:
                            expected_rate = instance.expected_request_rate
            service_name = scope_id if scope_type == "service" else scenario.service_for_instance(scope_id)
            view = select_thresholds_at_clock(candidates, aggregate.window.end_ms)

            for metric_name, definition in metrics_config["metrics"].items():
                mv = metric_version(definition)
                exists = conn.execute(
                    "SELECT 1 FROM evaluations WHERE window_pk=? AND metric=? AND metric_version=? AND threshold_version=?",
                    (window_pk, metric_name, mv, new_threshold_version),
                ).fetchone()
                if exists:
                    continue

                is_affected = (window_pk, metric_name) in affected
                old_row = None
                if old_threshold_version is not None:
                    old_row = conn.execute(
                        """
                        SELECT verdict, safe_verdict, precision_sensitive,
                               higher_scale_flip, payload_json, threshold_version
                        FROM evaluations
                        WHERE window_pk=? AND metric=? AND metric_version=?
                          AND threshold_version=?
                        """,
                        (window_pk, metric_name, mv, old_threshold_version),
                    ).fetchone()
                if old_row is None:
                    old_row = conn.execute(
                        """
                        SELECT verdict, safe_verdict, precision_sensitive,
                               higher_scale_flip, payload_json, threshold_version
                        FROM evaluations
                        WHERE window_pk=? AND metric=? AND metric_version=?
                        ORDER BY rowid DESC LIMIT 1
                        """,
                        (window_pk, metric_name, mv),
                    ).fetchone()

                if not is_affected and old_row is not None:
                    reused_payload = loads(old_row["payload_json"])
                    reused_payload["threshold_version"] = new_threshold_version
                    reused_payload["reused"] = True
                    reused_payload["source_threshold_version"] = old_row["threshold_version"]
                    reused_payload["verdict_reused_without_recompute"] = True
                    with write_transaction(conn):
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO evaluations(
                                window_pk, metric, metric_version, threshold_version,
                                scope_id, verdict, safe_verdict, precision_sensitive,
                                higher_scale_flip, reused, source_threshold_version, payload_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                window_pk,
                                metric_name,
                                mv,
                                new_threshold_version,
                                scope_id,
                                old_row["verdict"],
                                old_row["safe_verdict"],
                                old_row["precision_sensitive"],
                                old_row["higher_scale_flip"],
                                old_row["threshold_version"],
                                dumps(reused_payload),
                            ),
                        )
                    reused += 1
                    continue

                value_row = conn.execute(
                    "SELECT fixed_value, error_bound FROM metric_values WHERE window_pk=? AND metric=? AND metric_version=?",
                    (window_pk, metric_name, mv),
                ).fetchone()
                if value_row is None or expected_rate is None:
                    continue
                truth = decimal_truth_metric(metric_name, definition, aggregate, expected_rate)
                raw = raw_metric(metric_name, definition, aggregate, expected_rate).value
                evaluation = evaluate_metric(
                    metric=metric_name,
                    definition=definition,
                    raw_value=raw,
                    truth_value=truth,
                    fixed_value=int(value_row["fixed_value"]),
                    error_bound=Decimal(value_row["error_bound"]),
                    threshold_view=view,
                    scope_id=scope_id,
                    service_name=service_name,
                    metric_version=mv,
                    threshold_version=new_threshold_version,
                )
                if evaluation is None:
                    continue
                with write_transaction(conn):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO evaluations(
                            window_pk, metric, metric_version, threshold_version,
                            scope_id, verdict, safe_verdict, precision_sensitive,
                            higher_scale_flip, reused, source_threshold_version, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
                        """,
                        (
                            window_pk,
                            metric_name,
                            mv,
                            new_threshold_version,
                            scope_id,
                            evaluation["verdict"],
                            evaluation["safe_verdict"],
                            1 if evaluation["precision_sensitive"] else 0,
                            1 if evaluation["higher_scale_flip"] else 0,
                            dumps(evaluation),
                        ),
                    )
                recomputed += 1

    return {"recomputed": recomputed, "reused": reused}
