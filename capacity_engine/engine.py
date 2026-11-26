from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .errors import CapacityError, ErrorCode
from .evaluator import (
    assert_precision_budget,
    build_conclusions,
    build_sensitivity,
    cumulative_window_error_report,
    decimal_full_recompute,
    error_chain_report,
    evaluate_metric_values,
)
from .hashing import canonical_dumps, sha256_object
from .invalidation import (
    apply_dependency_patch,
    apply_metric_parameter_patch,
    apply_threshold_patch,
    plan_invalidation,
    reused_digests_match,
)
from .precision import Ledger
from .storage import StateStore
from .thresholds import merge_thresholds
from .windows import assign_windows, build_window_plan


def input_manifest(input_root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(input_root.rglob("*")):
        if not path.is_file() or ".sqlite-" in path.name:
            continue
        relative = path.relative_to(input_root).as_posix()
        if relative.startswith("."):
            continue
        if path.suffix == ".sqlite":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest[relative] = digest
    return manifest


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=float) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def build_window_watermarks(
    assignments: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    plan: Any,
) -> list[dict[str, Any]]:
    by_window: dict[int, list[dict[str, Any]]] = {}
    for assignment in assignments:
        by_window.setdefault(assignment["window_no"], []).append(assignment)
    summary_by_window: dict[int, list[dict[str, Any]]] = {}
    for summary in summaries:
        summary_by_window.setdefault(summary["window_no"], []).append(summary)
    reverse_slots = {number: slot for slot, number in plan.slot_to_window_no.items()}
    watermarks = []
    for window_no in sorted(by_window):
        items = by_window[window_no]
        slot = reverse_slots[window_no]
        start = plan.origin_ms + slot * plan.slide_ms
        end = start + plan.length_ms
        instance_digests = sorted(item["request_set_digest"] for item in summary_by_window.get(window_no, []))
        watermark = {
            "window_no": window_no,
            "slot_index": slot,
            "watermark_clock_ms": max(int(item["logical_clock_ms"]) for item in items),
            "request_count": len(items),
            "window_start_ms": start,
            "window_end_ms": end,
            "instance_summary_digests": instance_digests,
        }
        watermark["summary_digest"] = sha256_object(
            {k: v for k, v in watermark.items() if k != "summary_digest"}
        )
        watermarks.append(watermark)
    return watermarks


