from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from .errors import WorkbenchError
from .scenario import ERROR_CATEGORIES, Scenario
from .storage import Store, summary_hash


REPORT_SCHEMA_VERSION = 1

WINDOW_OUTCOME_ORDER = (
    "started",
    "started_in_progress",
    "started_failed",
    "budget_unstarted",
    "not_started_within_budget",
    "early_terminated",
    "not_started_after_knee",
)


def _json_or_none(value: str | None) -> Any:
    return json.loads(value) if value else None


def _empty_error_counts() -> dict[str, int]:
    return {category: 0 for category in ERROR_CATEGORIES}


def _window_outcome(state: str) -> str:
    return {
        "committed": "started",
        "running": "started_in_progress",
        "failed": "started_failed",
        "not_started_budget": "budget_unstarted",
        "not_started_incomplete": "not_started_within_budget",
        "pending": "not_started_within_budget",
        "skipped_early_termination": "early_terminated",
        "skipped_after_knee": "not_started_after_knee",
    }.get(state, state)


def _build_window_entries(
    window_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    aggregate_errors = _empty_error_counts()
    anomalies: list[dict[str, Any]] = []
    for row in sorted(window_rows, key=lambda item: int(item["window_index"])):
        summary = _json_or_none(row["summary_json"])
        error_counts = (
            dict(summary["error_counts"]) if summary is not None else _empty_error_counts()
        )
        for category in ERROR_CATEGORIES:
            aggregate_errors[category] += int(error_counts.get(category, 0))
        if summary is not None:
            anomalies.extend(summary.get("anomalies", []))
        entries.append(
            {
                "window_index": int(row["window_index"]),
                "state": row["state"],
                "outcome_class": _window_outcome(row["state"]),
                "clock_start_ms": int(row["clock_start_ms"]),
                "clock_end_ms": int(row["clock_end_ms"]),
                "request_seq_start": int(row["request_seq_start"]),
                "request_seq_end": int(row["request_seq_end"]),
                "planned_requests": int(row["planned_requests"]),
                "summary": summary,
                "summary_sha256": row["summary_sha256"],
                "reason": _json_or_none(row["last_error"]),
            }
        )
    anomalies.sort(
        key=lambda item: (
            item["tenant"],
            int(item["gradient_index"]),
            int(item["window_index"]),
            ERROR_CATEGORIES.index(item["category"]),
        )
    )
    return entries, aggregate_errors, anomalies


def _gradient_outcome_class(state: str, committed_windows: int) -> str:
    if state == "failed":
        return "started_failed"
    if state == "terminated":
        return "early_terminated"
    if state == "skipped_after_knee":
        return "not_started_after_knee"
    if state == "budget_abandoned":
        return "budget_unstarted" if committed_windows == 0 else "partial_budget_unstarted"
    if state == "incomplete":
        return "not_started_within_budget" if committed_windows == 0 else "partial_incomplete"
    if committed_windows > 0:
        return "started"
    return "not_started_within_budget"


def _capacity_conclusion(
    *,
    tenant: str,
    gradient_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    capacity_concurrency: int | None = None
    highest_gradient: int | None = None
    degraded_capacity = False
    aborted_at: dict[str, Any] | None = None
    incomplete_at: dict[str, Any] | None = None
    basis: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    for entry in sorted(gradient_entries, key=lambda item: int(item["gradient_index"])):
        verdict = entry.get("verdict")
        state = entry["state"]
        if state in ("completed", "terminated") and verdict in ("pass", "degraded_pass"):
            capacity_concurrency = int(entry["concurrency"])
            highest_gradient = int(entry["gradient_index"])
            degraded_capacity = verdict == "degraded_pass"
            basis.append(
                {
                    "gradient_index": entry["gradient_index"],
                    "concurrency": entry["concurrency"],
                    "verdict": verdict,
                    "role": "supports_capacity",
                    "windows": [
                        window["window_index"]
                        for window in entry["windows"]
                        if window["state"] == "committed"
                    ],
                }
            )
            for window in entry["windows"]:
                if window["state"] == "committed":
                    evidence.append(
                        {
                            "tenant": tenant,
                            "gradient_index": entry["gradient_index"],
                            "window_index": window["window_index"],
                            "verdict": _window_verdict(entry, window["window_index"]),
                            "role": "supports_capacity",
                            "request_seq_range": [
                                window["request_seq_start"],
                                window["request_seq_end"],
                            ],
                        }
                    )
            continue
        if verdict == "abort":
            trigger = (entry.get("termination") or {}).get("trigger")
            trigger_window = (
                trigger.get("window_index")
                if trigger
                else next(
                    (
                        item["window_index"]
                        for item in entry.get("evidence_chain", [])
                        if item.get("verdict") == "abort"
                    ),
                    None,
                )
            )
            aborted_at = {
                "gradient_index": entry["gradient_index"],
                "concurrency": entry["concurrency"],
                "window_index": trigger_window,
                "early_terminated": entry["state"] == "terminated",
                "threshold_evidence": next(
                    (
                        item
                        for item in entry.get("evidence_chain", [])
                        if item.get("window_index") == trigger_window
                    ),
                    trigger,
                ),
            }
            basis.append(
                {
                    "gradient_index": entry["gradient_index"],
                    "concurrency": entry["concurrency"],
                    "verdict": "abort",
                    "role": "stops_capacity_ladder",
                    "window_index": trigger_window,
                }
            )
            if trigger_window is not None:
                evidence.append(
                    {
                        "tenant": tenant,
                        "gradient_index": entry["gradient_index"],
                        "window_index": trigger_window,
                        "verdict": "abort",
                        "role": "triggers_abort",
                        "threshold_evidence": aborted_at["threshold_evidence"],
                    }
                )
            break
        if state not in ("skipped_after_knee",):
            incomplete_at = {
                "gradient_index": entry["gradient_index"],
                "concurrency": entry["concurrency"],
                "state": state,
                "outcome_class": entry["outcome_class"],
            }
            basis.append(
                {
                    "gradient_index": entry["gradient_index"],
                    "concurrency": entry["concurrency"],
                    "verdict": verdict,
                    "role": "does_not_support_capacity",
                    "state": state,
                }
            )
            break

    if aborted_at is not None and capacity_concurrency is None:
        status = "no_capacity"
    elif capacity_concurrency is not None:
        status = "capacity_found"
    elif incomplete_at is not None:
        status = "incomplete"
    else:
        status = "no_observations"

    return {
        "status": status,
        "capacity_concurrency": capacity_concurrency,
        "highest_passing_gradient": highest_gradient,
        "degraded_capacity": degraded_capacity,
        "aborted_at": aborted_at,
        "incomplete_at": incomplete_at,
        "decoupled_from_later_gradients": True,
        "basis": basis,
        "evidence_windows": evidence,
    }


def _window_verdict(entry: dict[str, Any], window_index: int) -> str | None:
    for item in entry.get("evidence_chain", []):
        if int(item["window_index"]) == window_index:
            return item.get("verdict")
    return None


def build_report(store: Store, run_id: str) -> dict[str, Any]:
    meta = store.meta(run_id)
    scenario: Scenario = store.load_scenario(run_id)
    gradients = store.gradients(run_id)
    windows = store.windows(run_id)
    windows_by_gradient: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for window in windows:
        windows_by_gradient.setdefault(
            (window["tenant"], int(window["gradient_index"])),
            [],
        ).append(window)

    gradient_entries: list[dict[str, Any]] = []
    all_anomalies: list[dict[str, Any]] = []
    unit_outcome_counts: Counter[str] = Counter()
    aggregate_error_counts = _empty_error_counts()
    for row in sorted(
        gradients,
        key=lambda item: (item["tenant"], int(item["gradient_index"])),
    ):
        window_entries, error_counts, anomalies = _build_window_entries(
            windows_by_gradient.get(
                (row["tenant"], int(row["gradient_index"])),
                [],
            )
        )
        for category in ERROR_CATEGORIES:
            aggregate_error_counts[category] += error_counts[category]
        all_anomalies.extend(anomalies)
        unit_outcome_counts.update(
            window["outcome_class"] for window in window_entries
        )
        evidence_chain = _json_or_none(row["evidence_json"]) or []
        termination = _json_or_none(row["termination_json"])
        gradient_entries.append(
            {
                "batch_id": row["batch_id"],
                "tenant": row["tenant"],
                "gradient_index": int(row["gradient_index"]),
                "concurrency": int(row["concurrency"]),
                "duration_ms": int(row["duration_ms"]),
                "pace_ms": int(row["pace_ms"]),
                "window_ms": int(row["window_ms"]),
                "request_timeout_ms": int(row["request_timeout_ms"]),
                "logical_start_ms": int(row["logical_start_ms"]),
                "logical_end_ms": int(row["logical_end_ms"]),
                "state": row["state"],
                "outcome_class": _gradient_outcome_class(
                    row["state"],
                    int(row["committed_windows"]),
                ),
                "verdict": row["verdict"],
                "committed_windows": int(row["committed_windows"]),
                "total_windows": int(row["total_windows"]),
                "windows": window_entries,
                "evidence_chain": evidence_chain,
                "termination": termination,
                "error_counts": error_counts,
                "error_count_total": sum(error_counts.values()),
            }
        )

    tenant_entries: list[dict[str, Any]] = []
    for tenant in sorted(scenario.tenants, key=lambda item: item.name):
        service = scenario.tenant_service_map[tenant.name]
        entries = [
            entry for entry in gradient_entries if entry["tenant"] == tenant.name
        ]
        conclusion = _capacity_conclusion(
            tenant=tenant.name,
            gradient_entries=entries,
        )
        tenant_entries.append(
            {
                "tenant": tenant.name,
                "service": service.name,
                "endpoint": {
                    "host": service.host,
                    "port": service.port,
                    "expected_status": service.expected_status,
                },
                "slo": {
                    "min_success_rate": tenant.slo.min_success_rate,
                    "max_p95_latency_ms": tenant.slo.max_p95_latency_ms,
                    "degraded_success_margin": tenant.slo.degraded_success_margin,
                    "degraded_latency_ratio": tenant.slo.degraded_latency_ratio,
                },
                "capacity_conclusion": conclusion,
                "gradients": entries,
            }
        )

    all_anomalies.sort(
        key=lambda item: (
            item["tenant"],
            int(item["gradient_index"]),
            int(item["window_index"]),
            ERROR_CATEGORIES.index(item["category"]),
        )
    )
    meta_status = meta["status"]
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run": {
            "run_id": run_id,
            "scenario_hash": meta["scenario_hash"],
            "seed": int(meta["seed"]),
            "status": meta_status,
            "global_time_budget_ms": int(meta["global_budget_ms"]),
            "budget_used_ms": store.budget_used(run_id),
            "budget_remaining_ms": int(meta["global_budget_ms"])
            - store.budget_used(run_id),
            "logical_clock_ms": int(meta["logical_clock_ms"]),
            "request_seq_watermark": int(meta["request_seq_watermark"]),
        },
        "policies": {
            "p95_rule": "sorted_linear_interpolation_rank_0.95_n_minus_1",
            "error_categories": list(ERROR_CATEGORIES),
            "capacity_order": "strictly_increasing_concurrency_ladder",
            "tenant_conclusion_decoupled_from_later_gradients": True,
        },
        "tenants": tenant_entries,
        "diagnostics": {
            "error_counts": aggregate_error_counts,
            "error_count_total": sum(aggregate_error_counts.values()),
            "unit_outcome_counts": {
                key: unit_outcome_counts.get(key, 0)
                for key in WINDOW_OUTCOME_ORDER
            },
            "anomalies": all_anomalies,
        },
    }


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def write_report(path: str | Path, report: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(report_json(report), encoding="utf-8")
    os.replace(temp, target)


def render_text_report(report: dict[str, Any]) -> str:
    lines = [
        (
            f"run={report['run']['run_id']} status={report['run']['status']} "
            f"clock={report['run']['logical_clock_ms']}ms "
            f"budget={report['run']['budget_used_ms']}/{report['run']['global_time_budget_ms']}ms"
        )
    ]
    for tenant in report["tenants"]:
        conclusion = tenant["capacity_conclusion"]
        capacity = conclusion["capacity_concurrency"]
        lines.append(
            f"{tenant['tenant']}: {conclusion['status']} "
            f"capacity={capacity if capacity is not None else '-'}"
            + (" degraded" if conclusion["degraded_capacity"] else "")
        )
        for gradient in tenant["gradients"]:
            window_bits = []
            by_window = {
                int(item["window_index"]): item.get("verdict")
                for item in gradient["evidence_chain"]
            }
            for window in gradient["windows"]:
                if window["state"] == "committed":
                    summary = window["summary"]
                    window_bits.append(
                        f"w{window['window_index']}={by_window.get(window['window_index'], '?')}"
                        f"({summary['success_rate']:.3f},{summary['p95_latency_ms']}ms)"
                    )
                else:
                    window_bits.append(
                        f"w{window['window_index']}={window['outcome_class']}"
                    )
            lines.append(
                f"  g{gradient['gradient_index']} c={gradient['concurrency']} "
                f"{gradient['state']}/{gradient['verdict']} "
                + " ".join(window_bits)
            )
            errors = {
                key: value
                for key, value in gradient["error_counts"].items()
                if value
            }
            if errors:
                lines.append(f"    errors={json.dumps(errors, sort_keys=True)}")
        if conclusion["aborted_at"]:
            aborted = conclusion["aborted_at"]
            lines.append(
                f"  evidence: abort at g{aborted['gradient_index']} "
                f"w{aborted['window_index']} threshold={json.dumps(aborted['threshold_evidence'], sort_keys=True)}"
            )
        elif conclusion["evidence_windows"]:
            refs = ", ".join(
                f"g{item['gradient_index']}w{item['window_index']}"
                for item in conclusion["evidence_windows"]
            )
            lines.append(f"  evidence: supports capacity via {refs}")
    return "\n".join(lines) + "\n"


def status_document(store: Store, run_id: str) -> dict[str, Any]:
    meta = store.meta(run_id)
    gradients = store.gradients(run_id)
    windows = store.windows(run_id)
    windows_by_gradient: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in windows:
        windows_by_gradient.setdefault(
            (row["tenant"], int(row["gradient_index"])),
            [],
        ).append(row)
    units = [
        {
            "tenant": row["tenant"],
            "gradient_index": int(row["gradient_index"]),
            "window_index": int(row["window_index"]),
            "state": row["state"],
            "outcome_class": _window_outcome(row["state"]),
            "attempt_count": int(row["attempt_count"]),
            "request_seq_range": [
                int(row["request_seq_start"]),
                int(row["request_seq_end"]),
            ],
            "clock_range_ms": [
                int(row["clock_start_ms"]),
                int(row["clock_end_ms"]),
            ],
        }
        for row in sorted(
            windows,
            key=lambda item: (
                item["tenant"],
                int(item["gradient_index"]),
                int(item["window_index"]),
            ),
        )
    ]
    gradient_status = []
    for row in sorted(
        gradients,
        key=lambda item: (item["tenant"], int(item["gradient_index"])),
    ):
        related = windows_by_gradient.get(
            (row["tenant"], int(row["gradient_index"])),
            [],
        )
        gradient_status.append(
            {
                "tenant": row["tenant"],
                "gradient_index": int(row["gradient_index"]),
                "concurrency": int(row["concurrency"]),
                "state": row["state"],
                "verdict": row["verdict"],
                "outcome_class": _gradient_outcome_class(
                    row["state"],
                    int(row["committed_windows"]),
                ),
                "committed_windows": int(row["committed_windows"]),
                "total_windows": int(row["total_windows"]),
                "window_states": {
                    state: sum(1 for item in related if item["state"] == state)
                    for state in sorted({item["state"] for item in related})
                },
            }
        )
    return {
        "run_id": run_id,
        "status": meta["status"],
        "logical_clock_ms": int(meta["logical_clock_ms"]),
        "request_seq_watermark": int(meta["request_seq_watermark"]),
        "global_time_budget_ms": int(meta["global_budget_ms"]),
        "budget_used_ms": store.budget_used(run_id),
        "budget_remaining_ms": int(meta["global_budget_ms"])
        - store.budget_used(run_id),
        "gradients": gradient_status,
        "units": units,
    }


def verify_run(
    store: Store,
    run_id: str,
    *,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    if not store.exists:
        raise WorkbenchError("E_DB_MISSING", f"database not found: {store.path}")
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: Any = None) -> None:
        item = {"name": name, "ok": bool(condition)}
        if detail is not None:
            item["detail"] = detail
        checks.append(item)
        if not condition:
            errors.append(item)

    meta = store.meta(run_id)
    windows = store.windows(run_id)
    gradients = store.gradients(run_id)
    used = store.budget_used(run_id)
    check(
        "global_budget_not_oversubscribed",
        used <= int(meta["global_budget_ms"]),
        {"used_ms": used, "budget_ms": int(meta["global_budget_ms"])},
    )
    committed = [row for row in windows if row["state"] == "committed"]
    expected_watermark = max(
        (int(row["request_seq_end"]) for row in committed),
        default=0,
    )
    expected_clock = max(
        (int(row["clock_end_ms"]) for row in committed),
        default=0,
    )
    check(
        "request_sequence_watermark",
        expected_watermark == int(meta["request_seq_watermark"]),
        {
            "stored": int(meta["request_seq_watermark"]),
            "expected": expected_watermark,
        },
    )
    check(
        "logical_clock_watermark",
        expected_clock == int(meta["logical_clock_ms"]),
        {
            "stored": int(meta["logical_clock_ms"]),
            "expected": expected_clock,
        },
    )

    bad_hashes = []
    partial_summaries = []
    for row in windows:
        if row["state"] == "committed":
            try:
                summary = json.loads(row["summary_json"])
                actual_hash = summary_hash(summary)
            except (TypeError, json.JSONDecodeError):
                bad_hashes.append(
                    {
                        "tenant": row["tenant"],
                        "gradient_index": row["gradient_index"],
                        "window_index": row["window_index"],
                        "reason": "invalid summary JSON",
                    }
                )
                continue
            if actual_hash != row["summary_sha256"]:
                bad_hashes.append(
                    {
                        "tenant": row["tenant"],
                        "gradient_index": row["gradient_index"],
                        "window_index": row["window_index"],
                        "stored": row["summary_sha256"],
                        "actual": actual_hash,
                    }
                )
        elif row["summary_json"] is not None:
            partial_summaries.append(
                {
                    "tenant": row["tenant"],
                    "gradient_index": row["gradient_index"],
                    "window_index": row["window_index"],
                    "state": row["state"],
                }
            )
    check("committed_summary_hashes", not bad_hashes, bad_hashes)
    check("no_partial_summary_visible", not partial_summaries, partial_summaries)

    bad_counts = []
    for gradient in gradients:
        count = sum(
            1
            for row in committed
            if row["tenant"] == gradient["tenant"]
            and int(row["gradient_index"]) == int(gradient["gradient_index"])
        )
        if count != int(gradient["committed_windows"]):
            bad_counts.append(
                {
                    "tenant": gradient["tenant"],
                    "gradient_index": gradient["gradient_index"],
                    "stored": int(gradient["committed_windows"]),
                    "actual": count,
                }
            )
    check("gradient_committed_window_counts", not bad_counts, bad_counts)

    report = build_report(store, run_id)
    check(
        "report_has_all_tenants",
        len(report["tenants"]) > 0,
        {"tenant_count": len(report["tenants"])},
    )
    if report_path is not None:
        path = Path(report_path)
        if not path.exists():
            check("report_file_exists", False, {"path": str(path)})
        else:
            try:
                supplied = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                check("report_file_parse", False, {"path": str(path), "error": str(exc)})
            else:
                check("report_matches_persisted_state", supplied == report)

    return {
        "ok": not errors,
        "run_id": run_id,
        "checks": checks,
        "errors": errors,
        "report": report,
    }

