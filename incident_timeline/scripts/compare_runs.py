"""比较两次 SQLite 运行结果：用于验证增量重算与全量重算逐字段一致。

用法:
  python3 scripts/compare_runs.py --db data/incident.db --full-run 3 --incremental-run 2
其中 run 3 是对同一事件文件做的一次独立全量重算。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from incident_timeline.storage import connect, load_snapshot  # noqa: E402


VOLATILE = {"changed_vs_previous", "order"}


def compare(db_path: str, full_run: int, incremental_run: int) -> dict:
    connection = connect(db_path)
    try:
        full = load_snapshot(connection, full_run)
        inc = load_snapshot(connection, incremental_run)
    finally:
        connection.close()

    report = {"faults": [], "timeline_equal": True, "accounts_equal": True}
    full_faults = {item["fault_id"]: item for item in full["faults"]}
    for inc_fault in inc["faults"]:
        full_fault = full_faults.get(inc_fault["fault_id"])
        if full_fault is None:
            report["faults"].append({"fault_id": inc_fault["fault_id"], "equal": False,
                                     "reason": "全量结果中不存在该故障"})
            continue
        diff = {
            key: {"full": full_fault.get(key), "incremental": inc_fault.get(key)}
            for key in full_fault
            if full_fault.get(key) != inc_fault.get(key)
        }
        report["faults"].append(
            {"fault_id": inc_fault["fault_id"], "equal": not diff, "diff": diff}
        )

    full_entries = _index_entries(full["timeline"])
    inc_entries = _index_entries(inc["timeline"])
    if set(full_entries) != set(inc_entries):
        report["timeline_equal"] = False
    for fault_id, entry in inc_entries.items():
        other = dict(full_entries.get(fault_id, {}))
        current = dict(entry)
        for key in VOLATILE:
            other.pop(key, None)
            current.pop(key, None)
        if other != current:
            report["timeline_equal"] = False
            report.setdefault("timeline_diff", []).append(fault_id)

    for dimension in ("by_source", "by_service"):
        full_rows = {row["key"]: row for row in full["accounts"][dimension]}
        inc_rows = {row["key"]: row for row in inc["accounts"][dimension]}
        if full_rows != inc_rows:
            report["accounts_equal"] = False
            report.setdefault("account_diff", []).append(dimension)
    report["all_equal"] = (
        report["timeline_equal"]
        and report["accounts_equal"]
        and all(item["equal"] for item in report["faults"])
    )
    return report


def _index_entries(entries):
    return {entry["fault_id"]: entry for entry in entries}


def main() -> int:
    parser = argparse.ArgumentParser(description="全量与增量运行结果逐字段比对")
    parser.add_argument("--db", required=True)
    parser.add_argument("--full-run", type=int, required=True)
    parser.add_argument("--incremental-run", type=int, required=True)
    args = parser.parse_args()
    report = compare(args.db, args.full_run, args.incremental_run)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["all_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
