"""内置自检：独立参考核对器 + 三个人工样本 + 工程确定性重放。"""

import json

from .generator import BACKUP, PRIMARY, generate_project
from .gaps import build_gap_intervals
from .reference import check_report, reference_gap_intervals, reference_recovery
from .report import build_report
from .samples import (
    build_sample_partial_backfill,
    build_sample_same_commit_middle_missing,
    build_sample_threshold_boundary,
)


def run_self_check(seed=20261128, n_sites=8, batches_per_element=24):
    checks = []

    report, project = _check_deterministic(seed, n_sites, batches_per_element, checks)
    checks.append(
        _named_check(
            "generated_project_reference_cross_check",
            check_report(project, report),
            {"seed": seed},
        )
    )

    checks.append(_check_sample_middle_missing())
    checks.append(_check_sample_threshold_boundary())
    checks.append(_check_sample_partial_backfill())

    passed = all(item["passed"] for item in checks)
    return {"passed": passed, "checks": checks}


def _check_deterministic(seed, n_sites, batches_per_element, checks):
    first = generate_project(seed, n_sites=n_sites, batches_per_element=batches_per_element)
    second = generate_project(seed, n_sites=n_sites, batches_per_element=batches_per_element)
    replayed_equal = json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    checks.append(
        {
            "name": "deterministic_project_replay",
            "passed": replayed_equal,
            "detail": {"seed": seed, "byte_comparable_json_equal": replayed_equal},
        }
    )
    different = generate_project(seed + 1, n_sites=n_sites, batches_per_element=batches_per_element)
    seed_differs = json.dumps(first, sort_keys=True) != json.dumps(different, sort_keys=True)
    checks.append(
        {
            "name": "different_seed_produces_different_project",
            "passed": seed_differs,
            "detail": {"seed_a": seed, "seed_b": seed + 1},
        }
    )
    return build_report(first), first


def _named_check(name, result, detail):
    return {
        "name": name,
        "passed": result["ok"],
        "detail": result if result["ok"] else result["first_mismatch"],
        "context": detail,
    }


def _check_sample_middle_missing():
    project = build_sample_same_commit_middle_missing()
    report = build_report(project)
    cross = check_report(project, report)

    alignment = {
        (row["site_id"], row["element"]): row for row in report["alignment"]
    }
    a1 = alignment[("ST-A1", "TEMP")]
    a2 = alignment[("ST-A2", "TEMP")]
    common_time = {
        (record["site_id"], record["element"], record["seq"]): record["write_time"]
        for record in project["ends"]["primary"]["landed"]
    }
    backup_common = next(
        record for record in project["ends"]["backup"]["landed"]
        if (record["site_id"], record["element"], record["seq"]) == ("ST-A1", "TEMP", 1)
    )
    primary_intervals = [
        interval for interval in report["gap_intervals"] if interval["end"] == PRIMARY
    ]
    a1_interval = _find_interval(primary_intervals, "ST-A1")
    a2_interval = _find_interval(primary_intervals, "ST-A2")

    expectations = [
        ("same_last_commit_seq_7", a1["primary_present_seqs"][-1] == 7 and
         a1["backup_present_seqs"][-1] == 7),
        ("middle_missing_3_4_5_6", a1["primary_missing_seqs"] == [3, 4, 5, 6]),
        ("write_time_jitter_handled_on_common_batch",
         backup_common["write_time"] - common_time[("ST-A1", "TEMP", 1)] == 2_000),
        ("interval_crosses_interrupt_event", "EVT-1" in a1_interval["crossed_event_ids"]),
        ("interval_record_count_matches", a1_interval["record_count"] == 4),
        ("record_threshold_violated",
         a1_interval["adjudication"]["violated"] is True
         and a1_interval["adjudication"]["record_excess"] == 1
         and a1_interval["adjudication"]["time_excess_ms"] == 0),
        ("both_end_missing_needs_rebuild",
         a2_interval["per_element_missing_seqs"]["TEMP"] == [4]),
        ("backfill_action_for_middle_gap",
         len(report["actions"]) == 1
         and report["actions"][0]["action_type"] == "BACKFILL"
         and report["actions"][0]["target_seqs"] == [3, 4, 5, 6]
         and report["actions"][0]["gap_id"] == a1_interval["gap_id"]),
    ]
    return _sample_result(
        "sample_same_commit_middle_missing", cross, expectations,
        {"site": "ST-A1", "gap_id": a1_interval["gap_id"]},
    )


