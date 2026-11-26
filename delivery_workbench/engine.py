from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .canon import canonical_hash
from .errors import Code, WorkbenchError
from .loader import load_bundle
from .patches import assign_change_sequences, build_patch_plan
from .scheduler import schedule_diagnostics
from .semantics import (
    apply_package_to_view,
    decide_package,
    diff_views,
    load_package,
    merge_snapshots,
)
from .storage import Storage
from .watermark import closure_from_roots, compute_all_watermarks, compute_incremental_watermarks


DEFAULT_INJECTION_CLOCK_MS = 5000
DEFAULT_AS_OF_CLOCK_MS = 8000
DEFAULT_TIME_BUDGET_MS = 100_000
DEFAULT_CONCURRENCY = 8


def crash_if(point: str) -> None:
    if os.environ.get("WB_CRASH_POINT") == point:
        os.kill(os.getpid(), signal.SIGKILL)


def max_input_clock(bundle: Dict[str, Any]) -> int:
    values = [event["logical_clock_ms"] for event in bundle["events"]]
    values.append(bundle["v1"]["declared_clock_ms"])
    values.append(bundle["v2"]["declared_clock_ms"])
    return max(values)


class Engine:
    def __init__(self, sample_dir: str | Path = "samples", db_path: str | Path = "state/workbench.sqlite3") -> None:
        self.sample_dir = Path(sample_dir)
        self.db_path = Path(db_path)
        self.storage = Storage(self.db_path)

    def load(self) -> Dict[str, Any]:
        bundle = load_bundle(self.sample_dir)
        self.storage.initialize()
        self.storage.save_bundle(bundle, max_input_clock(bundle))
        return {
            "input_hash": bundle["input_hash"],
            "topics": len(bundle["topics"]),
            "events": len(bundle["events"]),
            "subscriptions": len(bundle["subscriptions"]),
            "v1_entries": len(bundle["v1"]["entries"]),
            "v2_entries": len(bundle["v2"]["entries"]),
            "v2_shards": len(bundle["v2"]["shards"]),
            "logical_clock_watermark": max_input_clock(bundle),
        }

    def recover_prepared(self) -> List[Dict[str, Any]]:
        self.storage.initialize()
        results = []
        for checkpoint_id in self.storage.prepared_checkpoint_ids():
            results.append(self.storage.commit_prepared_checkpoint(checkpoint_id))
        return results

    def ensure_merged(
        self,
        injection_clock_ms: int = DEFAULT_INJECTION_CLOCK_MS,
        package_path: Optional[str | Path] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        self.storage.initialize()
        bundle = load_bundle(self.sample_dir)
        current_view = self.storage.latest_view()
        current_binding = self.storage.current_binding()
        if package_path is not None:
            package = load_package(package_path)
            decision = decide_package(current_binding, package)
            if decision["decision"] == "idempotent":
                if current_view is None or current_binding is None:
                    raise WorkbenchError(Code.STATE_CORRUPT, "idempotent package reported without current view")
                return bundle, current_view, current_binding
            base_view = current_view or merge_snapshots(bundle, injection_clock_ms)
            new_view = apply_package_to_view(base_view, package, decision)
            raw_changes = diff_views(base_view, new_view)
            seq_state = self.storage.change_seq_state()
            changes = assign_change_sequences(raw_changes, seq_state["by_topic"], seq_state["global"])
            binding = {
                "binding_id": f"SB-{package['snapshot_version']}-{package['manifest_hash'][:12]}",
                "binding_kind": "versioned_v2_package",
                "snapshot_version": package["snapshot_version"],
                "generation_min": package["generation_min"],
                "manifest_hash": package["manifest_hash"],
                "package_id": package["package_id"],
                "base_snapshot_version": package.get("base_snapshot_version"),
                "decision": decision,
                "view_id": new_view["view_id"],
                "content_hash": new_view["content_hash"],
                "shard_hashes": {f"shard-{item['ordinal']:03d}": item["sha256"] for item in package["shards"]},
                "applied_clock_ms": package["declared_clock_ms"],
            }
            closure = self._closure_record(new_view, changes, binding, "incremental_patch")
            crash_if("merge_after_compute_before_write")
            self.storage.save_merge_view(
                new_view,
                binding,
                max(injection_clock_ms, package["declared_clock_ms"]),
                changes,
                [closure],
            )
            return bundle, new_view, binding
        if current_view is not None and current_binding is not None:
            return bundle, current_view, current_binding
        view = merge_snapshots(bundle, injection_clock_ms)
        changes = assign_change_sequences(diff_views(None, view))
        binding = {
            "binding_id": f"SB-{bundle['v2']['snapshot_version']}-{bundle['v2']['manifest_hash'][:12]}",
            "binding_kind": "dual_format_merge",
            "snapshot_version": bundle["v2"]["snapshot_version"],
            "generation_min": bundle["v2"]["generation_min"],
            "manifest_hash": bundle["v2"]["manifest_hash"],
            "view_id": view["view_id"],
            "content_hash": view["content_hash"],
            "v1_snapshot_id": bundle["v1"]["snapshot_id"],
            "v2_snapshot_id": bundle["v2"]["snapshot_id"],
            "shard_hashes": {shard["shard_id"]: shard["sha256"] for shard in bundle["v2"]["shards"]},
            "applied_clock_ms": injection_clock_ms,
        }
        closure = self._closure_record(view, changes, binding, "initial_full_dependency_closure")
        crash_if("merge_after_compute_before_write")
        self.storage.save_merge_view(view, binding, injection_clock_ms, changes, [closure])
        return bundle, view, binding

    def merge(
        self,
        injection_clock_ms: int = DEFAULT_INJECTION_CLOCK_MS,
        package_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        self.recover_prepared()
        bundle, view, binding = self.ensure_merged(injection_clock_ms, package_path)
        return {
            "view_id": view["view_id"],
            "content_hash": view["content_hash"],
            "binding": binding,
            "entry_count": sum(1 for entry in view["entries"] if entry.get("present")),
            "missing_keys": view["missing_keys"],
            "injection_clock_ms": view["injection_clock_ms"],
            "adjudications": [
                {
                    "message_key": entry["message_key"],
                    "source": entry.get("source"),
                    "reason": entry.get("adjudication", {}).get("reason"),
                    "generation_rollback": entry.get("adjudication", {}).get("generation_rollback_against_other_format", False),
                }
                for entry in view["entries"]
                if entry.get("present") and len(entry.get("adjudication", {}).get("candidates", [])) == 2
            ],
        }

    def diagnose(
        self,
        as_of_clock_ms: int = DEFAULT_AS_OF_CLOCK_MS,
        time_budget_ms: int = DEFAULT_TIME_BUDGET_MS,
        concurrency: int = DEFAULT_CONCURRENCY,
        subscriber_filter: Optional[str] = None,
        injection_clock_ms: int = DEFAULT_INJECTION_CLOCK_MS,
    ) -> Dict[str, Any]:
        recovered = self.recover_prepared()
        bundle, view, binding = self.ensure_merged(injection_clock_ms)
        all_records, watermark_writes, watermark_meta = self._watermark_records(bundle, view, binding, as_of_clock_ms)
        changes = self.storage.list_change_events()
        closure = self._closure_for_binding(binding["binding_id"])
        cursors = self.storage.cursor_map()
        patches = build_patch_plan(
            bundle,
            view,
            changes,
            cursors,
            closure,
            binding["binding_id"],
            subscriber_filter,
        )
        cursor_updates = self._cursor_updates(patches, view)
        run = schedule_diagnostics(bundle, all_records, as_of_clock_ms, time_budget_ms, concurrency, subscriber_filter)
        has_core_writes = bool(watermark_writes or cursor_updates)
        if has_core_writes:
            plan = self._build_checkpoint_plan(
                as_of_clock_ms,
                binding,
                view,
                changes,
                closure,
                watermark_writes,
                patches,
                cursor_updates,
                run,
                subscriber_filter,
            )
            commit = self._persist_checkpoint(plan)
        else:
            self.storage.save_diagnostic_run(run)
            commit = {"status": "diagnostic_run_idempotent", "checkpoint_id": None}
        return {
            "recovered": recovered,
            "commit": commit,
            "watermark_recompute": watermark_meta,
            "watermarks": [all_records[key] for key in sorted(all_records)],
            "patches": patches,
            "cursor_updates": cursor_updates,
            "diagnostic_run": run,
        }

    def patch(
        self,
        subscriber_filter: Optional[str] = None,
        injection_clock_ms: int = DEFAULT_INJECTION_CLOCK_MS,
        as_of_clock_ms: int = DEFAULT_AS_OF_CLOCK_MS,
    ) -> Dict[str, Any]:
        recovered = self.recover_prepared()
        bundle, view, binding = self.ensure_merged(injection_clock_ms)
        all_records, watermark_writes, watermark_meta = self._watermark_records(bundle, view, binding, as_of_clock_ms)
        changes = self.storage.list_change_events()
        closure = self._closure_for_binding(binding["binding_id"])
        cursors = self.storage.cursor_map()
        patches = build_patch_plan(bundle, view, changes, cursors, closure, binding["binding_id"], subscriber_filter)
        if not patches:
            stored = self.storage.status().get("patches", [])
            replayed = [
                patch
                for patch in stored
                if patch.get("semantic_view_hash") == view["content_hash"]
                and (subscriber_filter is None or patch.get("subscriber_id") == subscriber_filter)
            ]
            return {
                "recovered": recovered,
                "replayed": True,
                "patches": sorted(replayed, key=lambda item: item["patch_id"]),
                "cursor_updates": [],
                "watermark_recompute": watermark_meta,
            }
        cursor_updates = self._cursor_updates(patches, view)
        plan = self._build_checkpoint_plan(
            as_of_clock_ms,
            binding,
            view,
            changes,
            closure,
            watermark_writes,
            patches,
            cursor_updates,
            None,
            subscriber_filter,
        )
        commit = self._persist_checkpoint(plan)
        return {
            "recovered": recovered,
            "replayed": False,
            "commit": commit,
            "patches": patches,
            "cursor_updates": cursor_updates,
            "watermark_recompute": watermark_meta,
        }

    def resume(self) -> Dict[str, Any]:
        self.storage.initialize()
        recovered = self.recover_prepared()
        replayed = None
        if (self.sample_dir / "topics.json").is_file():
            diagnosis = self.diagnose()
            replayed = {
                "checkpoint": diagnosis["commit"],
                "partition_watermarks": len(diagnosis["watermarks"]),
                "patches": len(diagnosis["patches"]),
                "diagnostic_run_id": diagnosis["diagnostic_run"]["run_id"],
            }
        status = self.storage.status()
        return {
            "recovered_checkpoints": recovered,
            "replayed_missing_parts": replayed,
            "prepared_remaining": self.storage.prepared_checkpoint_ids(),
            "logical_clock_watermark": status["logical_clock_watermark"],
            "partition_watermark_count": len(status["partition_watermarks"]),
            "subscription_cursor_count": len(status["subscription_cursors"]),
            "patch_count": len(status["patches"]),
            "conclusion": "all_prepared_checkpoints_committed" if not self.storage.prepared_checkpoint_ids() else "half_committed_records_remain",
        }

    def status(self) -> Dict[str, Any]:
        self.storage.initialize()
        return self.storage.status()

    def _watermark_records(
        self,
        bundle: Dict[str, Any],
        view: Dict[str, Any],
        binding: Dict[str, Any],
        as_of_clock_ms: int,
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]:
        state_rows = self.storage.partition_watermark_state()
        committed_keys = self.storage.partition_commit_keys()
        previous = {row["partition_key"]: __import__("json").loads(row["content_json"]) for row in state_rows}
        if not previous:
            records = compute_all_watermarks(bundle, view, as_of_clock_ms)
            writes = {
                pk: record
                for pk, record in records.items()
                if (pk, binding["binding_id"]) not in committed_keys
            }
            return records, writes, {"mode": "full", "recomputed_partitions": sorted(records), "reused_partitions": []}
        closure = self._closure_for_binding(binding["binding_id"])
        if closure is None or closure.get("closure_kind") == "initial_full_dependency_closure":
            computed = compute_all_watermarks(bundle, view, as_of_clock_ms)
            candidate_writes = computed
            meta = {"mode": "full_due_to_missing_incremental_closure", "recomputed_partitions": sorted(computed), "reused_partitions": []}
        else:
            result = compute_incremental_watermarks(bundle, view, closure["root_keys"], previous, as_of_clock_ms)
            computed = result["all_records"]
            candidate_writes = result["records"]
            meta = {
                "mode": "incremental_closure",
                "closure_id": closure["closure_id"],
                "recomputed_partitions": result["affected_partitions"],
                "reused_partitions": result["reused_partitions"],
            }
        writes = {
            pk: record
            for pk, record in candidate_writes.items()
            if (pk, binding["binding_id"]) not in committed_keys
        }
        return computed, writes, meta

    def _cursor_updates(self, patches: List[Dict[str, Any]], view: Dict[str, Any]) -> List[Dict[str, Any]]:
        updates: List[Dict[str, Any]] = []
        by_subscriber: Dict[str, List[Dict[str, Any]]] = {}
        for patch in patches:
            by_subscriber.setdefault(patch["subscriber_id"], []).append(patch)
        for subscriber_id, subscriber_patches in sorted(by_subscriber.items()):
            ordered = sorted(subscriber_patches, key=lambda patch: (patch["from_topic_seq"], patch["to_topic_seq"], patch["patch_id"]))
            from_seq = min(patch["from_topic_seq"] for patch in ordered)
            to_seq = max(patch["to_topic_seq"] for patch in ordered)
            topic = ordered[0]["topic"]
            patch_ids = [patch["patch_id"] for patch in ordered]
            cursor_record = {
                "subscriber_id": subscriber_id,
                "topic": topic,
                "last_topic_seq": to_seq,
                "from_topic_seq": from_seq,
                "patch_count": len(ordered),
                "patch_ids": patch_ids,
                "semantic_view_hash": view["content_hash"],
            }
            updates.append(
                {
                    "subscriber_id": subscriber_id,
                    "topic": topic,
                    "from_seq": from_seq,
                    "to_seq": to_seq,
                    "patch_set_hash": canonical_hash(patch_ids),
                    "cursor_record": cursor_record,
                }
            )
        return updates

    def _build_checkpoint_plan(
        self,
        as_of_clock_ms: int,
        binding: Dict[str, Any],
        view: Dict[str, Any],
        changes: List[Dict[str, Any]],
        closure: Optional[Dict[str, Any]],
        watermark_writes: Dict[str, Dict[str, Any]],
        patches: List[Dict[str, Any]],
        cursor_updates: List[Dict[str, Any]],
        diagnostic_run: Optional[Dict[str, Any]],
        subscriber_filter: Optional[str],
    ) -> Dict[str, Any]:
        plan: Dict[str, Any] = {
            "plan_schema": "delivery-checkpoint-plan/v1",
            "as_of_clock_ms": as_of_clock_ms,
            "logical_clock_watermark": max(as_of_clock_ms, int(binding.get("applied_clock_ms", 0) or 0)),
            "subscriber_filter": subscriber_filter,
            "semantic_binding": binding,
            "view": view,
            "change_events": changes,
            "closures": [] if closure is None else [closure],
            "watermark_records": watermark_writes,
            "patches": patches,
            "cursor_updates": cursor_updates,
            "diagnostic_run": diagnostic_run,
        }
        plan["plan_hash"] = canonical_hash(plan)
        plan["checkpoint_id"] = f"CP-{plan['plan_hash'][:20]}"
        return plan

    def _persist_checkpoint(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        crash_if("before_prepare")
        self.storage.prepare_checkpoint(plan)
        crash_if("checkpoint_after_prepare")
        result = self.storage.commit_prepared_checkpoint(plan["checkpoint_id"])
        crash_if("checkpoint_after_commit")
        return result

    def _closure_record(
        self,
        view: Dict[str, Any],
        changes: List[Dict[str, Any]],
        binding: Dict[str, Any],
        kind: str,
    ) -> Dict[str, Any]:
        roots = [change["message_key"] for change in changes]
        closure = closure_from_roots(roots, {entry["message_key"]: entry for entry in view["entries"]})
        record = {
            "closure_id": f"CL-{binding['snapshot_version']}-{binding['manifest_hash'][:12]}-{kind}",
            "closure_kind": kind,
            "binding_id": binding["binding_id"],
            "trigger_version": binding.get("package_id") or binding["binding_id"],
            "trigger_clock_ms": binding.get("applied_clock_ms"),
            "change_summary": {
                "total": len(changes),
                "added": sum(change["change_kind"] == "added" for change in changes),
                "modified": sum(change["change_kind"] == "modified" for change in changes),
                "invalidated": sum(change["change_kind"] == "invalidated" for change in changes),
            },
            **closure,
        }
        record["content_hash"] = canonical_hash({key: value for key, value in record.items() if key != "content_hash"})
        return record

    def _closure_for_binding(self, binding_id: str) -> Optional[Dict[str, Any]]:
        for closure in self.storage.status().get("invalidation_event_chains", []):
            if closure.get("binding_id") == binding_id:
                return closure
        return None


def canonical_diagnostic_report(
    bundle: Dict[str, Any],
    injection_clock_ms: int = DEFAULT_INJECTION_CLOCK_MS,
    as_of_clock_ms: int = DEFAULT_AS_OF_CLOCK_MS,
    time_budget_ms: int = DEFAULT_TIME_BUDGET_MS,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> Dict[str, Any]:
    view = merge_snapshots(bundle, injection_clock_ms)
    changes = assign_change_sequences(diff_views(None, view))
    closure = closure_from_roots([change["message_key"] for change in changes], {e["message_key"]: e for e in view["entries"]})
    watermarks = compute_all_watermarks(bundle, view, as_of_clock_ms)
    semantic_version_token = f"SB-{bundle['v2']['snapshot_version']}-{bundle['v2']['manifest_hash'][:12]}"
    patches = build_patch_plan(bundle, view, changes, {}, None, semantic_version_token)
    run = schedule_diagnostics(bundle, watermarks, as_of_clock_ms, time_budget_ms, concurrency)
    cursors = [
        {
            "subscriber_id": update["subscriber_id"],
            "topic": update["topic"],
            "last_topic_seq": update["to_seq"],
            "from_topic_seq": update["from_seq"],
            "patch_count": update["cursor_record"]["patch_count"],
            "patch_ids": update["cursor_record"]["patch_ids"],
            "semantic_view_hash": view["content_hash"],
        }
        for update in _canonical_cursor_updates(patches, view)
    ]
    return {
        "view": view,
        "changes": changes,
        "closure": closure,
        "partition_watermarks": [watermarks[key] for key in sorted(watermarks)],
        "subscription_cursors": sorted(cursors, key=lambda item: item["subscriber_id"]),
        "patches": patches,
        "diagnostic_run": run,
    }


def _canonical_cursor_updates(patches: List[Dict[str, Any]], view: Dict[str, Any]) -> List[Dict[str, Any]]:
    updates: List[Dict[str, Any]] = []
    by_subscriber: Dict[str, List[Dict[str, Any]]] = {}
    for patch in patches:
        by_subscriber.setdefault(patch["subscriber_id"], []).append(patch)
    for subscriber_id, subscriber_patches in sorted(by_subscriber.items()):
        ordered = sorted(subscriber_patches, key=lambda patch: (patch["from_topic_seq"], patch["to_topic_seq"]))
        updates.append({"subscriber_id": subscriber_id, "topic": ordered[0]["topic"], "from_seq": min(p["from_topic_seq"] for p in ordered), "to_seq": max(p["to_topic_seq"] for p in ordered), "cursor_record": {"patch_count": len(ordered), "patch_ids": [p["patch_id"] for p in ordered]}})
    return updates
