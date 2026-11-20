from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .aggregation import (
    aggregate_batch_partial,
    build_result_payload,
    build_window_groups,
    compare_group_states,
    merge_all_channels,
    merge_partial,
    move_channels,
    replay_exact,
    route_partial,
    state_digest,
)
from .budget import ACCELERATED_MODE, EXACT_MODE, decide_mode
from .errors import (
    GSBError,
    GSBStateConflictError,
    GSBVerifyError,
)
from .hotness import HotTracker
from .models import QuerySpec, Sample
from .spill import safe_query_name
from .storage import Storage
from .util import (
    atomic_write,
    first_difference,
    same_json_bitwise,
    sha256_json,
    stable_json,
)


def reset_state(db_path: Path, work_dir: Path, out_dir: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path) + suffix)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for directory in (work_dir, out_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)


class CrashInjector:
    def __init__(self, spec: str | None) -> None:
        self.specs = {
            item.strip()
            for item in (spec or os.environ.get("GSB_CRASH_AT", "")).split(",")
            if item.strip()
        }

    def _matches(self, event: str, batch_index: int) -> bool:
        candidates = {
            f"{event}:{batch_index}",
            f"{event}:{batch_index}:*",
        }
        return bool(self.specs.intersection(candidates))

    @staticmethod
    def _exit() -> None:
        os._exit(86)

    def stage(self, batch_index: int) -> None:
        if self._matches("stage", batch_index) or self._matches(
            "after-stage", batch_index
        ):
            self._exit()

    def storage_hook(self, event: str, ordinal: int, batch_index: int) -> None:
        aliases = {
            "apply_after_state_before_commit": {
                "apply_after_state_before_commit",
                "apply-before-commit",
                "before-commit",
            },
            "apply_after_commit": {
                "apply_after_commit",
                "apply-after-commit",
                "after-apply",
            },
        }
        names = aliases.get(event, {event})
        if any(self._matches(name, batch_index) for name in names):
            self._exit()

    def spill(self, batch_index: int) -> None:
        if self._matches("spill", batch_index) or self._matches(
            "after-spill", batch_index
        ):
            self._exit()