def _check_sample_threshold_boundary():
    project = build_sample_threshold_boundary()
    report = build_report(project)
    cross = check_report(project, report)

    primary_intervals = [
        interval for interval in report["gap_intervals"] if interval["end"] == PRIMARY
    ]
    boundary = _find_interval(primary_intervals, "ST-B1")
    violated = _find_interval(primary_intervals, "ST-B2")
    actions = report["actions"]
    backfill = next(item for item in actions if item["action_type"] == "BACKFILL")
    rebuild = next(item for item in actions if item["action_type"] == "REBUILD")

    expectations = [
        ("boundary_duration_equal_rpo",
         boundary["adjudication"]["duration_ms"] == boundary["adjudication"]["rpo_ms"]),
        ("boundary_records_equal_limit",
         boundary["record_count"] == boundary["adjudication"]["max_gap_records"]),
        ("equal_threshold_not_violated", boundary["adjudication"]["violated"] is False),
        ("one_more_record_violated", violated["adjudication"]["violated"] is True),
        ("time_excess_reported", violated["adjudication"]["time_excess_ms"] == 60_000),
        ("record_excess_reported", violated["adjudication"]["record_excess"] == 1),
        ("backfill_from_backup", backfill["target_seqs"] == [2, 3]
         and backfill["source_end"] == BACKUP),
        ("rebuild_when_both_missing", rebuild["target_seqs"] == [4, 5]
         and rebuild["source_end"] is None),
        ("action_references_gap",
         backfill["gap_id"] == violated["gap_id"]
         and rebuild["gap_id"] == violated["gap_id"]),
        ("priority_ordering",
         [item["action_id"] for item in actions] == sorted(
             item["action_id"] for item in actions)),
    ]
    return _sample_result(
        "sample_threshold_boundary", cross, expectations,
        {"boundary_gap": boundary["gap_id"], "violated_gap": violated["gap_id"]},
    )


def _check_sample_partial_backfill():
    project = build_sample_partial_backfill()
    report = build_report(project)
    cross = check_report(project, report)
    recovery_reference = {
        (item["event_id"], item["site_id"]): item
        for item in reference_recovery(project)
    }

    switchover = next(
        item for item in report["recovery_settlements"] if item["event_type"] == "switchover"
    )
    recovery = next(
        item for item in report["recovery_settlements"] if item["event_type"] == "recovery"
    )
    recovered_site = next(item for item in recovery["site_rows"] if item["site_id"] == "ST-C1")
    met_site = next(item for item in recovery["site_rows"] if item["site_id"] == "ST-C2")
    unmet_ids = [item["site_id"] for item in recovery["unmet_sites"]]

    expectations = [
        ("switchover_uses_backup", switchover["active_end"] == BACKUP),
        ("backup_rpo_met_at_switchover",
         all(row["rpo_met"] for row in switchover["site_rows"])),
        ("recovery_uses_primary", recovery["active_end"] == PRIMARY),
        ("partial_backfill_still_unmet", recovered_site["rpo_met"] is False),
        ("last_commit_trap_exposed",
         recovered_site["last_committed_seq"] == 10
         and recovered_site["missing_seqs_by_element"]["TEMP"] == [4, 5]),
        ("recovery_diff_reported",
         recovered_site["time_diff_ms"] == recovery_reference[("EVT-3", "ST-C1")]["time_diff_ms"]
         and recovered_site["record_diff"] == 0),
        ("fully_backfilled_site_met", met_site["rpo_met"] is True),
        ("unmet_site_list", unmet_ids == ["ST-C1"]),
    ]
    return _sample_result(
        "sample_partial_backfill", cross, expectations,
        {"recovery_event": "EVT-3", "unmet_site": "ST-C1"},
    )


def _find_interval(intervals, site_id):
    matches = [item for item in intervals if item["site_id"] == site_id]
    assert len(matches) == 1, f"{site_id} 缺口区间数量异常: {len(matches)}"
    return matches[0]


def _sample_result(name, cross_check, expectations, context):
    failed_expectations = [label for label, ok in expectations if not ok]
    passed = cross_check["ok"] and not failed_expectations
    detail = {
        "reference_cross_check": cross_check,
        "expectations": [
            {"name": label, "passed": ok} for label, ok in expectations
        ],
        "context": context,
    }
    if not passed and cross_check["ok"]:
        detail["failed_expectations"] = failed_expectations
    return {"name": name, "passed": passed, "detail": detail}

