"""Streaming execution engine with resumable aggregate/window state.

The engine advances table rows in deterministic batches.  Aggregate groups and
window ring buffers live entirely in snapshot-able structures, so a resumed
run rebuilds them from SQLite and continues from the per-table cursor.

Fault injection switches only alter files/parameters; the engine source stays
free of test-specific branches except the explicit checkpoint boundary hooks
passed in by the CLI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import FIXED_POINT_SCALE, STATE_FORMAT_VERSION, FIXED_POINT_VERSION
from .errors import BusinessError, E_BATCH, E_ORDER
from .fixedpoint import fixed_point_spec, quant_error_bound
from .queries import AggregatePlan, ExecutionPlan, WindowPlan
from .ring import RingBuffer, RingEntry
from .schema import Table
from .snapshot import SnapshotStore
from .state import GroupState, WindowPartitionState


def _key_text(values: tuple) -> str:
    return json.dumps(list(values), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


@dataclass
class TableRuntime:
    table: Table
    cursor: int = 0
    watermark: int | None = None
    out_of_order_count: int = 0
    in_order_count: int = 0

    @property
    def ooo_ratio(self) -> float:
        total = self.out_of_order_count + self.in_order_count
        return 0.0 if total == 0 else self.out_of_order_count / total


@dataclass
class EngineReport:
    params_digest: str
    replaced_state: bool
    cursors: dict
    aggregate_results: dict
    window_results: dict
    state_summary: dict
    batch_events: list
    order_events: list
    trace: list
    checkpoints: list
    fixed_point: dict


class StreamingEngine:
    def __init__(self, tables: dict[str, Table], plan: ExecutionPlan,
                 snapshot_dir: str, batch_sizes: dict[str, int] | None = None,
                 checkpoint_hook=None):
        self.tables = tables
        self.plan = plan
        self.store = SnapshotStore(snapshot_dir)
        self.batch_sizes = batch_sizes or {}
        self.checkpoint_hook = checkpoint_hook
        self.params_digest = self._compute_params_digest(plan)
        self.runtimes = {name: TableRuntime(table) for name, table in tables.items()}
        self.agg_state: dict[str, dict[str, GroupState]] = {
            plan_item.query_id: {} for plan_item in plan.aggregates
        }
        self.win_state: dict[str, dict[str, WindowPartitionState]] = {
            plan_item.query_id: {} for plan_item in plan.windows
        }
        self.emissions: dict[str, dict[int, dict]] = {
            plan_item.query_id: {} for plan_item in plan.windows
        }
        self.batch_events: list[dict] = []
        self.order_events: list[dict] = []
        self.checkpoints: list[str] = []
        self.replaced_state = False
        self._next_row_id = 0

    # ------------------------------------------------------------------ #
    # Plan identity / snapshot lifecycle
    # ------------------------------------------------------------------ #
    @staticmethod
    def _compute_params_digest(plan: ExecutionPlan) -> str:
        import hashlib
        canonical_lines = [item.canonical for item in plan.all_plans()]
        joined = "\n".join(sorted(canonical_lines))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def restore(self) -> None:
        """Load the latest snapshot; replace whole state on version mismatch."""
        path = self.store.latest_path()
        if path is None:
            return
        payload = self.store.load(path)
        meta = payload["meta"]
        format_match = meta.get("state_format_version") == str(STATE_FORMAT_VERSION)
        fp_match = meta.get("fixed_point_version") == FIXED_POINT_VERSION
        params_match = meta.get("params_digest") == self.params_digest
        if not (format_match and fp_match and params_match):
            self.replaced_state = True
            self.agg_state = {q.query_id: {} for q in self.plan.aggregates}
            self.win_state = {q.query_id: {} for q in self.plan.windows}
            self.emissions = {q.query_id: {} for q in self.plan.windows}
            self._next_row_id = 0
            return
        for table_name, cursor in payload["cursors"].items():
            if table_name in self.runtimes:
                self.runtimes[table_name].cursor = int(cursor)
        for qid, groups in payload["agg_states"].items():
            self.agg_state[qid] = {
                key: GroupState.from_state(state) for key, state in groups.items()
            }
        max_rows_by_query = {q.query_id: q.max_rows for q in self.plan.windows}
        for qid, partitions in payload["win_states"].items():
            self.win_state[qid] = {
                key: WindowPartitionState.from_state(
                    state, max_rows_by_query.get(qid, 1000))
                for key, state in partitions.items()
            }
        self.emissions = {
            qid: {int(row_id): emission for row_id, emission in rows.items()}
            for qid, rows in payload["emissions"].items()
        }
        self._next_row_id = max(
            (runtime.cursor for runtime in self.runtimes.values()),
            default=0,
        )

    def checkpoint(self, name: str) -> str:
        payload = {
            "params_digest": self.params_digest,
            "cursors": {
                table_name: runtime.cursor
                for table_name, runtime in sorted(self.runtimes.items())
            },
            "agg_states": {
                qid: {key: state.to_state() for key, state in sorted(groups.items())}
                for qid, groups in self.agg_state.items()
            },
            "win_states": {
                qid: {
                    key: partition.to_state()
                    for key, partition in sorted(partitions.items())
                }
                for qid, partitions in self.win_state.items()
            },
            "emissions": self.emissions,
            "trace": [
                {"query_id": item.query_id,
                 "canonical": item.canonical,
                 "digest": item.digest}
                for item in self.plan.all_plans()
            ],
        }
        path = self.store.save(name, payload)
        self.checkpoints.append(name)
        return path

    # ------------------------------------------------------------------ #
    # Row processing
    # ------------------------------------------------------------------ #
    def _row_values(self, table: Table, row: list) -> dict[str, object]:
        return {col.name: row[idx] for idx, col in enumerate(table.columns)}

    def _process_aggregate(self, plan_item: AggregatePlan, row_id: int,
                           values: dict[str, object]) -> None:
        groups = self.agg_state[plan_item.query_id]
        group_key = _key_text(tuple(values[col] for col in plan_item.group_by))
        state = groups.get(group_key)
        if state is None:
            state = GroupState()
            groups[group_key] = state
        if plan_item.func == "COUNT":
            if row_id not in state.seen_row_ids:
                state.seen_row_ids.add(row_id)
                state.count += 1
        elif plan_item.func == "SUM":
            state.track_sum(row_id, int(values[plan_item.arg]))
        elif plan_item.func in ("MIN", "MAX"):
            state.track_extreme(row_id, int(values[plan_item.arg]),
                                 plan_item.func == "MIN")
        else:
            state.track_mean(row_id, int(values[plan_item.arg]))

    def _process_window(self, plan_item: WindowPlan, table: Table,
                        row_id: int, values: dict[str, object]) -> None:
        partitions = self.win_state[plan_item.query_id]
        part_key = _key_text(
            tuple(values[col] for col in plan_item.partition_by))
        partition = partitions.get(part_key)
        if partition is None:
            partition = WindowPartitionState(RingBuffer(plan_item.max_rows))
            partitions[part_key] = partition

        ts = int(values[plan_item.order_by])
        if partition.watermark is not None and ts < partition.watermark:
            runtime = self.runtimes[table.name]
            if not ring_admits(partition.ring, ts, partition.watermark,
                               table.allowed_lag):
                self.emissions[plan_item.query_id][row_id] = {
                    "row_id": row_id,
                    "partition_key": part_key,
                    "ts": ts,
                    "admitted": False,
                    "frame_sum": None,
                    "row_number": None,
                    "reason": "beyond_allowed_lag",
                }
                return
            late = True
        else:
            late = False

        value = None if plan_item.arg is None else int(values[plan_item.arg])
        entry = RingEntry(row_id=row_id, ts=ts,
                          value=value if value is not None else 0)
        lower_ts = ts + plan_item.lower
        upper_ts = ts + plan_item.upper
        partition.ring.expire_before(lower_ts)
        partition.ring.push(entry)
        if late:
            partition.ring.recompute_sum()
        partition.watermark = ts if partition.watermark is None else max(
            partition.watermark, ts)

        frame_entries = [
            item for item in partition.ring.entries()
            if lower_ts <= item.ts <= upper_ts
        ]
        frame_sum = sum(item.value for item in frame_entries)
        row_number = None
        if plan_item.func == "ROW_NUMBER":
            ordered = sorted(frame_entries, key=lambda item: (item.ts, item.row_id))
            row_number = next(
                position for position, item in enumerate(ordered, start=1)
                if item.row_id == row_id
            )
        self.emissions[plan_item.query_id][row_id] = {
            "row_id": row_id,
            "partition_key": part_key,
            "ts": ts,
            "admitted": True,
            "late": late,
            "frame_lower": lower_ts,
            "frame_upper": upper_ts,
            "interval_closure": "inclusive_both_bounds",
            "frame_size": len(frame_entries),
            "frame_sum": frame_sum if plan_item.func == "SUM" else None,
            "row_number": row_number,
        }

    def _process_row(self, table: Table, row_id: int, row: list) -> None:
        values = self._row_values(table, row)
        ts = int(values[table.timestamp_column])
        runtime = self.runtimes[table.name]
        if runtime.watermark is not None and ts < runtime.watermark:
            runtime.out_of_order_count += 1
            if runtime.ooo_ratio > table.ooo_ratio_limit:
                event = {
                    "code": E_ORDER,
                    "table": table.name,
                    "row": runtime.cursor + 1,
                    "timestamp": ts,
                    "watermark": runtime.watermark,
                    "out_of_order_ratio": round(runtime.ooo_ratio, 6),
                    "limit": table.ooo_ratio_limit,
                }
                self.order_events.append(event)
                raise BusinessError(
                    E_ORDER,
                    "out-of-order ratio exceeded the declared table limit",
                    event,
                )
        else:
            runtime.in_order_count += 1
        runtime.watermark = ts if runtime.watermark is None else max(
            runtime.watermark, ts)

        for plan_item in self.plan.aggregates:
            if plan_item.table == table.name:
                self._process_aggregate(plan_item, row_id, values)
        for plan_item in self.plan.windows:
            if plan_item.table == table.name:
                self._process_window(plan_item, table, row_id, values)
        runtime.cursor += 1

    # ------------------------------------------------------------------ #
    # Batch driving
    # ------------------------------------------------------------------ #
    def run(self) -> EngineReport:
        for table_name in sorted(self.tables):
            table = self.tables[table_name]
            runtime = self.runtimes[table_name]
            batch_size = max(1, int(self.batch_sizes.get(table_name, 1000)))
            while runtime.cursor < len(table.rows):
                start = runtime.cursor
                end = min(start + batch_size, len(table.rows))
                open_at_start = self._has_open_window(table)
                for row_index in range(start, end):
                    self._process_row(table, row_index, table.rows[row_index])
                    self._next_row_id = max(self._next_row_id, row_index + 1)
                batch_no = len(self.batch_events) + 1
                snapshot_name = f"{table_name}_batch_{batch_no:04d}.sqlite3"
                mid_window = open_at_start or self._has_open_window(table)
                self.checkpoint(snapshot_name)
                event = {
                    "code": E_BATCH if mid_window else "OK",
                    "table": table_name,
                    "batch_no": batch_no,
                    "row_range": [start + 1, end],
                    "checkpoint": snapshot_name,
                    "boundary_in_open_window": bool(mid_window),
                }
                self.batch_events.append(event)
                if self.checkpoint_hook is not None:
                    self.checkpoint_hook(snapshot_name, event, self)
        return self.build_report()

    def _has_open_window(self, table: Table) -> bool:
        for plan_item in self.plan.windows:
            if plan_item.table != table.name:
                continue
            for partition in self.win_state[plan_item.query_id].values():
                if len(partition.ring) > 0:
                    return True
        return False

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def build_report(self) -> EngineReport:
        aggregate_results: dict[str, list] = {}
        state_summary: dict[str, dict] = {}
        for plan_item in self.plan.aggregates:
            rows = []
            summaries = {}
            for group_key in sorted(self.agg_state[plan_item.query_id]):
                state = self.agg_state[plan_item.query_id][group_key]
                summary = state.summary(FIXED_POINT_SCALE)
                summaries[group_key] = summary
                rows.append({"group_key": group_key, **summary})
            aggregate_results[plan_item.query_id] = rows
            state_summary[plan_item.query_id] = summaries

        window_results: dict[str, list] = {}
        for plan_item in self.plan.windows:
            window_results[plan_item.query_id] = [
                self.emissions[plan_item.query_id][row_id]
                for row_id in sorted(self.emissions[plan_item.query_id])
            ]
            for partition_key in sorted(self.win_state[plan_item.query_id]):
                partition = self.win_state[plan_item.query_id][partition_key]
                state_summary.setdefault(plan_item.query_id, {})[partition_key] = {
                    "window": partition.to_state()
                }

        return EngineReport(
            params_digest=self.params_digest,
            replaced_state=self.replaced_state,
            cursors={name: rt.cursor for name, rt in sorted(self.runtimes.items())},
            aggregate_results=aggregate_results,
            window_results=window_results,
            state_summary=state_summary,
            batch_events=self.batch_events,
            order_events=self.order_events,
            trace=[
                {"query_id": item.query_id,
                 "canonical": item.canonical,
                 "digest": item.digest}
                for item in self.plan.all_plans()
            ],
            checkpoints=list(self.checkpoints),
            fixed_point=fixed_point_spec(FIXED_POINT_SCALE),
        )


def ring_admits(ring: RingBuffer, ts: int, watermark: int,
                allowed_lag: int) -> bool:
    return ts >= watermark - allowed_lag


def report_to_dict(report: EngineReport) -> dict:
    return {
        "params_digest": report.params_digest,
        "replaced_state": report.replaced_state,
        "cursors": report.cursors,
        "aggregate_results": report.aggregate_results,
        "window_results": report.window_results,
        "state_summary": report.state_summary,
        "batch_events": report.batch_events,
        "order_events": report.order_events,
        "trace": report.trace,
        "checkpoints": report.checkpoints,
        "fixed_point": report.fixed_point,
        "quant_error_bound": quant_error_bound(FIXED_POINT_SCALE),
    }


def write_results(result_dir: str, report: EngineReport) -> dict[str, str]:
    os.makedirs(result_dir, exist_ok=True)
    paths = {
        "report": os.path.join(result_dir, "report.json"),
        "aggregates": os.path.join(result_dir, "aggregates.json"),
        "windows": os.path.join(result_dir, "windows.json"),
        "trace": os.path.join(result_dir, "trace.json"),
    }
    documents = {
        "report": report_to_dict(report),
        "aggregates": report.aggregate_results,
        "windows": report.window_results,
        "trace": report.trace,
    }
    for key, path in paths.items():
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(documents[key], handle, sort_keys=True,
                      ensure_ascii=False, indent=2)
            handle.write("\n")
    return paths