class CapacityEngine:
    def __init__(
        self,
        input_data: Any,
        input_root: str | Path,
        out_dir: str | Path,
        *,
        crash_at: str | None = None,
        crash_patch: str | None = None,
    ):
        self.input = input_data
        self.input_root = Path(input_root)
        self.out_dir = Path(out_dir)
        self.crash_at = crash_at
        self.crash_patch = crash_patch
        self.store: StateStore | None = None
        self.manifest = input_manifest(self.input_root)

    def run(self) -> dict[str, Any]:
        store = StateStore(self.out_dir)
        self.store = store
        with store.exclusive_lock():
            store.initialize()
            baseline = self._ensure_baseline()
            current = baseline
            revision_summaries = [self._revision_summary(baseline)]
            for patch_index, patch in enumerate(self.input.patches, start=1):
                revision_id = str(patch["patch_id"])
                if store.is_revision_committed(revision_id):
                    current = store.load_report(revision_id)
                    self._write_report_file(current)
                    revision_summaries.append(self._revision_summary(current))
                    continue
                current = self._apply_patch(current, patch, patch_index)
                revision_summaries.append(self._revision_summary(current))
            run_report = {
                "ok": True,
                "input_digest": sha256_object(self.manifest),
                "output_dir": str(self.out_dir),
                "database": str(store.db_path),
                "revision_count": len(revision_summaries),
                "revisions": revision_summaries,
                "final_revision_id": current["revision_id"],
                "final_report": str(self._report_path(current["revision_id"])),
            }
            atomic_write_json(self.out_dir / "run_summary.json", run_report)
            return run_report

    def _report_path(self, revision_id: str) -> Path:
        return self.out_dir / "reports" / f"{revision_id}.json"

    def _write_report_file(self, report: dict[str, Any]) -> None:
        atomic_write_json(self._report_path(report["revision_id"]), report)

    def _crash_now(self, stage: str) -> None:
        exit_code = int(self.input.anomalies.get("crash_exit_code", 86))
        message = f"injected crash at {stage}\n"
        sys.stderr.write(message)
        sys.stderr.flush()
        os._exit(exit_code)

    def _claim_stage(self, unit_key: str, revision_id: str, stage: str, clock: int) -> bool:
        assert self.store is not None
        return self.store.claim_unit(
            unit_key=unit_key,
            revision_id=revision_id,
            stage=stage,
            input_hash=sha256_object(self.manifest),
            logical_clock_ms=clock,
        )

    def _maybe_crash_after_claim(self, stage: str, patch_id: str | None = None) -> None:
        if self.crash_at != stage:
            return
        if stage == "patch_commit" and self.crash_patch and patch_id != self.crash_patch:
            return
        self._crash_now(stage)

    def _unit_states(self) -> list[dict[str, Any]]:
        assert self.store is not None
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT unit_key, revision_id, stage, state, attempt, result_hash FROM work_units ORDER BY unit_key"
            ).fetchall()
        return [dict(row) for row in rows]

    def _ensure_baseline(self) -> dict[str, Any]:
        assert self.store is not None
        revision_id = "baseline"
        if self.store.is_revision_committed(revision_id):
            report = self.store.load_report(revision_id)
            self._write_report_file(report)
            return report

        clock = max(int(record["logical_clock_ms"]) for record in self.input.traces)
        metric_definitions = self.input.metric_definitions
        plan = build_window_plan(self.input.traces, metric_definitions)
        assignments, summaries = assign_windows(self.input.traces, plan)
        watermarks = build_window_watermarks(assignments, summaries, plan)

        window_key = "baseline:window_aggregation"
        window_committed = self._claim_stage(window_key, revision_id, "window_aggregation", clock)
        if not window_committed:
            self._maybe_crash_after_claim("window_aggregation")
            window_report = {
                "revision_id": revision_id,
                "window_assignments": assignments,
                "window_summaries": summaries,
                "window_watermarks": watermarks,
            }
            self.store.save_window_stage(window_report)
            self.store.complete_unit(window_key, sha256_object(watermarks))

        conversion_key = "baseline:fixedpoint_conversion"
        conversion_committed = self._claim_stage(conversion_key, revision_id, "fixedpoint_conversion", clock)
        values, ledger = evaluate_metric_values(self.input, metric_definitions)
        assert_precision_budget(values)
        if not conversion_committed:
            self._maybe_crash_after_claim("fixedpoint_conversion")
            self.store.save_conversion_stage(
                {
                    "revision_id": revision_id,
                    "evaluation": {"values": values, "ledger_entries": ledger.entries},
                }
            )
            self.store.complete_unit(conversion_key, sha256_object([item["value_digest"] for item in values]))

        threshold_key = "baseline:threshold_merge"
        threshold_committed = self._claim_stage(threshold_key, revision_id, "threshold_merge", clock)
        threshold_merge = merge_thresholds(self.input)
        if not threshold_committed:
            self._maybe_crash_after_claim("threshold_merge")
            self.store.save_threshold_stage({"revision_id": revision_id, "threshold_merge": threshold_merge})
            self.store.complete_unit(threshold_key, threshold_merge["merge_digest"])

        conclusions = build_conclusions(values, threshold_merge["thresholds"], metric_definitions)
        sensitivity = build_sensitivity(values, conclusions, metric_definitions)
        error_chains = error_chain_report(values, ledger)
        window_error_summaries = cumulative_window_error_report(error_chains)
        decimal_truth = decimal_full_recompute(self.input, metric_definitions)
        evaluation = {
            "values": values,
            "ledger_entries": ledger.entries,
            "conclusions": conclusions,
            "sensitivity_assessments": sensitivity,
            "sensitive_records": [item for item in sensitivity if item["sensitive"]],
            "inflection_records": [item for item in conclusions if item["inflection_triggered"]],
            "error_chains": error_chains,
            "window_error_summaries": window_error_summaries,
            "decimal_full_recompute": decimal_truth,
        }
        report = {
            "schema": "capacity-evaluation-report/v1",
            "revision_id": revision_id,
            "parent_revision_id": None,
            "patch_id": None,
            "kind": "baseline",
            "input_manifest": self.manifest,
            "input_digest": sha256_object(self.manifest),
            "logical_clock_watermark": clock,
            "metric_definitions": metric_definitions,
            "window_plan": asdict(plan) | {"slot_to_window_no": plan.slot_to_window_no},
            "window_assignments": assignments,
            "window_summaries": summaries,
            "window_watermarks": watermarks,
            "threshold_merge": threshold_merge,
            "evaluation": evaluation,
            "recovery_contract": {
                "checkpoint_table": "work_units",
                "atomic_tables": [
                    "window_watermarks",
                    "window_summaries",
                    "threshold_view",
                    "invalidation_records",
                    "sensitivity_records",
                    "logical_clock_watermark",
                ],
                "ledger_idempotency_key": "(revision_id, entry_key)",
            },
        }
        report["report_digest"] = sha256_object({k: v for k, v in report.items() if k != "report_digest"})
        self.store.save_revision_report(report)
        self._write_report_file(report)
        return report

    def _apply_patch(self, parent: dict[str, Any], patch: dict[str, Any], patch_index: int) -> dict[str, Any]:
        assert self.store is not None
        revision_id = str(patch["patch_id"])
        clock = max(int(parent["logical_clock_watermark"]), int(patch["logical_clock_ms"]))
        unit_key = f"patch_commit:{revision_id}"
        unit_committed = self._claim_stage(unit_key, revision_id, "patch_commit", clock)
        if not unit_committed:
            self._maybe_crash_after_claim("patch_commit", revision_id)

        effective_metrics = parent["metric_definitions"]
        if patch["kind"] == "metric_parameter_update":
            effective_metrics = apply_metric_parameter_patch(parent["metric_definitions"], patch)

        parent_merge = parent["threshold_merge"]
        effective_thresholds = dict(parent_merge["thresholds"])
        effective_edges = list(parent_merge["dependency_edges"])
        if patch["kind"] in {"threshold_update", "tombstone"}:
            effective_thresholds = apply_threshold_patch(effective_thresholds, patch)
        elif patch["kind"] == "dependency_change":
            effective_edges = apply_dependency_patch(effective_edges, patch)

        invalidation = plan_invalidation(
            patch,
            parent["evaluation"]["values"],
            parent["evaluation"]["sensitivity_assessments"],
            parent_merge["dependency_edges"],
        )

        only_keys: set[tuple[int, str, str]] = set()
        for value_key in invalidation["recompute"]["metric_values"]:
            obs_key, metric = value_key.rsplit(":", 1)
            window_part, instance_id = obs_key.split(":", 1)
            only_keys.add((int(window_part[1:]), instance_id, metric))

        reuse_ledger_keys = set(invalidation["reuse"]["error_ledger"])
        reused_ledger_entries = [
            entry
            for entry in parent["evaluation"]["ledger_entries"]
            if f"W{entry['window_no']}:{entry['instance_id']}:{entry['metric']}" in reuse_ledger_keys
        ]
        incremental_ledger = Ledger(entries=reused_ledger_entries)
        incremental_ledger.seed_seq(
            max((int(entry["seq"]) for entry in reused_ledger_entries), default=0)
        )
        values, ledger = evaluate_metric_values(
            self.input,
            effective_metrics,
            ledger=incremental_ledger,
            only_keys=only_keys,
            prior_values=parent["evaluation"]["values"],
        )
        assert_precision_budget(values)

        fresh_conclusions = build_conclusions(values, effective_thresholds, effective_metrics)
        fresh_conclusion_by_key = {
            f"{item['obs_key']}:{item['metric']}": item for item in fresh_conclusions
        }
        parent_conclusion_by_key = {
            f"{item['obs_key']}:{item['metric']}": item
            for item in parent["evaluation"]["conclusions"]
        }
        conclusions: list[dict[str, Any]] = []
        reuse_conclusion_keys = set(invalidation["reuse"]["conclusions"])
        for key, fresh in sorted(fresh_conclusion_by_key.items()):
            if key in reuse_conclusion_keys:
                conclusions.append(parent_conclusion_by_key[key])
            else:
                conclusions.append(fresh)
        conclusions.sort(key=lambda item: (item["window_no"], item["instance_id"], item["metric"]))

        fresh_sensitivity = build_sensitivity(values, conclusions, effective_metrics)
        parent_sensitivity_by_key = {
            item["sensitivity_key"]: item
            for item in parent["evaluation"]["sensitivity_assessments"]
        }
        sensitivity: list[dict[str, Any]] = []
        reuse_sensitivity_keys = set(invalidation["reuse"]["sensitivity"])
        for fresh in fresh_sensitivity:
            key = fresh["sensitivity_key"]
            sensitivity.append(parent_sensitivity_by_key[key] if key in reuse_sensitivity_keys else fresh)
        sensitivity.sort(key=lambda item: item["sensitivity_key"])

        error_chains = error_chain_report(values, ledger)
        window_error_summaries = cumulative_window_error_report(error_chains)
        decimal_truth = decimal_full_recompute(self.input, effective_metrics)
        evaluation = {
            "values": values,
            "ledger_entries": ledger.entries,
            "conclusions": conclusions,
            "sensitivity_assessments": sensitivity,
            "sensitive_records": [item for item in sensitivity if item["sensitive"]],
            "inflection_records": [item for item in conclusions if item["inflection_triggered"]],
            "error_chains": error_chains,
            "window_error_summaries": window_error_summaries,
            "decimal_full_recompute": decimal_truth,
        }

        patch_decision = {
            "threshold_key": patch.get("target", {}).get(
                "metric", f"dependency:{patch.get('dependency', {}).get('edge_key', patch['patch_id'])}"
            ),
            "winner_candidate_id": f"patch:{patch['patch_id']}",
            "decision_rule": "committed_patch_logical_clock_overrides_prior_snapshot",
            "reason": f"patch_{patch['kind']}_committed",
            "candidates": [],
        }
        threshold_merge = {
            **parent_merge,
            "thresholds": effective_thresholds,
            "dependency_edges": effective_edges,
            "dependency_edges_digest": sha256_object(effective_edges),
            "decisions": parent_merge["decisions"] + [patch_decision],
            "patch_decisions": parent_merge.get("patch_decisions", []) + [patch_decision],
        }
        threshold_merge["merge_digest"] = sha256_object(
            {
                "thresholds": effective_thresholds,
                "decisions": threshold_merge["decisions"],
                "edges": effective_edges,
                "edge_digest": threshold_merge["dependency_edges_digest"],
            }
        )

        report = {
            "schema": "capacity-evaluation-report/v1",
            "revision_id": revision_id,
            "parent_revision_id": parent["revision_id"],
            "patch_id": revision_id,
            "patch": patch,
            "patch_index": patch_index,
            "kind": "patch",
            "input_manifest": self.manifest,
            "input_digest": sha256_object(self.manifest),
            "logical_clock_watermark": clock,
            "metric_definitions": effective_metrics,
            "window_plan": parent["window_plan"],
            "window_assignments": parent["window_assignments"],
            "window_summaries": parent["window_summaries"],
            "window_watermarks": parent["window_watermarks"],
            "threshold_merge": threshold_merge,
            "invalidation": invalidation,
            "evaluation": evaluation,
            "recovery_contract": {
                "checkpoint_table": "work_units",
                "patch_unit_key": unit_key,
                "ledger_idempotency_key": "(revision_id, entry_key)",
            },
        }
        reuse_checks = reused_digests_match(invalidation, parent, report)
        report["reuse_checks"] = reuse_checks
        failed_checks = [check for check in reuse_checks if not check["unchanged"]]
        if failed_checks:
            raise CapacityError(
                ErrorCode.INTERNAL,
                "invalidation reuse digest check failed",
                {"failed_checks": failed_checks},
            )
        report["report_digest"] = sha256_object({k: v for k, v in report.items() if k != "report_digest"})
        self.store.save_revision_report(report)
        self._write_report_file(report)
        return report

    def _revision_summary(self, report: dict[str, Any]) -> dict[str, Any]:
        evaluation = report["evaluation"]
        return {
            "revision_id": report["revision_id"],
            "parent_revision_id": report.get("parent_revision_id"),
            "patch_id": report.get("patch_id"),
            "report_digest": report["report_digest"],
            "logical_clock_watermark": report["logical_clock_watermark"],
            "observation_count": len(evaluation["values"]),
            "conclusion_count": len(evaluation["conclusions"]),
            "sensitive_count": len(evaluation["sensitive_records"]),
            "ledger_entry_count": len(evaluation["ledger_entries"]),
            "invalidated_recompute": report.get("invalidation", {}).get("recompute") if report.get("invalidation") else None,
            "invalidated_reuse_counts": {
                key: len(items)
                for key, items in report.get("invalidation", {}).get("reuse", {}).items()
            }
            if report.get("invalidation")
            else None,
        }
