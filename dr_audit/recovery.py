"""灾备切换与中断恢复事件后的恢复点结算。"""

from .generator import BACKUP, PRIMARY

SETTLED_EVENT_TYPES = ("switchover", "recovery")


def _active_end_after(event, previous):
    if event["type"] == "switchover":
        return event.get("active_end_after", BACKUP)
    if event["type"] == "recovery":
        return event.get("active_end_after", previous)
    return previous


def settle_recovery_points(project):
    """以事件之后各站点最后提交位点核对 RPO 达成情况。

    每次切换 / 恢复事件确定事件后的活跃端，以该端在事件时刻之前
    已落盘批次计算逐要素位点；恢复点差值同时给出记录缺口与时间缺口，
    避免只比较最后提交位点而漏掉中间批次缺失。
    """
    level_policy = project["level_policy"]
    landed = {
        PRIMARY: project["ends"][PRIMARY]["landed"],
        BACKUP: project["ends"][BACKUP]["landed"],
    }

    events = project["events"]
    settlements = []
    active_end = PRIMARY
    for event in events:
        if event["type"] not in SETTLED_EVENT_TYPES:
            continue
        active_end = _active_end_after(event, active_end)
        element_rows = []
        site_rows = []
        unmet_sites = []
        for site in project["sites"]:
            site_id = site["site_id"]
            policy = level_policy[site["level"]]
            element_results = [
                _settle_element(site_id, element, active_end, event["at"], landed, project["manifest"])
                for element in site["elements"]
            ]
            element_rows.extend(element_results)

            worst = _worst_element(element_results)
            rpo_met = (
                worst["lag_ms"] <= policy["rpo_ms"]
                and all(
                    row["lag_records"] <= policy["max_gap_records"]
                    for row in element_results
                )
            )
            site_row = {
                "site_id": site_id,
                "level": site["level"],
                "active_end": active_end,
                "rpo_ms": policy["rpo_ms"],
                "rpo_unit": "ms",
                "max_gap_records": policy["max_gap_records"],
                "record_unit": "batch",
                "rpo_met": rpo_met,
                "lag_ms": worst["lag_ms"],
                "lag_records": sum(row["lag_records"] for row in element_results),
                "time_diff_ms": max(0, worst["lag_ms"] - policy["rpo_ms"]),
                "record_diff": sum(
                    max(0, row["lag_records"] - policy["max_gap_records"])
                    for row in element_results
                ),
                "last_commit_time": max(
                    (row["last_commit_time"] for row in element_results), default=0
                ),
                "last_committed_seq": max(
                    (row["last_committed_seq"] for row in element_results), default=0
                ),
                "expected_seq": max(row["expected_seq"] for row in element_results),
                "missing_seqs_by_element": {
                    row["element"]: row["missing_seqs"] for row in element_results
                },
            }
            site_rows.append(site_row)
            if not rpo_met:
                unmet_sites.append(
                    {
                        "site_id": site_id,
                        "level": site["level"],
                        "active_end": active_end,
                        "rpo_ms": policy["rpo_ms"],
                        "lag_ms": site_row["lag_ms"],
                        "time_diff_ms": site_row["time_diff_ms"],
                        "lag_records": site_row["lag_records"],
                        "max_gap_records": policy["max_gap_records"],
                        "record_diff": site_row["record_diff"],
                        "missing_seqs_by_element": site_row["missing_seqs_by_element"],
                    }
                )

        settlements.append(
            {
                "event_id": event["event_id"],
                "event_type": event["type"],
                "event_time": event["at"],
                "active_end": active_end,
                "element_rows": element_rows,
                "site_rows": site_rows,
                "unmet_sites": unmet_sites,
            }
        )
    return settlements


def _settle_element(site_id, element, active_end, event_time, landed, manifest):
    records = [
        record
        for record in landed[active_end]
        if record["site_id"] == site_id
        and record["element"] == element
        and record["write_time"] <= event_time
    ]
    present_seqs = {record["seq"] for record in records}
    expected_records = [
        record
        for record in manifest
        if record["site_id"] == site_id
        and record["element"] == element
        and record["write_time"] <= event_time
    ]
    expected_seqs = {record["seq"] for record in expected_records}
    expected_seq = max(expected_seqs, default=0)
    missing_seqs = sorted(expected_seqs - present_seqs)

    if records:
        last_record = max(records, key=lambda item: item["seq"])
        last_committed_seq = last_record["seq"]
        last_commit_time = last_record["write_time"]
    else:
        last_committed_seq = 0
        last_commit_time = 0

    if missing_seqs:
        first_missing = min(
            record["write_time"]
            for record in expected_records
            if record["seq"] in set(missing_seqs)
        )
        lag_ms = event_time - first_missing
    else:
        lag_ms = 0
    lag_records = len(missing_seqs)
    return {
        "site_id": site_id,
        "element": element,
        "active_end": active_end,
        "event_time": event_time,
        "expected_seq": expected_seq,
        "last_committed_seq": last_committed_seq,
        "last_commit_time": last_commit_time,
        "present_seqs": sorted(present_seqs),
        "missing_seqs": missing_seqs,
        "lag_ms": lag_ms,
        "lag_records": lag_records,
    }


def _worst_element(element_results):
    return max(element_results, key=lambda row: (row["lag_ms"], row["lag_records"]))

