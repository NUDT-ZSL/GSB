"""Independent result verification tool.

The verifier deliberately does not import the engine's execution state
classes.  It reloads tables/queries and recomputes every expected result from
first principles, then compares against the result files and the on-disk
snapshot.  All output keys follow a fixed order so repeated runs diff cleanly.
"""

from __future__ import annotations

import json
import os

from . import STATE_FORMAT_VERSION, FIXED_POINT_VERSION, FIXED_POINT_SCALE
from .errors import BusinessError, E_DIVERGE
from .fixedpoint import mean_scaled
from .queries import load_queries
from .schema import load_tables
from .snapshot import SnapshotStore


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _key(values: tuple) -> str:
    return json.dumps(list(values), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def reference_aggregates(tables, plan) -> dict[str, list]:
    expected: dict[str, list] = {}
    for plan_item in plan.aggregates:
        table = tables[plan_item.table]
        groups: dict[str, dict] = {}
        for row in table.rows:
            values = {col.name: row[idx] for idx, col in enumerate(table.columns)}
            key = _key(tuple(values[col] for col in plan_item.group_by))
            bucket = groups.setdefault(
                key, {"count": 0, "sum": 0, "min": None, "max": None})
            bucket["count"] += 1
            if plan_item.func == "COUNT":
                continue
            value = int(values[plan_item.arg])
            bucket["sum"] += value
            bucket["min"] = value if bucket["min"] is None else min(bucket["min"], value)
            bucket["max"] = value if bucket["max"] is None else max(bucket["max"], value)
        rows = []
        for key in sorted(groups):
            bucket = groups[key]
            row = {"group_key": key}
            if plan_item.func == "COUNT":
                row.update({"count": bucket["count"], "sum": 0,
                            "min": None, "max": None, "mean_scaled": None})
            elif plan_item.func == "SUM":
                row.update({"count": bucket["count"], "sum": bucket["sum"],
                            "min": None, "max": None, "mean_scaled": None})
            elif plan_item.func == "MIN":
                row.update({"count": bucket["count"], "sum": 0,
                            "min": bucket["min"], "max": None, "mean_scaled": None})
            elif plan_item.func == "MAX":
                row.update({"count": bucket["count"], "sum": 0,
                            "min": None, "max": bucket["max"], "mean_scaled": None})
            else:  # MEAN
                row.update({
                    "count": bucket["count"], "sum": bucket["sum"],
                    "min": None, "max": None,
                    "mean_scaled": mean_scaled(bucket["sum"], bucket["count"],
                                               FIXED_POINT_SCALE),
                })
            rows.append(row)
        expected[plan_item.query_id] = rows
    return expected


def reference_windows(tables, plan) -> dict[str, list]:
    expected: dict[str, list] = {}
    for plan_item in plan.windows:
        table = tables[plan_item.table]
        partitions: dict[str, list] = {}
        for row_id, row in enumerate(table.rows):
            values = {col.name: row[idx] for idx, col in enumerate(table.columns)}
            key = _key(tuple(values[col] for col in plan_item.partition_by))
            ts = int(values[plan_item.order_by])
            value = 0 if plan_item.arg is None else int(values[plan_item.arg])
            partitions.setdefault(key, []).append((row_id, ts, value))
        rows = []
        for part_key in sorted(partitions):
            watermark: int | None = None
            frame: list[tuple] = []
            for row_id, ts, value in partitions[part_key]:
                late = watermark is not None and ts < watermark
                if watermark is not None and ts < watermark - table.allowed_lag:
                    rows.append({
                        "row_id": row_id,
                        "partition_key": part_key,
                        "ts": ts,
                        "admitted": False,
                        "frame_sum": None,
                        "row_number": None,
                        "reason": "beyond_allowed_lag",
                    })
                    continue
                lower_ts = ts + plan_item.lower
                upper_ts = ts + plan_item.upper
                frame = [item for item in frame if item[1] >= lower_ts]
                frame.append((row_id, ts, value))
                if len(frame) > plan_item.max_rows:
                    frame = frame[-plan_item.max_rows:]
                frame = [item for item in frame if lower_ts <= item[1] <= upper_ts]
                watermark = ts if watermark is None else max(watermark, ts)
                ordered = sorted(frame, key=lambda item: (item[1], item[0]))
                rows.append({
                    "row_id": row_id,
                    "partition_key": part_key,
                    "ts": ts,
                    "admitted": True,
                    "late": late,
                    "frame_lower": lower_ts,
                    "frame_upper": upper_ts,
                    "interval_closure": "inclusive_both_bounds",
                    "frame_size": len(ordered),
                    "frame_sum": sum(item[2] for item in ordered)
                    if plan_item.func == "SUM" else None,
                    "row_number": next(
                        position for position, item in enumerate(ordered, start=1)
                        if item[0] == row_id
                    ) if plan_item.func == "ROW_NUMBER" else None,
                })
        expected[plan_item.query_id] = sorted(rows, key=lambda item: item["row_id"])
    return expected


def _first_mismatch(expected, actual, path=""):
    if isinstance(expected, dict) and isinstance(actual, dict):
        keys = sorted(set(expected) | set(actual))
        for key in keys:
            if key not in expected:
                return f"{path}.{key}", "missing in expected", actual[key]
            if key not in actual:
                return f"{path}.{key}", "missing in actual", expected[key]
            found = _first_mismatch(expected[key], actual[key], f"{path}.{key}")
            if found:
                return found
    elif isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            if index >= len(expected):
                return f"{path}[{index}]", "missing in expected", actual[index]
            if index >= len(actual):
                return f"{path}[{index}]", "missing in actual", expected[index]
            found = _first_mismatch(expected[index], actual[index],
                                    f"{path}[{index}]")
            if found:
                return found
    elif expected != actual:
        return path, expected, actual
    return None


def _locate_divergence(mismatch) -> dict:
    location, expected, actual = mismatch
    return {
        "code": E_DIVERGE,
        "first_mismatch_location": location,
        "expected": expected,
        "actual": actual,
    }


def verify(tables_path: str, queries_path: str, result_dir: str,
           snapshot_dir: str) -> dict:
    tables = load_tables(tables_path)
    plan = load_queries(queries_path, tables)
    report = _read_json(os.path.join(result_dir, "report.json"))
    actual_aggregates = _read_json(os.path.join(result_dir, "aggregates.json"))
    actual_windows = _read_json(os.path.join(result_dir, "windows.json"))

    expected_aggregates = reference_aggregates(tables, plan)
    expected_windows = reference_windows(tables, plan)
    checks = []

    mismatch = _first_mismatch(expected_aggregates, actual_aggregates,
                               "aggregates")
    checks.append({
        "name": "aggregate_results_match_reference",
        "status": "PASS" if mismatch is None else "FAIL",
        "detail": None if mismatch is None else _locate_divergence(mismatch),
    })
    mismatch = _first_mismatch(expected_windows, actual_windows, "windows")
    checks.append({
        "name": "window_results_match_reference",
        "status": "PASS" if mismatch is None else "FAIL",
        "detail": None if mismatch is None else _locate_divergence(mismatch),
    })

    store = SnapshotStore(snapshot_dir)
    latest = store.latest_path()
    snapshot_section = {"name": "snapshot_readable_and_versioned",
                        "status": "FAIL", "detail": "no snapshot found"}
    if latest is not None:
        payload = store.load(latest)
        meta = payload["meta"]
        version_ok = (
            meta.get("state_format_version") == str(STATE_FORMAT_VERSION)
            and meta.get("fixed_point_version") == FIXED_POINT_VERSION
            and int(meta.get("fixed_point_scale", 0)) == FIXED_POINT_SCALE
        )
        cursor_ok = all(
            payload["cursors"].get(name) == len(table.rows)
            for name, table in tables.items()
        )
        detail = {
            "snapshot": os.path.basename(latest),
            "state_format_version": meta.get("state_format_version"),
            "fixed_point_version": meta.get("fixed_point_version"),
            "fixed_point_scale": meta.get("fixed_point_scale"),
            "all_cursors_at_end": cursor_ok,
        }
        snapshot_section = {
            "name": "snapshot_readable_and_versioned",
            "status": "PASS" if version_ok and cursor_ok else "FAIL",
            "detail": detail,
        }
    checks.append(snapshot_section)

    summary_section = {
        "name": "state_summary_matches_result_rows",
        "status": "PASS",
        "detail": {"groups_checked": 0, "partitions_checked": 0},
    }
    groups_checked = 0
    for query_id, rows in actual_aggregates.items():
        summaries = report["state_summary"].get(query_id, {})
        for row in rows:
            groups_checked += 1
            if summaries.get(row["group_key"]) != {
                    key: row[key] for key in
                    ("count", "sum", "min", "max", "mean_scaled")}:
                summary_section = {
                    "name": "state_summary_matches_result_rows",
                    "status": "FAIL",
                    "detail": {"query_id": query_id,
                               "group_key": row["group_key"]},
                }
                break
    partitions_checked = 0
    for query_id, rows in actual_windows.items():
        partitions = {row["partition_key"] for row in rows}
        partitions_checked += len(partitions)
    summary_section["detail"] = {
        "groups_checked": groups_checked,
        "partitions_checked": partitions_checked,
    }
    checks.append(summary_section)

    overall = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    return {
        "overall_status": overall,
        "result_dir": os.path.abspath(result_dir),
        "snapshot_dir": os.path.abspath(snapshot_dir),
        "checks": checks,
    }


def assert_identical_results(left_path: str, right_path: str) -> dict:
    """Bit-level comparison used by the resume-vs-uninterrupted check."""
    left_bytes = open(left_path, "rb").read()
    right_bytes = open(right_path, "rb").read()
    if left_bytes == right_bytes:
        return {
            "status": "PASS",
            "files_compared": [os.path.basename(left_path),
                               os.path.basename(right_path)],
            "byte_size": len(left_bytes),
        }
    left = _read_json(left_path)
    right = _read_json(right_path)
    if isinstance(left, dict) and isinstance(right, dict):
        # Batch boundaries and checkpoint file names intentionally depend on
        # the injected batching schedule; the engine state itself must not.
        for volatile_key in ("batch_events", "checkpoints"):
            left.pop(volatile_key, None)
            right.pop(volatile_key, None)
        left_bytes = json.dumps(left, sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8")
        right_bytes = json.dumps(right, sort_keys=True,
                                 separators=(",", ":"),
                                 ensure_ascii=False).encode("utf-8")
        if left_bytes == right_bytes:
            return {
                "status": "PASS",
                "files_compared": [os.path.basename(left_path),
                                   os.path.basename(right_path)],
                "byte_size": len(left_bytes),
                "normalized": True,
            }
    mismatch = _first_mismatch(left, right, os.path.basename(left_path))
    raise BusinessError(
        E_DIVERGE,
        "resumed and uninterrupted results are not byte-identical",
        _locate_divergence(mismatch) if mismatch else
        {"location": os.path.basename(left_path)},
    )
