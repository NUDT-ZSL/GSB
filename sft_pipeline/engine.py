from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .errors import (
    BusinessError,
    E_RESOURCE_LIMIT,
    E_STATE_INCONSISTENT,
)
from .reports import read_json_report, write_json_report
from .stages import (
    STAGE_LABELS,
    STAGE_NAMES,
    execute_dedupe,
    execute_export,
    execute_ingest,
    execute_normalize,
    execute_quality,
    execute_sample,
    signature_similarity,
)
from .storage import SnapshotStore, committed_stages
from .util import canonical_json_bytes, read_json_file, sha256_bytes, sha256_json, sha256_file
from .validation import ConfigBundle, load_bundle
from .workspace import Workspace


ALGORITHM_VERSION = "sft-pipeline-1.0.0"
ProgressHook = Callable[[str, int, int], None]
AfterStageHook = Callable[[str, dict[str, Any]], bool]


@dataclass
class RunResult:
    mode: str
    started_with_stages: list[str]
    recomputed_stages: list[str]
    cache_hit_stages: list[str]
    reused_snapshot_stages: list[str]
    invalidated_from: str | None
    stage_reports: list[dict[str, Any]]
    stage_diffs: dict[str, dict[str, Any]]
    degradations: list[dict[str, Any]]
    stopped_gracefully: bool
    completed: bool
    final_export: dict[str, Any] | None

    @property
    def content_changed_stages(self) -> list[str]:
        return [
            stage
            for stage, diff in self.stage_diffs.items()
            if diff["content_changed"]
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": self.mode,
            "started_with_stages": self.started_with_stages,
            "recomputed_stages": self.recomputed_stages,
            "cache_hit_stages": self.cache_hit_stages,
            "reused_snapshot_stages": self.reused_snapshot_stages,
            "invalidated_from": self.invalidated_from,
            "stage_reports": self.stage_reports,
            "stage_diffs": self.stage_diffs,
            "content_changed_stages": self.content_changed_stages,
            "degradations": self.degradations,
            "stopped_gracefully": self.stopped_gracefully,
            "completed": self.completed,
            "final_export": self.final_export,
        }


def _stage_scope_digest(bundle: ConfigBundle, stage: str) -> str:
    if stage == "ingest":
        return bundle.ingest_scope_digest
    if stage == "normalize":
        return bundle.templates_digest
    if stage == "quality":
        return bundle.quality_scope_digest
    if stage == "dedupe":
        return bundle.dedupe_scope_digest
    if stage == "sample":
        return bundle.quotas_digest
    if stage == "export":
        return bundle.export_scope_digest
    raise BusinessError(E_STATE_INCONSISTENT, "unknown stage", stage=stage)


def _cache_key_for(
    bundle: ConfigBundle,
    stage: str,
    upstream_or_source: str | None,
) -> tuple[str, str, str]:
    dependency_digest = _stage_scope_digest(bundle, stage)
    base_input = (
        bundle.samples_digest if stage == "ingest" else upstream_or_source
    )
    key = sha256_json(
        {
            "algorithm_version": ALGORITHM_VERSION,
            "stage": stage,
            "base_input": base_input,
            "dependency_digest": dependency_digest,
        }
    )
    return key, dependency_digest, str(base_input)


def _payload_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    if isinstance(records, list):
        return records
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else []


def _input_count_for_stage(stage: str, bundle: ConfigBundle, previous: dict[str, Any] | None) -> int:
    if stage == "ingest":
        return len(bundle.samples)
    if previous is None:
        raise BusinessError(
            E_STATE_INCONSISTENT,
            "missing upstream payload",
            stage=stage,
        )
    if stage == "sample":
        return sum(
            1
            for record in _payload_records(previous)
            if record.get("dedupe", {}).get("kept")
        )
    return len(_payload_records(previous))


