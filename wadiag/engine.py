from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .compaction import build_compaction_outputs, plan_compaction
from .errors import EngineError, error
from .estimation import diagnose_run
from .models import ClockTable, DiagnosticConfig, StorageConfig, Trajectory, WriteRecord
from .runtime import crash_point
from .segments import (
    Segment,
    build_flush_segments,
    build_manifest,
    build_merged_segments,
)
from .serialization import canonical_json_bytes, canonical_json_text, json_safe, sha256_bytes
from .storage import StateStore
from .validation import (
    load_clock_table,
    load_diagnostic_config,
    load_storage_config,
    load_trajectory,
)


class DiagnosticEngine:
    def __init__(self, workspace: str | Path) -> None:
        self.store = StateStore(workspace)

    def load(
        self,
        trajectory_path: str | Path,
        storage_path: str | Path,
        diagnostic_path: str | Path,
        clock_path: str | Path | None = None,
    ) -> dict[str, Any]:
        trajectory = load_trajectory(trajectory_path)
        storage = load_storage_config(storage_path)
        diagnostic = load_diagnostic_config(diagnostic_path)
        clock = load_clock_table(clock_path)
        return self.load_models(trajectory, storage, diagnostic, clock)

    def load_models(
        self,
        trajectory: Trajectory,
        storage: StorageConfig,
        diagnostic: DiagnosticConfig,
        clock: ClockTable,
    ) -> dict[str, Any]:
        if clock.overrides:
            trajectory = replace(
                trajectory,
                records=tuple(
                    replace(
                        record,
                        clock_ms=clock.overrides.get(record.seq, record.clock_ms),
                    )
                    for record in trajectory.records
                ),
            )
        snapshot = {
            "trajectory": trajectory.to_dict(),
            "storage": storage.to_dict(),
            "diagnostic": diagnostic.to_dict(),
            "clock": clock.to_dict(),
        }
        snapshot_bytes = canonical_json_bytes(snapshot)
        snapshot_sha = sha256_bytes(snapshot_bytes)
        run_id = "R" + snapshot_sha[:24]
        effective_clock = max(
            [record.clock_ms for record in trajectory.records]
            + [
                clock.timestamp_for(
                    trajectory.records[-1].seq,
                    trajectory.records[-1].clock_ms,
                )
            ]
        )
        with self.store.exclusive():
            self.store.initialize()
            created = self.store.create_run(
                run_id=run_id,
                trajectory=trajectory,
                storage=storage,
                diagnostic=diagnostic,
                clock=clock,
                snapshot_sha256=snapshot_sha,
                clock_ms=effective_clock,
            )
            watermark = self.store.get_watermark(run_id)
        return {
            "ok": True,
            "command": "load",
            "run_id": run_id,
            "trajectory_id": trajectory.trajectory_id,
            "record_count": len(trajectory.records),
            "max_seq": trajectory.records[-1].seq,
            "clock_ms": watermark["clock_ms"],
            "snapshot_sha256": snapshot_sha,
            "created": created,
            "idempotent_replay": not created,
        }

    def _run_context(self) -> tuple[
        str,
        Trajectory,
        StorageConfig,
        DiagnosticConfig,
        ClockTable,
    ]:
        run_id = self.store.active_run_id()
        trajectory, storage, diagnostic, clock = self.store.load_run_models(run_id)
        return run_id, trajectory, storage, diagnostic, clock

    @staticmethod
    def _write_records(trajectory: Trajectory) -> tuple[WriteRecord, ...]:
        return trajectory.records

    def _ensure_no_prepared(self, run_id: str) -> None:
        count = self.store.pending_count(run_id, "prepared")
        if count:
            raise error(
                "E_PENDING",
                "存在未完成的持久化操作，请先执行 resume",
                run_id=run_id,
                pending_count=count,
            )

    def flush(
        self,
        *,
        from_segment: int | None = None,
        to_segment: int | None = None,
    ) -> dict[str, Any]:
        with self.store.exclusive():
            run_id, trajectory, storage, diagnostic, clock_table = self._run_context()
            self._ensure_no_prepared(run_id)
            all_segments = build_flush_segments(
                trajectory.records,
                storage,
                run_id,
            )
            total = len(all_segments)
            first = 1 if from_segment is None else from_segment
            last = total if to_segment is None else to_segment
            if first < 1 or last < first or last > total:
                raise error(
                    "E_BAD_RANGE",
                    "段区间非法",
                    from_segment=first,
                    to_segment=last,
                    available=f"1:{total}",
                )
            selected = all_segments[first - 1 : last]
            heads = self.store.get_level_heads(run_id)
            visible = {
                uid
                for level in heads
                for uid in heads[level]["segment_uids"]
            }
            rows = {row["uid"]: row for row in self.store.get_all_segment_rows(run_id)}
            pending_rows = [
                segment
                for segment in selected
                if segment.meta.uid in rows and rows[segment.meta.uid]["status"] != "ready"
            ]
            if pending_rows:
                raise error(
                    "E_PENDING",
                    "目标段已有未完成写入，请先执行 resume",
                    segment_uids=[segment.meta.uid for segment in pending_rows],
                )
            new_segments = tuple(
                segment
                for segment in selected
                if segment.meta.uid not in visible
                and not (
                    segment.meta.uid in rows and bool(rows[segment.meta.uid]["retired"])
                )
            )
            watermark = self.store.get_watermark(run_id)
            if not new_segments:
                return {
                    "ok": True,
                    "command": "flush",
                    "run_id": run_id,
                    "requested_range": [first, last],
                    "segments_written": 0,
                    "segment_uids": [],
                    "memtable_flushed_seq": watermark["memtable_flushed_seq"],
                    "clock_ms": watermark["clock_ms"],
                    "idempotent_replay": True,
                }
            op_digest = sha256_bytes(
                canonical_json_bytes(
                    {
                        "run_id": run_id,
                        "kind": "flush",
                        "uids": [segment.meta.uid for segment in new_segments],
                    }
                )
            )
            op_id = f"F:{op_digest[:24]}"
            current_clock = watermark["clock_ms"]
            decision_clock = current_clock + storage.logical_clock_step_ms
            self.store.prepare_flush(
                run_id=run_id,
                op_id=op_id,
                segments=new_segments,
                from_segment=first,
                to_segment=last,
                clock_ms=decision_clock,
            )
            for segment in new_segments:
                self.store.write_segment(segment)
            manifest = build_manifest(run_id, new_segments, f"flush-{first}-{last}")
            self.store.write_manifest(run_id, manifest)
            crash_point("flush_files")
            result = self.store.commit_pending(op_id)
            watermark = self.store.get_watermark(run_id)
            return {
                "ok": True,
                "command": "flush",
                "run_id": run_id,
                "requested_range": [first, last],
                "segments_written": len(new_segments),
                "segment_uids": [segment.meta.uid for segment in new_segments],
                "manifest_digest": manifest["manifest_digest"],
                "memtable_flushed_seq": watermark["memtable_flushed_seq"],
                "clock_ms": watermark["clock_ms"],
                "commit": result,
            }

    @staticmethod
    def _decision_payload(plan: dict[str, Any]) -> dict[str, Any]:
        def sanitize(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    str(key): sanitize(item)
                    for key, item in value.items()
                    if key != "selected_segments"
                }
            if isinstance(value, (list, tuple)):
                return [sanitize(item) for item in value]
            if hasattr(value, "meta") and hasattr(value.meta, "to_manifest"):
                return value.meta.uid
            return json_safe(value)

        return sanitize(plan)

    def _rebuild_output_segments(
        self,
        kept: list[Any],
        storage: StorageConfig,
        run_id: str,
        target_level: int,
        generation: int,
        first_segment_no: int,
    ) -> tuple[Segment, ...]:
        return build_merged_segments(
            kept,
            storage,
            run_id,
            first_segment_no=first_segment_no,
            level=target_level,
            generation=generation,
        )

    def compact(self) -> dict[str, Any]:
        with self.store.exclusive():
            run_id, trajectory, storage, diagnostic, clock_table = self._run_context()
            self._ensure_no_prepared(run_id)
            level_segments, loaded_storage = self.store.load_level_segments(run_id)
            plan = plan_compaction(level_segments, loaded_storage, diagnostic)
            if not plan["started"]:
                return {
                    "ok": True,
                    "command": "compact",
                    "run_id": run_id,
                    "applied": False,
                    **json_safe(plan),
                }
            all_visible = [
                segment
                for level in sorted(level_segments)
                for segment in level_segments[level]
            ]
            probe_outputs, cleared, generation, kept = build_compaction_outputs(
                plan,
                all_visible,
                storage,
                run_id,
                0,
            )
            segment_numbers = self.store.allocate_segment_numbers(
                run_id,
                len(probe_outputs),
            )
            first_number = segment_numbers[0] if segment_numbers else 1
            outputs = self._rebuild_output_segments(
                kept,
                storage,
                run_id,
                plan["target_level"],
                generation,
                first_number,
            )
            heads = self.store.get_level_heads(run_id)
            source_level = plan["chosen_level"]
            target_level = plan["target_level"]
            source_uids = list(plan["selected_segment_uids"])
            op_digest = sha256_bytes(
                canonical_json_bytes(
                    {
                        "run_id": run_id,
                        "kind": "compact",
                        "source_level": source_level,
                        "target_level": target_level,
                        "source_uids": source_uids,
                        "output_uids": [segment.meta.uid for segment in outputs],
                        "cleared": cleared,
                    }
                )
            )
            op_id = f"C:{op_digest[:24]}"
            decision_clock = (
                self.store.get_watermark(run_id)["clock_ms"]
                + storage.logical_clock_step_ms
            )
            decision_payload = self._decision_payload(plan)
            self.store.prepare_compaction(
                run_id=run_id,
                op_id=op_id,
                source_uids=source_uids,
                target_level=target_level,
                outputs=outputs,
                cleared_tombstone_keys=cleared,
                source_view_version=heads[source_level]["view_version"],
                target_view_version=heads[target_level]["view_version"],
                clock_ms=decision_clock,
                decision_payload=decision_payload,
            )
            for segment in outputs:
                self.store.write_segment(segment)
            if outputs:
                manifest = build_manifest(run_id, outputs, f"compact-L{target_level}")
                self.store.write_manifest(run_id, manifest)
            crash_point("compact_files")
            result = self.store.commit_pending(op_id)
            return {
                "ok": True,
                "command": "compact",
                "run_id": run_id,
                "applied": True,
                "plan": decision_payload,
                "source_level": source_level,
                "target_level": target_level,
                "source_segment_uids": source_uids,
                "output_segment_uids": [segment.meta.uid for segment in outputs],
                "cleared_tombstone_keys": cleared,
                "commit": result,
                "clock_ms": decision_clock,
            }

    def _compute_diagnosis_locked(
        self,
        run_id: str,
        *,
        query_key: str | None,
        save: bool,
    ) -> dict[str, Any]:
        trajectory, storage, diagnostic, clock = self.store.load_run_models(run_id)
        level_segments, _ = self.store.load_level_segments(run_id)
        heads = self.store.get_level_heads(run_id)
        view_digest = self.store.view_digest(run_id)
        payload = diagnose_run(
            level_segments,
            storage,
            diagnostic,
            query_key=query_key,
            level_views=heads,
        )
        payload["run_id"] = run_id
        payload["trajectory_id"] = trajectory.trajectory_id
        payload["view_digest"] = view_digest
        payload["storage_config"] = storage.to_dict()
        payload["diagnostic_config"] = diagnostic.to_dict()
        payload["clock_watermark_ms"] = self.store.get_watermark(run_id)["clock_ms"]
        payload["ok"] = True
        payload["command"] = "diagnose"
        if save:
            self.store.save_diagnosis(
                run_id,
                view_digest,
                payload,
                self.store.get_watermark(run_id)["clock_ms"],
            )
        return payload

    def diagnose(self, *, query_key: str | None = None) -> dict[str, Any]:
        with self.store.exclusive():
            run_id, _, _, _, _ = self._run_context()
            return self._compute_diagnosis_locked(
                run_id,
                query_key=query_key,
                save=True,
            )

    def resume(self) -> dict[str, Any]:
        with self.store.exclusive():
            run_id, _, _, _, _ = self._run_context()
            result = self.store.resume(run_id)
            result["ok"] = True
            result["command"] = "resume"
            return result

    def status(self) -> dict[str, Any]:
        with self.store.exclusive():
            run_id = self.store.active_run_id()
            payload = self.store.status(run_id)
            payload["ok"] = True
            payload["command"] = "status"
            return payload

    def verify(self) -> dict[str, Any]:
        with self.store.exclusive():
            run_id, trajectory, storage, diagnostic, clock = self._run_context()
            errors: list[dict[str, Any]] = []
            try:
                level_segments, loaded_storage = self.store.load_level_segments(run_id)
            except EngineError as exc:
                return {
                    "ok": False,
                    "command": "verify",
                    "run_id": run_id,
                    "segments_checked": 0,
                    "levels_checked": 0,
                    "errors": [exc.to_dict()],
                    "deterministic_rebuild": False,
                }
            heads = self.store.get_level_heads(run_id)
            all_rows = {row["uid"]: row for row in self.store.get_all_segment_rows(run_id)}
            seen: dict[str, int] = {}
            for level in sorted(level_segments):
                for segment in level_segments[level]:
                    uid = segment.meta.uid
                    if uid in seen:
                        errors.append(
                            {
                                "code": "E_VERIFY_DUPLICATE_VIEW_MEMBERSHIP",
                                "uid": uid,
                                "levels": [seen[uid], level],
                            }
                        )
                    seen[uid] = level
                    row = all_rows.get(uid)
                    if row is None:
                        errors.append({"code": "E_VERIFY_MISSING_ROW", "uid": uid})
                        continue
                    if row["status"] != "ready" or bool(row["retired"]):
                        errors.append(
                            {
                                "code": "E_VERIFY_SEGMENT_STATE",
                                "uid": uid,
                                "status": row["status"],
                                "retired": bool(row["retired"]),
                            }
                        )
                uids = heads[level]["segment_uids"]
                expected_summary = {
                    "segment_count": len(level_segments[level]),
                    "bytes": sum(
                        segment.meta.size_bytes for segment in level_segments[level]
                    ),
                    "record_count": sum(
                        segment.meta.record_count for segment in level_segments[level]
                    ),
                    "tombstone_count": sum(
                        segment.meta.tombstone_count for segment in level_segments[level]
                    ),
                    "min_key": min(
                        (segment.meta.min_key for segment in level_segments[level]),
                        default=None,
                    ),
                    "max_key": max(
                        (segment.meta.max_key for segment in level_segments[level]),
                        default=None,
                    ),
                    "digest": sha256_bytes(canonical_json_bytes(sorted(uids))),
                }
                if heads[level]["summary"] != expected_summary:
                    errors.append(
                        {
                            "code": "E_VERIFY_LEVEL_SUMMARY",
                            "level": level,
                            "expected": expected_summary,
                            "actual": heads[level]["summary"],
                        }
                    )
            pending = self.store.pending_count(run_id, "prepared")
            if pending:
                errors.append(
                    {
                        "code": "E_VERIFY_PENDING_OPERATIONS",
                        "pending_count": pending,
                    }
                )
            for manifest_row in self.store.get_manifests(run_id):
                if manifest_row["state"] != "committed":
                    errors.append(
                        {
                            "code": "E_VERIFY_MANIFEST_STATE",
                            "manifest_digest": manifest_row["manifest_digest"],
                            "state": manifest_row["state"],
                        }
                    )
                    continue
                manifest_path = self.store.workspace / manifest_row["relative_path"]
                if not manifest_path.exists():
                    errors.append(
                        {
                            "code": "E_VERIFY_MANIFEST_MISSING",
                            "manifest_digest": manifest_row["manifest_digest"],
                            "path": manifest_row["relative_path"],
                        }
                    )
                    continue
                try:
                    manifest_payload = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    rebuilt_digest = self.store.manifest_from_metas(
                        run_id,
                        manifest_payload["kind"],
                        manifest_payload["segments"],
                    )["manifest_digest"]
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append(
                        {
                            "code": "E_VERIFY_MANIFEST_CONTENT",
                            "manifest_digest": manifest_row["manifest_digest"],
                            "reason": str(exc),
                        }
                    )
                    continue
                if rebuilt_digest != manifest_row["manifest_digest"]:
                    errors.append(
                        {
                            "code": "E_VERIFY_MANIFEST_CONTENT",
                            "manifest_digest": manifest_row["manifest_digest"],
                            "actual": rebuilt_digest,
                        }
                    )
            view_digest = self.store.view_digest(run_id)
            snapshot = self.store.get_diagnosis(run_id, view_digest)
            snapshot_query_key = (
                None
                if snapshot is None
                else snapshot["payload"].get("lookup_cost", {}).get("key")
            )
            diagnosis = self._compute_diagnosis_locked(
                run_id,
                query_key=snapshot_query_key,
                save=False,
            )
            if snapshot is None:
                self.store.save_diagnosis(
                    run_id,
                    view_digest,
                    diagnosis,
                    self.store.get_watermark(run_id)["clock_ms"],
                )
                snapshot = self.store.get_diagnosis(run_id, view_digest)
            expected_digest = sha256_bytes(
                canonical_json_text(diagnosis).encode("utf-8")
            )
            actual_digest = None if snapshot is None else snapshot["payload_digest"]
            if actual_digest != expected_digest:
                errors.append(
                    {
                        "code": "E_VERIFY_DIAGNOSIS_MISMATCH",
                        "expected": expected_digest,
                        "actual": actual_digest,
                    }
                )
            status = self.store.status(run_id)
            return {
                "ok": not errors,
                "command": "verify",
                "run_id": run_id,
                "view_digest": view_digest,
                "segments_checked": len(seen),
                "levels_checked": len(level_segments),
                "pending_prepared": pending,
                "diagnosis_payload_digest": expected_digest,
                "orphan_segment_files": status["orphan_segment_files"],
                "errors": errors,
                "deterministic_rebuild": not errors,
            }