class Engine:
    def __init__(
        self,
        sample: Sample,
        db_path: Path,
        work_dir: Path,
        out_dir: Path,
        crash_injector: CrashInjector | None = None,
    ) -> None:
        self.sample = sample
        self.db_path = db_path
        self.work_dir = work_dir
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(db_path, work_dir)
        self.crash = crash_injector or CrashInjector(None)

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def process_query(
        self,
        query: QuerySpec,
        worker_id: str,
        only_batch_indexes: set[int] | None = None,
    ) -> dict[str, Any]:
        record = self.storage.initialize_query(self.sample, query)
        if record["status"] == "complete":
            return self.query_status(query)
        self.storage.recover_prepared(query)
        staged_rows = self.storage.staged_batches(query.query_id)
        committed = {
            int(row["ordinal"])
            for row in staged_rows
            if row["status"] == "committed"
        }
        prepared = {
            int(row["ordinal"])
            for row in staged_rows
            if row["status"] == "prepared"
        }
        for batch in self.sample.batches:
            if only_batch_indexes is not None and batch.batch_index not in only_batch_indexes:
                continue
            if batch.ordinal in committed or batch.ordinal in prepared:
                continue
            partial = aggregate_batch_partial(batch, query)
            self.storage.stage_batch(query, batch, partial, worker_id)
            self.crash.stage(batch.batch_index)
        applied = self.storage.apply_all_ready(
            self.sample,
            query,
            worker_id,
            self.crash.storage_hook,
        )
        for item in applied:
            if item["spilled"]:
                self.crash.spill(item["batch_index"])
        committed_rows = self.storage.committed_batches(query.query_id)
        if len(committed_rows) != len(self.sample.batches):
            return self.query_status(query)
        return self.finalize_query(query)

    def _hot_report(self, record: Any) -> dict[str, Any]:
        tracker = HotTracker(
            self.sample.config.hot_key_min_rows,
            self.sample.config.skew_ratio,
            json.loads(record["hot_state_json"]),
        )
        report = tracker.report()
        report["events"] = self.storage.hot_events(record["query_id"])
        return report

    def _mode_report(self, record: Any) -> dict[str, Any]:
        events = self.storage.mode_events(record["query_id"])
        transitions = [
            {
                "ordinal": event["ordinal"],
                "batch_index": event["batch_index"],
                "from": event["previous_mode"],
                "to": event["mode"],
                "elapsed_before_ms": event["elapsed_before_ms"],
                "elapsed_after_ms": event["elapsed_after_ms"],
                "reason": event["reason"],
                "rejected_modes": event["rejected_modes"],
            }
            for event in events
            if event["switched"]
        ]
        return {
            "initial_mode": EXACT_MODE,
            "final_mode": record["current_mode"],
            "actual_modes": sorted({event["mode"] for event in events}),
            "switch_count": len(transitions),
            "switches": transitions,
            "decisions": events,
        }

    def _latency_report(self, record: Any) -> dict[str, Any]:
        elapsed = float(record["elapsed_ms"])
        budget = self.sample.config.time_budget_ms
        decisions = self.storage.mode_events(record["query_id"])
        return {
            "clock_source": "injected_logical_clock",
            "wall_clock_used": False,
            "budget_ms": budget,
            "elapsed_ms": elapsed,
            "remaining_ms": budget - elapsed,
            "within_budget": elapsed <= budget,
            "batch_costs": [
                {
                    "ordinal": event["ordinal"],
                    "batch_index": event["batch_index"],
                    "mode": event["mode"],
                    "logical_cost_ms": event["logical_ms"],
                }
                for event in decisions
            ],
        }

    def _spill_report(self, record: Any) -> dict[str, Any]:
        stats = json.loads(record["stats_json"])
        return {
            "limit_bytes": self.sample.config.memory_limit_bytes,
            "peak_state_bytes": int(stats.get("state_bytes_peak", 0)),
            "spill_count": int(stats.get("spills", 0)),
            "spilled_groups_total": int(stats.get("spill_groups", 0)),
            "spilled_bytes_total": int(stats.get("spill_bytes", 0)),
            "readback_count": int(stats.get("readbacks", 0)),
            "readback_groups_total": int(stats.get("readback_groups", 0)),
            "readback_bytes_total": int(stats.get("readback_bytes", 0)),
            "active": bool(record["spill_active"]),
            "active_generation": int(record["spill_generation"]),
            "active_path": record["spill_path"],
            "events": self.storage.spill_events(record["query_id"]),
        }

    def query_status(self, query: QuerySpec) -> dict[str, Any]:
        record = self.storage.get_query(query.query_id)
        if record is None:
            raise GSBStateConflictError(
                "query state is absent",
                {"query_id": query.query_id},
            )
        staged = self.storage.staged_batches(query.query_id)
        committed = [row for row in staged if row["status"] == "committed"]
        prepared = [row for row in staged if row["status"] == "prepared"]
        payload: dict[str, Any] = {
            "query_id": query.query_id,
            "status": record["status"],
            "group_key": query.group_key,
            "mode": self._mode_report(record),
            "latency": self._latency_report(record),
            "hotspots": self._hot_report(record),
            "spills": self._spill_report(record),
            "watermark": {
                "ordinal": int(record["watermark_ordinal"]),
                "batch_index": record["watermark_batch_index"],
                "committed_batches": len(committed),
                "prepared_batches": len(prepared),
                "expected_batches": len(self.sample.batches),
            },
            "recovery": {
                "prepared_recovered": int(
                    json.loads(record["stats_json"]).get("prepared_recovered", 0)
                ),
                "prepared_discarded": int(
                    json.loads(record["stats_json"]).get("prepared_discarded", 0)
                ),
                "half_committed_repaired": int(
                    json.loads(record["stats_json"]).get(
                        "half_committed_repaired", 0
                    )
                ),
            },
            "memory_leases": self.storage.lease_summary(query.query_id),
            "result_digest": record["result_digest"],
            "result_path": record["output_path"],
            "consistency": json.loads(record["consistency_json"])
            if record["consistency_json"]
            else None,
        }
        return payload

    def _replay_accelerated_consistency(
        self,
        query: QuerySpec,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        modes = self.storage.mode_by_ordinal(query.query_id)
        exact: dict[str, dict[str, Any]] = {}
        normal: dict[str, dict[str, Any]] = {}
        hot: dict[str, dict[str, Any]] = {}
        tracker = HotTracker(
            self.sample.config.hot_key_min_rows,
            self.sample.config.skew_ratio,
        )
        all_ever_hot: set[str] = set()
        values_checked = 0
        groups_checked = 0
        for batch in self.sample.batches:
            partial = aggregate_batch_partial(batch, query)
            tracker.step(batch, partial)
            all_ever_hot.update(tracker.ever_hot)
            mode = modes.get(batch.ordinal, EXACT_MODE)
            accelerated = mode == ACCELERATED_MODE
            if accelerated:
                move_channels(normal, hot, all_ever_hot)
                route_partial(partial, normal, hot, all_ever_hot, True, query)
            else:
                merge_partial(normal, partial, query)
            merge_partial(exact, partial, query)
            accelerated_state = merge_all_channels(normal, hot, query)
            diff = compare_group_states(exact, accelerated_state)
            groups_checked = max(groups_checked, len(exact))
            values_checked = max(values_checked, len(exact) * len(query.aggregates) * 3)
            if diff is not None:
                return {
                    "ok": False,
                    "comparison": "bitwise_no_tolerance",
                    "first_divergence": {
                        "trigger_batch_index": batch.batch_index,
                        "trigger_ordinal": batch.ordinal,
                        "mode_at_trigger": mode,
                        "group_key": diff.get("group_key"),
                        "path": diff.get("path"),
                        "exact_intermediate": diff.get("exact_intermediate"),
                        "accelerated_intermediate": diff.get("accelerated_intermediate"),
                        "detail": diff,
                    },
                    "groups_checked": groups_checked,
                    "values_checked": values_checked,
                }, accelerated_state
        return {
            "ok": True,
            "comparison": "bitwise_no_tolerance",
            "groups_checked": groups_checked,
            "values_checked": values_checked,
            "checked_batches": len(self.sample.batches),
            "float_tolerance_configured": self.sample.config.float_tolerance,
            "float_tolerance_applied": False,
            "first_divergence": None,
        }, merge_all_channels(normal, hot, query)

    def finalize_query(self, query: QuerySpec) -> dict[str, Any]:
        record = self.storage.get_query(query.query_id)
        assert record is not None
        normal, hot, _ = self.storage.load_durable_state(query, mutate_stats=True)
        actual_groups = merge_all_channels(normal, hot, query)
        exact_groups = replay_exact(self.sample.batches, query)
        final_diff = compare_group_states(exact_groups, actual_groups)
        accelerated_consistency, replayed_accelerated = (
            self._replay_accelerated_consistency(query)
        )
        replay_actual_diff = compare_group_states(replayed_accelerated, actual_groups)
        if final_diff is not None:
            consistency = accelerated_consistency
            consistency["ok"] = False
            consistency["durable_state_match"] = False
            consistency["first_divergence"] = {
                "trigger_batch_index": self.sample.batches[-1].batch_index,
                "group_key": final_diff.get("group_key"),
                "path": final_diff.get("path"),
                "exact_intermediate": final_diff.get("exact_intermediate"),
                "accelerated_intermediate": final_diff.get("accelerated_intermediate"),
                "detail": final_diff,
            }
        else:
            consistency = accelerated_consistency
            consistency["durable_state_match"] = replay_actual_diff is None
            if replay_actual_diff is not None:
                consistency["ok"] = False
                consistency["durable_state_diff"] = replay_actual_diff
        record = self.storage.get_query(query.query_id)
        assert record is not None
        mode_report = self._mode_report(record)
        mode_history: list[str] = []
        for event in self.storage.mode_events(query.query_id):
            if not mode_history or mode_history[-1] != event["mode"]:
                mode_history.append(event["mode"])
        result_path = self.out_dir / f"{safe_query_name(query.query_id)}.result.json"
        payload = build_result_payload(
            query,
            actual_groups,
            self.sample.batches,
            actual_mode=str(record["current_mode"]),
            mode_history=mode_history,
            consistency=consistency,
        )
        result_digest = sha256_json(payload)
        payload["result_digest"] = result_digest
        result_bytes = stable_json(payload).encode("utf-8")
        atomic_write(result_path, result_bytes)
        hot_report = self._hot_report(record)
        latency_report = self._latency_report(record)
        spill_report = self._spill_report(record)
        status = "complete" if consistency["ok"] else "inconsistent"
        report = {
            "schema_version": 1,
            "query_id": query.query_id,
            "status": status,
            "group_key": query.group_key,
            "sample_fingerprint": self.sample.fingerprint,
            "mode": mode_report,
            "latency": latency_report,
            "hotspot_analysis": {
                "streaming_identification": True,
                "full_pre_scan_for_hotness": False,
                "criteria": hot_report["criteria"],
                "current_version": hot_report["current_version"],
                "hot_keys": hot_report["hot_keys"],
                "observations": hot_report["observations"],
                "changes": hot_report["changes"],
                "ever_hot_keys": hot_report["ever_hot_keys"],
            },
            "spill_and_memory": spill_report,
            "consistency": consistency,
            "result_path": str(result_path),
            "result_digest": result_digest,
            "result_sha256": sha256_json(
                {key: value for key, value in payload.items() if key != "result_digest"}
            ),
            "watermark": {
                "ordinal": int(record["watermark_ordinal"]),
                "batch_index": record["watermark_batch_index"],
                "committed_batches": len(self.storage.committed_batches(query.query_id)),
            },
            "recovery": {
                "prepared_recovered": int(
                    json.loads(record["stats_json"]).get("prepared_recovered", 0)
                ),
                "prepared_discarded": int(
                    json.loads(record["stats_json"]).get("prepared_discarded", 0)
                ),
                "half_committed_repaired": int(
                    json.loads(record["stats_json"]).get(
                        "half_committed_repaired", 0
                    )
                ),
                "idempotent_batch_commits": len(self.sample.batches),
            },
            "acceleration": {
                "path": ACCELERATED_MODE,
                "strategy": "hot-key channel split followed by two-phase merge",
                "arithmetic_order": (
                    "batch-local row order, then ascending batch ordinal merge"
                ),
                "truth_path": EXACT_MODE,
                "used": ACCELERATED_MODE in mode_report["actual_modes"],
            },
            "state": {
                "group_state_digest": state_digest(actual_groups),
                "group_count": len(actual_groups),
                "normal_channel_groups": len(normal),
                "hot_channel_groups": len(hot),
            },
        }
        report_path = self.out_dir / f"{safe_query_name(query.query_id)}.report.json"
        atomic_write(report_path, stable_json(report).encode("utf-8"))
        self.storage.finalize_query(
            query,
            payload,
            report,
            result_path,
            consistency,
            result_digest,
            status=status,
        )
        if not consistency["ok"]:
            raise GSBVerifyError(
                "exact and accelerated paths diverged",
                {"report_path": str(report_path), **consistency},
            )
        return report

    def status_all(self) -> dict[str, Any]:
        result: list[dict[str, Any]] = []
        for query_id in self.storage.query_ids():
            query = self.sample.query(query_id)
            result.append(self.query_status(query))
        return {
            "schema_version": 1,
            "ok": True,
            "database": str(self.db_path.resolve()),
            "queries": sorted(result, key=lambda item: item["query_id"]),
        }

    def verify_query(self, query: QuerySpec) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        record = self.storage.get_query(query.query_id)
        if record is None:
            return {
                "query_id": query.query_id,
                "ok": False,
                "errors": [{"code": "E_STATE_MISSING", "message": "query state is missing"}],
            }
        expected_spec_hash = sha256_json(query.to_dict())
        if record["spec_hash"] != expected_spec_hash:
            errors.append(
                {
                    "code": "E_SPEC_HASH",
                    "message": "persisted query spec hash differs from sample",
                }
            )
        if record["sample_fingerprint"] != self.sample.fingerprint:
            errors.append(
                {
                    "code": "E_SAMPLE_FINGERPRINT",
                    "message": "persisted sample fingerprint differs",
                }
            )
        staged = self.storage.staged_batches(query.query_id)
        prepared = [row for row in staged if row["status"] == "prepared"]
        if prepared:
            errors.append(
                {
                    "code": "E_PREPARED_BATCHES",
                    "message": "prepared batches remain",
                    "batch_indexes": [row["batch_index"] for row in prepared],
                }
            )
        committed = self.storage.committed_batches(query.query_id)
        if len(committed) != len(self.sample.batches):
            errors.append(
                {
                    "code": "E_BATCH_COUNT",
                    "message": "committed batch count differs from sample",
                    "committed": len(committed),
                    "expected": len(self.sample.batches),
                }
            )
        for expected, stored in zip(self.sample.batches, committed):
            if (
                expected.ordinal != int(stored["ordinal"])
                or expected.batch_index != int(stored["batch_index"])
                or expected.row_count != int(stored["row_count"])
                or expected.actual_digest != stored["actual_digest"]
            ):
                errors.append(
                    {
                        "code": "E_BATCH_MISMATCH",
                        "message": "persisted batch metadata differs",
                        "ordinal": expected.ordinal,
                    }
                )
                break
        exact_groups = replay_exact(self.sample.batches, query)
        consistency, replay_accelerated = self._replay_accelerated_consistency(query)
        if not consistency["ok"]:
            errors.append(
                {
                    "code": "E_PATH_DIVERGENCE",
                    "message": "exact and accelerated replay diverged",
                    "detail": consistency["first_divergence"],
                }
            )
        try:
            normal, hot, stats = self.storage.load_durable_state(query, mutate_stats=False)
            actual_groups = merge_all_channels(normal, hot, query)
            diff = compare_group_states(exact_groups, actual_groups)
            if diff is not None:
                errors.append(
                    {
                        "code": "E_STATE_DIVERGENCE",
                        "message": "durable group state differs from exact replay",
                        "detail": diff,
                    }
                )
            replay_diff = compare_group_states(replay_accelerated, actual_groups)
            if replay_diff is not None:
                errors.append(
                    {
                        "code": "E_CHANNEL_REPLAY_DIVERGENCE",
                        "message": "durable state differs from channel replay",
                        "detail": replay_diff,
                    }
                )
        except GSBError as exc:
            errors.append(exc.to_dict()["error"])
            stats = {}
        self._verify_mode_clock(query, errors)
        self._verify_hotness(query, errors)
        self._verify_group_versions(query, errors)
        result_ok = self._verify_result_file(query, record, exact_groups, errors)
        report = {
            "query_id": query.query_id,
            "ok": not errors and result_ok,
            "errors": errors,
            "checks": {
                "sample_fingerprint": record["sample_fingerprint"] == self.sample.fingerprint,
                "spec_hash": record["spec_hash"] == expected_spec_hash,
                "committed_batches": len(committed),
                "exact_groups": len(exact_groups),
                "state_digest": state_digest(exact_groups),
                "spill_stats": stats,
            },
            "result_digest": record["result_digest"],
        }
        return report

    def _verify_mode_clock(self, query: QuerySpec, errors: list[dict[str, Any]]) -> None:
        decisions = {int(event["ordinal"]): event for event in self.storage.mode_events(query.query_id)}
        rows_by_ordinal = {batch.ordinal: batch.row_count for batch in self.sample.batches}
        mode = EXACT_MODE
        elapsed = 0.0
        overhead_used = False
        for batch in self.sample.batches:
            decision = decide_mode(
                self.sample.config,
                mode,
                elapsed,
                batch.ordinal,
                rows_by_ordinal,
                overhead_used,
            )
            stored = decisions.get(batch.ordinal)
            if stored is None:
                errors.append(
                    {
                        "code": "E_MODE_EVENT_MISSING",
                        "message": "mode decision event is missing",
                        "ordinal": batch.ordinal,
                    }
                )
                return
            if (
                stored["mode"] != decision.mode
                or not same_json_bitwise(
                    float(stored["elapsed_after_ms"]), decision.elapsed_after_ms
                )
                or bool(stored["switched"]) != decision.switched
            ):
                errors.append(
                    {
                        "code": "E_MODE_REPLAY",
                        "message": "persisted mode or logical clock differs from replay",
                        "ordinal": batch.ordinal,
                        "stored": stored,
                        "expected": decision.to_dict(),
                    }
                )
                return
            mode = decision.mode
            elapsed = decision.elapsed_after_ms
            if decision.switched:
                overhead_used = True
        record = self.storage.get_query(query.query_id)
        assert record is not None
        if not same_json_bitwise(float(record["elapsed_ms"]), elapsed):
            errors.append(
                {
                    "code": "E_CLOCK_WATERMARK",
                    "message": "query logical clock watermark differs from replay",
                    "stored": record["elapsed_ms"],
                    "expected": elapsed,
                }
            )
        if record["current_mode"] != mode:
            errors.append(
                {
                    "code": "E_MODE_WATERMARK",
                    "message": "query mode watermark differs from replay",
                    "stored": record["current_mode"],
                    "expected": mode,
                }
            )

    def _verify_hotness(self, query: QuerySpec, errors: list[dict[str, Any]]) -> None:
        tracker = HotTracker(
            self.sample.config.hot_key_min_rows,
            self.sample.config.skew_ratio,
        )
        expected_events: list[dict[str, Any]] = []
        for batch in self.sample.batches:
            event = tracker.step(batch, aggregate_batch_partial(batch, query))
            if event is not None:
                expected_events.append(event)
        record = self.storage.get_query(query.query_id)
        assert record is not None
        stored_state = json.loads(record["hot_state_json"])
        if not same_json_bitwise(stored_state, tracker.to_dict()):
            errors.append(
                {
                    "code": "E_HOTNESS_STATE",
                    "message": "persisted streaming hotspot state differs from replay",
                }
            )
        stored_events = self.storage.hot_events(query.query_id)
        if not same_json_bitwise(stored_events, expected_events):
            errors.append(
                {
                    "code": "E_HOTNESS_EVENTS",
                    "message": "persisted hotspot change events differ from replay",
                }
            )

    def _verify_group_versions(self, query: QuerySpec, errors: list[dict[str, Any]]) -> None:
        for batch in self.sample.batches:
            partial = aggregate_batch_partial(batch, query)
            expected = set(partial)
            rows = self.storage.connection.execute(
                "SELECT * FROM group_state_versions WHERE query_id = ? AND ordinal = ?",
                (query.query_id, batch.ordinal),
            ).fetchall()
            actual = {row["group_token"] for row in rows}
            if actual != expected:
                errors.append(
                    {
                        "code": "E_GROUP_VERSION_LEDGER",
                        "message": "group state version ledger differs from partial keys",
                        "ordinal": batch.ordinal,
                        "missing": sorted(expected - actual),
                        "extra": sorted(actual - expected),
                    }
                )
                return
            if any(int(row["version"]) != batch.ordinal for row in rows):
                errors.append(
                    {
                        "code": "E_GROUP_VERSION",
                        "message": "group state version does not match batch ordinal",
                        "ordinal": batch.ordinal,
                    }
                )
                return

    def _verify_result_file(
        self,
        query: QuerySpec,
        record: Any,
        exact_groups: dict[str, dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> bool:
        if not record["output_path"] or not record["result_json"]:
            errors.append(
                {
                    "code": "E_RESULT_MISSING",
                    "message": "persisted result is missing",
                }
            )
            return False
        path = Path(record["output_path"])
        if not path.is_file():
            errors.append(
                {
                    "code": "E_RESULT_FILE",
                    "message": "result output file is missing",
                    "path": str(path),
                }
            )
            return False
        try:
            file_payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(
                {
                    "code": "E_RESULT_FILE",
                    "message": "result output file cannot be parsed",
                    "path": str(path),
                    "cause": str(exc),
                }
            )
            return False
        stored_payload = json.loads(record["result_json"])
        if not same_json_bitwise(file_payload, stored_payload):
            errors.append(
                {
                    "code": "E_RESULT_FILE",
                    "message": "result output file differs from persisted result",
                    "path": str(path),
                    "first_difference": first_difference(file_payload, stored_payload),
                }
            )
            return False
        expected_digest = sha256_json(
            {key: value for key, value in stored_payload.items() if key != "result_digest"}
        )
        if expected_digest != stored_payload.get("result_digest"):
            errors.append(
                {
                    "code": "E_RESULT_DIGEST",
                    "message": "embedded result digest is invalid",
                }
            )
            return False
        if expected_digest != record["result_digest"]:
            errors.append(
                {
                    "code": "E_RESULT_DIGEST",
                    "message": "stored result digest differs from result content",
                }
            )
            return False
        if query.window is None:
            groups = stored_payload.get("groups")
            expected = build_result_payload(
                query,
                exact_groups,
                self.sample.batches,
                actual_mode=str(record["current_mode"]),
                mode_history=stored_payload.get("mode_history", []),
                consistency=stored_payload.get("consistency", {}),
            )["groups"]
            if not same_json_bitwise(groups, expected):
                errors.append(
                    {
                        "code": "E_RESULT_DIVERGENCE",
                        "message": "output groups differ from exact truth replay",
                        "first_difference": first_difference(groups, expected),
                    }
                )
                return False
        else:
            expected_windows, rows_total = build_window_groups(
                self.sample.batches, query
            )
            if not same_json_bitwise(stored_payload.get("windows"), expected_windows):
                errors.append(
                    {
                        "code": "E_RESULT_DIVERGENCE",
                        "message": "window output differs from exact row-window replay",
                        "first_difference": first_difference(
                            stored_payload.get("windows"), expected_windows
                        ),
                    }
                )
                return False
            if stored_payload.get("total_rows") != rows_total:
                errors.append(
                    {
                        "code": "E_RESULT_DIVERGENCE",
                        "message": "window total_rows differs from sample",
                    }
                )
                return False
        return True

    def verify_all(self) -> dict[str, Any]:
        queries = [self.verify_query(query) for query in self.sample.queries]
        ok = all(item["ok"] for item in queries)
        return {
            "schema_version": 1,
            "ok": ok,
            "database": str(self.db_path.resolve()),
            "queries": sorted(queries, key=lambda item: item["query_id"]),
        }


def standalone_status(db_path: Path, work_dir: Path) -> dict[str, Any]:
    storage = Storage(db_path, work_dir)
    try:
        result: list[dict[str, Any]] = []
        for query_id in storage.query_ids():
            record = storage.get_query(query_id)
            assert record is not None
            config = json.loads(record["config_json"])
            tracker = HotTracker(
                int(config["hot_key_min_rows"]),
                float(config["skew_ratio"]),
                json.loads(record["hot_state_json"]),
            )
            hot = tracker.report()
            decisions = [json.loads(row["event_json"]) for row in storage.connection.execute(
                "SELECT event_json FROM mode_events WHERE query_id = ? ORDER BY ordinal",
                (query_id,),
            ).fetchall()]
            spills = [json.loads(row["event_json"]) for row in storage.connection.execute(
                "SELECT event_json FROM spill_events WHERE query_id = ? ORDER BY generation",
                (query_id,),
            ).fetchall()]
            stats = json.loads(record["stats_json"])
            result.append(
                {
                    "query_id": query_id,
                    "status": record["status"],
                    "mode": {
                        "final_mode": record["current_mode"],
                        "switch_count": sum(
                            1 for event in decisions if event.get("switched")
                        ),
                        "switches": [
                            event for event in decisions if event.get("switched")
                        ],
                    },
                    "latency": {
                        "clock_source": "injected_logical_clock",
                        "wall_clock_used": False,
                        "elapsed_ms": float(record["elapsed_ms"]),
                        "budget_ms": float(config["time_budget_ms"]),
                        "within_budget": float(record["elapsed_ms"])
                        <= float(config["time_budget_ms"]),
                    },
                    "hotspots": {
                        "current_version": hot["current_version"],
                        "hot_keys": hot["hot_keys"],
                        "observations": hot["observations"],
                        "changes": hot["changes"],
                    },
                    "spills": {
                        "spill_count": int(stats.get("spills", 0)),
                        "spill_groups": int(stats.get("spill_groups", 0)),
                        "spill_bytes": int(stats.get("spill_bytes", 0)),
                        "readback_count": int(stats.get("readbacks", 0)),
                        "readback_groups": int(stats.get("readback_groups", 0)),
                        "readback_bytes": int(stats.get("readback_bytes", 0)),
                        "events": spills,
                    },
                    "watermark": {
                        "ordinal": int(record["watermark_ordinal"]),
                        "batch_index": record["watermark_batch_index"],
                        "applied_batch_count": int(record["applied_batch_count"]),
                    },
                    "recovery": {
                        "prepared_recovered": int(stats.get("prepared_recovered", 0)),
                        "prepared_discarded": int(stats.get("prepared_discarded", 0)),
                        "half_committed_repaired": int(
                            stats.get("half_committed_repaired", 0)
                        ),
                    },
                    "memory_leases": storage.lease_summary(query_id),
                    "result_digest": record["result_digest"],
                    "result_path": record["output_path"],
                }
            )
        return {
            "schema_version": 1,
            "ok": True,
            "database": str(db_path.resolve()),
            "queries": sorted(result, key=lambda item: item["query_id"]),
        }
    finally:
        storage.close()