def _estimate_memory_mb(
    stage: str,
    count: int,
    *,
    raw_bytes: int,
    previous_bytes: int,
    signature_size: int = 0,
) -> float:
    if count <= 0:
        return 0.0
    if stage == "ingest":
        estimated = raw_bytes * 3 + count * 2048
    elif stage == "dedupe":
        pairs = count * (count - 1) / 2
        estimated = (
            previous_bytes * 3
            + count * signature_size * 16
            + pairs * 48
        )
    else:
        estimated = previous_bytes * 4 + count * 2048
    return round(estimated / (1024 * 1024), 6)


class PipelineEngine:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.workspace.ensure_layout()
        self.store = SnapshotStore(workspace)

    def _enforce_limits(
        self,
        bundle: ConfigBundle,
        stage: str,
        count: int,
        estimate_mb: float,
    ) -> None:
        limits = bundle.pipeline["limits"]
        sample_limit = int(limits["max_samples_per_stage"])
        memory_limit = float(limits["max_resident_memory_mb"])
        if count > sample_limit:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "sample count exceeds the per-stage resource limit",
                field="limits.max_samples_per_stage",
                stage=stage,
                details={"count": count, "limit": sample_limit},
            )
        if estimate_mb > memory_limit:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "estimated resident memory exceeds the configured limit",
                field="limits.max_resident_memory_mb",
                stage=stage,
                details={"estimated_mb": estimate_mb, "limit_mb": memory_limit},
            )

    def _degradation_plan(self, bundle: ConfigBundle) -> list[dict[str, Any]]:
        limits = bundle.pipeline["limits"]
        requested = int(limits["requested_parallel_stages"])
        maximum = int(limits["max_parallel_stages"])
        if requested <= maximum:
            return []
        return [
            {
                "category": "parallel_stages_capped",
                "requested": requested,
                "effective": maximum,
                "strategy": "reduce concurrency, keep stage order stable",
            }
        ]

    def _execute_stage(
        self,
        bundle: ConfigBundle,
        stage: str,
        previous_payload: dict[str, Any] | None,
        progress: ProgressHook | None,
    ) -> dict[str, Any]:
        if stage == "ingest":
            return execute_ingest(bundle, progress)
        if previous_payload is None:
            raise BusinessError(
                E_STATE_INCONSISTENT,
                "missing upstream payload for stage execution",
                stage=stage,
            )
        if stage == "normalize":
            return execute_normalize(previous_payload, bundle.templates, progress)
        if stage == "quality":
            return execute_quality(
                previous_payload, bundle.rules["quality"], progress
            )
        if stage == "dedupe":
            return execute_dedupe(
                previous_payload, bundle.rules["dedupe"], progress
            )
        if stage == "sample":
            return execute_sample(previous_payload, bundle.quotas, progress)
        if stage == "export":
            return execute_export(
                self.workspace,
                previous_payload,
                bundle.pipeline["export"],
                progress,
            )
        raise BusinessError(E_STATE_INCONSISTENT, "unknown stage", stage=stage)

    def _verify_expected_completed_ids(
        self,
        stage: str,
        payload: dict[str, Any],
        previous_payload: dict[str, Any] | None,
    ) -> None:
        completed = payload.get("completed_sample_ids")
        if not isinstance(completed, list) or completed != sorted(set(completed)):
            raise BusinessError(
                E_STATE_INCONSISTENT,
                "completed sample ids must be sorted and unique",
                stage=stage,
            )
        if stage == "ingest":
            expected = [record["sample_id"] for record in payload["records"]]
        elif stage in {"normalize", "quality", "dedupe"}:
            expected = previous_payload["completed_sample_ids"]  # type: ignore[index]
        elif stage == "sample":
            expected = [
                record["sample_id"]
                for record in previous_payload["records"]  # type: ignore[index]
                if record["dedupe"]["kept"]
            ]
        else:
            expected = [
                record["sample_id"]
                for record in previous_payload["records"]  # type: ignore[index]
            ]
        if sorted(expected) != completed:
            raise BusinessError(
                E_STATE_INCONSISTENT,
                "stage did not process the complete expected sample set",
                stage=stage,
                details={
                    "expected_count": len(expected),
                    "completed_count": len(completed),
                },
            )

    def _baseline_payloads(
        self, state: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        for stage in STAGE_NAMES:
            if stage not in state["stages"]:
                break
            snapshot = self.store.read_snapshot(stage, state["stages"][stage])
            payloads[stage] = snapshot["payload"]
        return payloads

    def _reconcile(
        self,
        bundle: ConfigBundle,
        state: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str | None]:
        payloads: dict[str, dict[str, Any]] = {}
        upstream: str | None = None
        invalidated_from: str | None = None
        committed = committed_stages(state)
        for index, stage in enumerate(committed):
            entry = state["stages"][stage]
            snapshot = self.store.read_snapshot(stage, entry)
            payloads[stage] = snapshot["payload"]
            expected_upstream = (
                None if stage == "ingest" else state["stages"][STAGE_NAMES[index - 1]]["checksum"]
            )
            expected_key, _dependency, _base = _cache_key_for(
                bundle,
                stage,
                bundle.samples_digest if stage == "ingest" else expected_upstream,
            )
            if (
                entry.get("upstream_checksum") != expected_upstream
                or snapshot.get("upstream_checksum") != expected_upstream
                or entry.get("cache_key") != expected_key
                or snapshot.get("cache_key") != expected_key
            ):
                invalidated_from = stage
                break
            self.store.publish_snapshot_cache(
                stage, entry, snapshot["payload"]
            )

        if invalidated_from is not None:
            result = self.store.invalidate_from(
                invalidated_from,
                "input or dependency fingerprint changed",
                expected_head=self.store.head_token(state),
            )
            state = self.store.load_manifest()
            payloads = {
                stage: payloads[stage]
                for stage in committed_stages(state)
                if stage in payloads
            }
            if not result.get("invalidated"):
                invalidated_from = None
        return state, payloads, invalidated_from

    def _stage_diffs(
        self,
        baseline: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        baseline_state: dict[str, Any],
        current_state: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        diffs: dict[str, dict[str, Any]] = {}
        for stage in STAGE_NAMES:
            old_payload = baseline.get(stage)
            new_payload = current.get(stage)
            if old_payload is None and new_payload is None:
                continue
            old_records = {
                record["sample_id"]: record
                for record in _payload_records(old_payload or {})
            }
            new_records = {
                record["sample_id"]: record
                for record in _payload_records(new_payload or {})
            }
            affected = sorted(
                identifier
                for identifier in set(old_records) | set(new_records)
                if old_records.get(identifier) != new_records.get(identifier)
            )
            reused = sorted(
                identifier
                for identifier in set(old_records) & set(new_records)
                if old_records[identifier] == new_records[identifier]
            )
            old_version = (
                baseline_state["stages"].get(stage, {}).get("version")
                if stage in baseline_state["stages"]
                else None
            )
            new_version = (
                current_state["stages"].get(stage, {}).get("version")
                if stage in current_state["stages"]
                else None
            )
            old_checksum = (
                baseline_state["stages"].get(stage, {}).get("checksum")
                if stage in baseline_state["stages"]
                else None
            )
            new_checksum = (
                current_state["stages"].get(stage, {}).get("checksum")
                if stage in current_state["stages"]
                else None
            )
            diffs[stage] = {
                "old_version": old_version,
                "new_version": new_version,
                "content_changed": old_checksum != new_checksum,
                "affected_sample_ids": affected,
                "reused_sample_ids": reused,
                "affected_count": len(affected),
                "reused_count": len(reused),
            }
        return diffs

    def run(
        self,
        *,
        progress_hook: ProgressHook | None = None,
        after_stage_hook: AfterStageHook | None = None,
    ) -> RunResult:
        with self.store.operation_lock:
            return self._run_locked(
                progress_hook=progress_hook,
                after_stage_hook=after_stage_hook,
            )

    def _run_locked(
        self,
        *,
        progress_hook: ProgressHook | None,
        after_stage_hook: AfterStageHook | None,
    ) -> RunResult:
        self.store.ensure_manifest()
        bundle = load_bundle(self.workspace)
        baseline_state = self.store.load_manifest()
        started_with = committed_stages(baseline_state)
        baseline_payloads = self._baseline_payloads(baseline_state)
        degradations = self._degradation_plan(bundle)

        state, payloads, invalidated_from = self._reconcile(bundle, baseline_state)
        initial_count = len(started_with)
        if invalidated_from is not None:
            mode = "edit_replay" if initial_count else "fresh"
        elif 0 < initial_count < len(STAGE_NAMES):
            mode = "resume"
        elif initial_count == len(STAGE_NAMES):
            mode = "complete_reuse"
        else:
            mode = "fresh"

        head = self.store.head_token(state)
        stage_reports: list[dict[str, Any]] = []
        stopped_gracefully = False
        recomputed: list[str] = []
        cache_hits: list[str] = []

        start_index = len(committed_stages(state))
        previous_payload = (
            payloads[STAGE_NAMES[start_index - 1]] if start_index > 0 else None
        )
        raw_samples_bytes = self.workspace.samples_path.stat().st_size
        for stage in STAGE_NAMES[start_index:]:
            input_count = _input_count_for_stage(stage, bundle, previous_payload)
            previous_bytes = (
                len(canonical_json_bytes(previous_payload))
                if previous_payload is not None
                else 0
            )
            estimate_mb = _estimate_memory_mb(
                stage,
                input_count,
                raw_bytes=raw_samples_bytes,
                previous_bytes=previous_bytes,
                signature_size=int(bundle.rules["dedupe"]["signature_size"]),
            )
            self._enforce_limits(bundle, stage, input_count, estimate_mb)
            upstream_checksum = (
                state["stages"][STAGE_NAMES[STAGE_NAMES.index(stage) - 1]]["checksum"]
                if STAGE_NAMES.index(stage) > 0
                else None
            )
            cache_key, dependency_digest, base_input = _cache_key_for(
                bundle,
                stage,
                bundle.samples_digest if stage == "ingest" else upstream_checksum,
            )
            cached = self.store.get_cached(stage, cache_key)
            if cached is not None:
                payload = cached
                action = "cache_hit"
                cache_hits.append(stage)
            else:
                payload = self._execute_stage(
                    bundle, stage, previous_payload, progress_hook
                )
                action = "computed"
                recomputed.append(stage)
            self._verify_expected_completed_ids(stage, payload, previous_payload)
            commit = self.store.commit_payload(
                stage=stage,
                payload=payload,
                cache_key=cache_key,
                upstream_checksum=upstream_checksum,
                dependency_digest=dependency_digest,
                expected_head=head,
            )
            head = commit["head"]
            state = self.store.load_manifest()
            payloads[stage] = payload
            previous_payload = payload
            entry = commit["entry"]
            report = {
                "stage": stage,
                "label": STAGE_LABELS[stage],
                "action": action,
                "version": entry["version"],
                "checksum": entry["checksum"],
                "input_count": input_count,
                "completed_count": entry["completed_count"],
                "processed_count": entry["processed_count"],
                "output_count": entry["output_count"],
                "estimated_memory_mb": estimate_mb,
                "base_input": base_input,
                "cache_key": cache_key,
            }
            stage_reports.append(report)
            if after_stage_hook is not None and after_stage_hook(stage, entry):
                stopped_gracefully = True
                write_json_report(
                    self.workspace.graceful_stop_path,
                    {
                        "ok": True,
                        "stopped_gracefully": True,
                        "at_stage": stage,
                        "at_version": entry["version"],
                        "next_stage": (
                            STAGE_NAMES[STAGE_NAMES.index(stage) + 1]
                            if STAGE_NAMES.index(stage) + 1 < len(STAGE_NAMES)
                            else None
                        ),
                        "committed_stages": committed_stages(state),
                    },
                )
                break

        current_state = self.store.load_manifest()
        current_payloads = self._baseline_payloads(current_state)
        reuse_versions = {
            stage: entry["version"]
            for stage, entry in baseline_state["stages"].items()
            if stage in current_state["stages"]
            and current_state["stages"][stage]["version"] == entry["version"]
        }
        reused_snapshot_stages = [
            stage for stage in STAGE_NAMES if stage in reuse_versions
        ]
        stage_diffs = self._stage_diffs(
            baseline_payloads, current_payloads, baseline_state, current_state
        )
        final_export: dict[str, Any] | None = None
        if "export" in current_payloads:
            export_payload = current_payloads["export"]
            final_export = {
                "version": current_state["stages"]["export"]["version"],
                "path": export_payload["export_path"],
                "content_checksum": export_payload["content_checksum"],
                "dataset_bytes_checksum": export_payload["dataset_bytes_checksum"],
                "record_count": export_payload["record_count"],
            }
        completed = committed_stages(current_state) == list(STAGE_NAMES)
        result = RunResult(
            mode=mode,
            started_with_stages=started_with,
            recomputed_stages=recomputed,
            cache_hit_stages=cache_hits,
            reused_snapshot_stages=reused_snapshot_stages,
            invalidated_from=invalidated_from,
            stage_reports=stage_reports,
            stage_diffs=stage_diffs,
            degradations=degradations,
            stopped_gracefully=stopped_gracefully,
            completed=completed,
            final_export=final_export,
        )
        recovery_report = {
            "ok": True,
            "mode": mode,
            "resumed": mode == "resume",
            "started_with_stages": started_with,
            "invalidated_from": invalidated_from,
            "recomputed_stages": recomputed,
            "cache_hit_stages": cache_hits,
            "reused_snapshot_stages": reused_snapshot_stages,
            "content_changed_stages": result.content_changed_stages,
            "stage_diffs": stage_diffs,
            "degradations": degradations,
            "final_committed_stages": committed_stages(current_state),
            "stopped_gracefully": stopped_gracefully,
            "completed": completed,
            "final_export": final_export,
        }
        write_json_report(
            self.workspace.recovery_report_path, recovery_report
        )
        return result

    def self_check(self) -> dict[str, Any]:
        bundle = load_bundle(self.workspace)
        state = self.store.load_manifest()
        checks: list[dict[str, Any]] = []
        complete = committed_stages(state) == list(STAGE_NAMES)
        payloads: dict[str, dict[str, Any]] = {}
        upstream_checksum: str | None = None

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        add(
            "manifest_prefix",
            committed_stages(state)
            == list(STAGE_NAMES[: len(state["stages"])]),
            "committed stages form a contiguous prefix",
        )
        for index, stage in enumerate(committed_stages(state)):
            entry = state["stages"][stage]
            snapshot = self.store.read_snapshot(stage, entry)
            payload = snapshot["payload"]
            payloads[stage] = payload
            add(
                f"{stage}.snapshot_checksum",
                snapshot["content_checksum"] == entry["checksum"],
                "snapshot and manifest checksums match",
            )
            expected_upstream = None if index == 0 else upstream_checksum
            add(
                f"{stage}.upstream_checksum",
                entry["upstream_checksum"] == expected_upstream,
                "upstream checksum linkage matches",
            )
            expected_key, dependency_digest, _base = _cache_key_for(
                bundle,
                stage,
                bundle.samples_digest if stage == "ingest" else expected_upstream,
            )
            add(
                f"{stage}.cache_key",
                entry["cache_key"] == expected_key
                and snapshot["cache_key"] == expected_key
                and entry["dependency_digest"] == dependency_digest,
                "dependency-aware cache key matches",
            )
            object_path = self.store._cache_object_path(entry["checksum"])  # noqa: SLF001
            cache_ok = (
                object_path.is_file()
                and sha256_file(object_path) == entry["checksum"]
            )
            add(
                f"{stage}.cache_object",
                cache_ok,
                "content-addressed cache object is intact",
            )
            key_path = self.store._cache_key_path(entry["cache_key"])  # noqa: SLF001
            key_index_ok = False
            if key_path.is_file():
                try:
                    key_record = read_json_file(key_path)
                    key_index_ok = isinstance(key_record, dict) and (
                        key_record.get("stage") == stage
                        and key_record.get("cache_key") == entry["cache_key"]
                        and key_record.get("content_checksum") == entry["checksum"]
                    )
                except (OSError, json.JSONDecodeError):
                    key_index_ok = False
            add(
                f"{stage}.cache_key_index",
                key_index_ok,
                "published cache key points to the committed snapshot payload",
            )
            completed = payload["completed_sample_ids"]
            add(
                f"{stage}.completed_ids",
                completed == sorted(set(completed)),
                "completed sample ids are sorted and unique",
            )
            if stage == "ingest":
                expected_completed = sorted(
                    record["sample_id"] for record in bundle.samples
                )
            elif stage in {"normalize", "quality", "dedupe"}:
                expected_completed = payloads[STAGE_NAMES[index - 1]]["completed_sample_ids"]
            elif stage == "sample":
                expected_completed = sorted(
                    record["sample_id"]
                    for record in payloads["dedupe"]["records"]
                    if record["dedupe"]["kept"]
                )
            else:
                expected_completed = sorted(
                    record["sample_id"]
                    for record in payloads["sample"]["records"]
                )
            add(
                f"{stage}.completed_set",
                completed == expected_completed,
                "all expected samples were processed before commit",
            )
            record_ids = [record["sample_id"] for record in _payload_records(payload)]
            add(
                f"{stage}.record_ids",
                len(record_ids) == len(set(record_ids)),
                "stage records contain unique sample ids",
            )
            upstream_checksum = entry["checksum"]

        if "dedupe" in payloads:
            dedupe_payload = payloads["dedupe"]
            threshold = float(dedupe_payload["threshold"])
            score_by_id = {
                record["sample_id"]: float(record["quality_score"])
                for record in payloads["quality"]["records"]
            }
            removed_ok = True
            for record in dedupe_payload["records"]:
                decision = record["dedupe"]
                if decision["threshold"] != threshold:
                    removed_ok = False
                    break
                if not decision["kept"] and not decision["similar_neighbors"]:
                    removed_ok = False
                    break
                for neighbor in decision["similar_neighbors"]:
                    if neighbor["similarity"] < threshold:
                        removed_ok = False
                        break
            add(
                "dedupe.removed_detail",
                removed_ok,
                "each removal records a qualifying neighbor and threshold",
            )
            winners_ok = True
            cluster_ids = {
                cluster["cluster_id"] for cluster in dedupe_payload["clusters"]
            }
            for cluster in dedupe_payload["clusters"]:
                expected_winner = min(
                    cluster["member_ids"],
                    key=lambda identifier: (-score_by_id[identifier], identifier),
                )
                if cluster["representative_id"] != expected_winner:
                    winners_ok = False
                    break
            add(
                "dedupe.winner_rule",
                winners_ok and len(cluster_ids) == len(dedupe_payload["clusters"]),
                "cluster representatives follow score desc/sample_id asc",
            )
            add(
                "dedupe.threshold",
                len(
                    [
                        record
                        for record in dedupe_payload["records"]
                        if record["dedupe"]["kept"]
                    ]
                )
                == len(dedupe_payload["kept_sample_ids"]),
                "kept sample list matches per-record decisions",
            )
            for cluster in dedupe_payload["clusters"]:
                for edge in cluster["edges"]:
                    left = next(
                        record
                        for record in dedupe_payload["records"]
                        if record["sample_id"] == edge["left"]
                    )
                    right = next(
                        record
                        for record in dedupe_payload["records"]
                        if record["sample_id"] == edge["right"]
                    )
                    recomputed = signature_similarity(
                        [int(value, 16) for value in left["dedupe"]["signature"]],
                        [int(value, 16) for value in right["dedupe"]["signature"]],
                    )
                    if recomputed != edge["similarity"]:
                        add(
                            "dedupe.edge_recompute",
                            False,
                            "edge similarity does not match recorded signatures",
                        )
                        break
                else:
                    continue
                break
            else:
                add(
                    "dedupe.edge_recompute",
                    True,
                    "all recorded edge similarities recompute from signatures",
                )

        if "sample" in payloads and "dedupe" in payloads:
            sample_payload = payloads["sample"]
            kept_ids = set(payloads["dedupe"]["kept_sample_ids"])
            selected = [record["sample_id"] for record in sample_payload["records"]]
            target_counts: dict[int, int] = {}
            for record in sample_payload["records"]:
                target_index = record["sampling"]["target_index"]
                target_counts[target_index] = target_counts.get(target_index, 0) + 1
            goals_ok = all(
                target_counts.get(goal["target_index"], 0) == goal["actual"]
                for goal in sample_payload["goals"]
            )
            add(
                "sample.quota_manifest",
                goals_ok,
                "goal actual counts match selected records",
            )
            add(
                "sample.selection",
                len(selected) == len(set(selected))
                and set(selected).issubset(kept_ids)
                and len(selected) == sample_payload["actual_total"],
                "selected ids are unique dedupe survivors",
            )

        if "export" in payloads and "sample" in payloads:
            export_payload = payloads["export"]
            rows_bytes = "".join(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
                for row in export_payload["rows"]
            ).encode("utf-8")
            content_checksum = sha256_bytes(rows_bytes)
            add(
                "export.content_checksum",
                content_checksum == export_payload["content_checksum"]
                and export_payload["header"]["content_checksum"]
                == f"sha256:{content_checksum}",
                "row content checksum and header checksum match",
            )
            export_path = self.workspace.root / export_payload["export_path"]
            actual_file_checksum = (
                sha256_file(export_path) if export_path.is_file() else None
            )
            add(
                "export.file_checksum",
                actual_file_checksum == export_payload["dataset_bytes_checksum"],
                "exported JSONL bytes match the recorded checksum",
            )
            sample_ids = [
                record["sample_id"] for record in payloads["sample"]["records"]
            ]
            export_ids = [row["sample_id"] for row in export_payload["rows"]]
            add(
                "export.order",
                sample_ids == export_ids,
                "export order follows sampling declaration order",
            )

        return {
            "ok": all(check["ok"] for check in checks),
            "complete": complete,
            "checks": checks,
            "committed_stages": committed_stages(state),
            "next_version": state["next_version"],
        }


def pipeline_status(workspace: Workspace) -> dict[str, Any]:
    workspace.ensure_layout()
    store = SnapshotStore(workspace)
    state = store.load_manifest()
    stages: list[dict[str, Any]] = []
    for index, stage in enumerate(STAGE_NAMES):
        entry = state["stages"].get(stage)
        stages.append(
            {
                "stage": stage,
                "label": STAGE_LABELS[stage],
                "index": index,
                "committed": entry is not None,
                "version": entry["version"] if entry else None,
                "checksum": entry["checksum"] if entry else None,
                "completed_count": entry["completed_count"] if entry else 0,
                "processed_count": entry["processed_count"] if entry else 0,
                "output_count": entry["output_count"] if entry else 0,
            }
        )
    committed_names = committed_stages(state)
    if len(committed_names) == len(STAGE_NAMES):
        current_stage = "complete"
        next_stage = None
    else:
        next_stage = STAGE_NAMES[len(committed_names)]
        current_stage = (
            committed_names[-1] if committed_names else "not_started"
        )
    export_info = None
    if "export" in state["stages"]:
        try:
            export_payload = store.read_snapshot(
                "export", state["stages"]["export"]
            )["payload"]
            export_info = {
                "version": state["stages"]["export"]["version"],
                "path": export_payload["export_path"],
                "content_checksum": export_payload["content_checksum"],
                "dataset_bytes_checksum": export_payload["dataset_bytes_checksum"],
                "record_count": export_payload["record_count"],
            }
        except BusinessError as exc:
            export_info = {"error": exc.as_dict()}
    edit_report = read_json_report(workspace.last_edit_path)
    recovery_report = read_json_report(workspace.recovery_report_path)
    last_error = read_json_report(workspace.last_error_path)
    demo_report = read_json_report(workspace.demo_report_path)
    return {
        "ok": True,
        "current_stage": current_stage,
        "next_stage": next_stage,
        "next_version": state["next_version"],
        "generation": state["generation"],
        "stages": stages,
        "export": export_info,
        "last_edit": edit_report,
        "recovery": recovery_report,
        "last_error": last_error,
        "demo": demo_report,
    }
