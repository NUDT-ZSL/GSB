"""主流程：工程 -> 对齐 -> 缺口 -> 裁决 -> 动作 -> 恢复点 -> 汇总报表。"""

from .alignment import align_project
from .gaps import build_actions, build_gap_intervals
from .recovery import settle_recovery_points


def build_report(project):
    aligned = align_project(project)
    gap_intervals = build_gap_intervals(project)
    actions = build_actions(gap_intervals, project)
    recovery = settle_recovery_points(project)
    summary = build_summary(project, aligned, gap_intervals, actions, recovery)

    return {
        "schema_version": "1.0",
        "report_meta": {
            "generator_seed": project["generator"]["seed"],
            "n_sites": project["generator"]["n_sites"],
            "batches_per_element": project["generator"]["batches_per_element"],
            "cadence_ms": project["generator"]["cadence_ms"],
            "merge_gap_ms": project["generator"]["merge_gap_ms"],
        },
        "level_policy": project["level_policy"],
        "events": project["events"],
        "alignment": aligned,
        "gap_intervals": gap_intervals,
        "actions": actions,
        "recovery_settlements": recovery,
        "site_element_summary": summary,
    }


def build_summary(project, aligned, gap_intervals, actions, recovery):
    """按站点与要素分组的缺口与恢复点达成汇总，逐项可追溯。"""
    actions_by_target = {}
    for action in actions:
        key = (action["site_id"], action["element"], action["target_end"])
        actions_by_target.setdefault(key, []).append(
            {
                "action_id": action["action_id"],
                "action_type": action["action_type"],
                "priority": action["priority"],
                "gap_id": action["gap_id"],
            }
        )

    gap_refs = {}
    for interval in gap_intervals:
        policy = project["level_policy"][interval["level"]]
        for element in interval["elements"]:
            key = (interval["site_id"], element, interval["end"])
            gap_refs.setdefault(key, []).append(
                {
                    "gap_id": interval["gap_id"],
                    "start_time": interval["start_time"],
                    "end_time": interval["end_time"],
                    "element_record_count": len(
                        interval["per_element_missing_seqs"].get(element, [])
                    ),
                    "interval_total_record_count": interval["record_count"],
                    "violated": interval["adjudication"]["violated"],
                    "rpo_ms": policy["rpo_ms"],
                    "rpo_unit": "ms",
                    "max_gap_records": policy["max_gap_records"],
                    "record_unit": "batch",
                    "time_excess_ms": interval["adjudication"]["time_excess_ms"],
                    "record_excess": interval["adjudication"]["record_excess"],
                }
            )

    latest_settlement = recovery[-1] if recovery else None
    recovery_by_key = {}
    if latest_settlement is not None:
        for row in latest_settlement["element_rows"]:
            recovery_by_key[(row["site_id"], row["element"])] = row
        unmet_site_ids = {row["site_id"] for row in latest_settlement["unmet_sites"]}
    else:
        unmet_site_ids = set()

    summary_rows = []
    for row in aligned:
        site_id = row["site_id"]
        element = row["element"]
        policy = project["level_policy"][row["level"]]
        related_gaps = (
            gap_refs.get((site_id, element, "primary"), [])
            + gap_refs.get((site_id, element, "backup"), [])
        )
        related_gaps.sort(key=lambda item: item["start_time"])
        related_actions = (
            actions_by_target.get((site_id, element, "primary"), [])
            + actions_by_target.get((site_id, element, "backup"), [])
        )
        related_actions.sort(key=lambda item: item["gap_id"])
        recovery_row = recovery_by_key.get((site_id, element))
        summary_rows.append(
            {
                "site_id": site_id,
                "level": row["level"],
                "element": element,
                "primary_missing_count": len(row["primary_missing_seqs"]),
                "backup_missing_count": len(row["backup_missing_seqs"]),
                "primary_missing_seqs": row["primary_missing_seqs"],
                "backup_missing_seqs": row["backup_missing_seqs"],
                "gap_interval_refs": related_gaps,
                "action_refs": related_actions,
                "thresholds": {
                    "rpo_ms": policy["rpo_ms"],
                    "rpo_unit": "ms",
                    "max_gap_records": policy["max_gap_records"],
                    "record_unit": "batch",
                },
                "latest_recovery_event": (
                    None if latest_settlement is None else latest_settlement["event_id"]
                ),
                "latest_recovery": (
                    None
                    if recovery_row is None
                    else {
                        "active_end": recovery_row["active_end"],
                        "last_committed_seq": recovery_row["last_committed_seq"],
                        "expected_seq": recovery_row["expected_seq"],
                        "lag_ms": recovery_row["lag_ms"],
                        "lag_records": recovery_row["lag_records"],
                        "missing_seqs": recovery_row["missing_seqs"],
                        "site_rpo_met": site_id not in unmet_site_ids,
                    }
                ),
            }
        )
    return summary_rows

