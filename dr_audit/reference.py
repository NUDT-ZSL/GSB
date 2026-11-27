"""独立缺口区间参考计算器与恢复点核对器。

本模块刻意不导入 alignment / gaps / recovery，仅依据工程原始数据
（manifest、两端 landed、事件与等级策略）用独立写法重新计算，
再与主流程报表逐项比对，报告首个不一致站点要素及其对齐依据。
"""

PRIMARY = "primary"
BACKUP = "backup"


def _landed_set(project, end):
    return {
        (item["site_id"], item["element"], item["seq"])
        for item in project["ends"][end]["landed"]
    }


def reference_alignment(project):
    """返回逐站点要素的两端缺失序号集合（独立重算）。"""
    primary_set = _landed_set(project, PRIMARY)
    backup_set = _landed_set(project, BACKUP)
    result = {}
    for site in project["sites"]:
        for element in site["elements"]:
            key_prefix = (site["site_id"], element)
            p_seqs = {seq for (sid, ele, seq) in primary_set if (sid, ele) == key_prefix}
            b_seqs = {seq for (sid, ele, seq) in backup_set if (sid, ele) == key_prefix}
            result[key_prefix] = {
                "primary_missing_seqs": sorted(b_seqs - p_seqs),
                "backup_missing_seqs": sorted(p_seqs - b_seqs),
            }
    return result


def reference_gap_intervals(project):
    """独立归并缺口区间并独立裁决，返回可比对的区间元组列表。"""
    merge_gap_ms = project["generator"]["merge_gap_ms"]
    manifest_index = {
        (item["site_id"], item["element"], item["seq"]): item
        for item in project["manifest"]
    }
    intervals = []
    for end in (PRIMARY, BACKUP):
        landed = _landed_set(project, end)
        missing = [item for key, item in manifest_index.items() if key not in landed]
        by_site = {}
        for item in missing:
            by_site.setdefault(item["site_id"], []).append(item)
        for site in project["sites"]:
            records = sorted(
                by_site.get(site["site_id"], []),
                key=lambda item: (item["write_time"], item["element"], item["seq"]),
            )
            groups = _independent_grouping(records, merge_gap_ms)
            for group in groups:
                intervals.append(
                    _interval_tuple(site["level"], project["level_policy"], end, group)
                )
    return sorted(intervals, key=lambda item: (item["end"], item["site_id"], item["start_time"]))


def _independent_grouping(records, merge_gap_ms):
    groups = []
    group = []
    cursor = None
    for item in records:
        if cursor is None or item["write_time"] - cursor <= merge_gap_ms:
            group.append(item)
        else:
            groups.append(group)
            group = [item]
        cursor = item["write_time"]
    if group:
        groups.append(group)
    return groups


def _interval_tuple(level, level_policy, end, group):
    policy = level_policy[level]
    start = min(item["write_time"] for item in group)
    stop = max(item["write_time"] for item in group)
    per_element = {}
    for item in group:
        per_element.setdefault(item["element"], set()).add(item["seq"])
    duration = stop - start
    count = len(group)
    violated = duration > policy["rpo_ms"] or count > policy["max_gap_records"]
    return {
        "end": end,
        "site_id": group[0]["site_id"],
        "start_time": start,
        "end_time": stop,
        "duration_ms": duration,
        "record_count": count,
        "elements": tuple(sorted(per_element)),
        "per_element_missing_seqs": tuple(
            sorted((element, tuple(sorted(seqs))) for element, seqs in per_element.items())
        ),
        "violated": violated,
        "time_excess_ms": max(0, duration - policy["rpo_ms"]),
        "record_excess": max(0, count - policy["max_gap_records"]),
    }


def reference_recovery(project):
    """独立恢复点结算，返回逐事件逐站点的 RPO 账目。"""
    active = PRIMARY
    results = []
    for event in project["events"]:
        if event["type"] not in ("switchover", "recovery"):
            continue
        active = event.get("active_end_after", active)
        landed = project["ends"][active]["landed"]
        for site in project["sites"]:
            policy = project["level_policy"][site["level"]]
            element_missing = {}
            worst_lag_ms = 0
            total_lag_records = 0
            for element in site["elements"]:
                expected = [
                    item
                    for item in project["manifest"]
                    if item["site_id"] == site["site_id"]
                    and item["element"] == element
                    and item["write_time"] <= event["at"]
                ]
                present = {
                    item["seq"]
                    for item in landed
                    if item["site_id"] == site["site_id"]
                    and item["element"] == element
                    and item["write_time"] <= event["at"]
                }
                missing = sorted(item["seq"] for item in expected if item["seq"] not in present)
                element_missing[element] = missing
                total_lag_records += len(missing)
                if missing:
                    first_time = min(
                        item["write_time"] for item in expected if item["seq"] in set(missing)
                    )
                    worst_lag_ms = max(worst_lag_ms, event["at"] - first_time)
            rpo_met = (
                worst_lag_ms <= policy["rpo_ms"]
                and all(
                    len(seqs) <= policy["max_gap_records"]
                    for seqs in element_missing.values()
                )
            )
            results.append(
                {
                    "event_id": event["event_id"],
                    "site_id": site["site_id"],
                    "active_end": active,
                    "rpo_met": rpo_met,
                    "lag_ms": worst_lag_ms,
                    "time_diff_ms": max(0, worst_lag_ms - policy["rpo_ms"]),
                    "lag_records": total_lag_records,
                    "record_diff": sum(
                        max(0, len(seqs) - policy["max_gap_records"])
                        for seqs in element_missing.values()
                    ),
                    "missing_seqs_by_element": element_missing,
                }
            )
    return results


def check_report(project, report):
    """对主流程报表逐站点逐要素复核，返回首个不一致项。"""
    mismatch = _check_alignment(project, report)
    if mismatch is not None:
        return mismatch
    mismatch = _check_gap_integrity(report)
    if mismatch is not None:
        return mismatch
    mismatch = _check_gaps(project, report)
    if mismatch is not None:
        return mismatch
    mismatch = _check_recovery(project, report)
    if mismatch is not None:
        return mismatch
    return {"ok": True, "first_mismatch": None}


def _check_alignment(project, report):
    reference = reference_alignment(project)
    for row in report["alignment"]:
        key = (row["site_id"], row["element"])
        expected = reference[key]
        basis = {
            "align_key": ["site_id", "element", "seq"],
            "primary_present": row["primary_present_seqs"],
            "backup_present": row["backup_present_seqs"],
        }
        if row["primary_missing_seqs"] != expected["primary_missing_seqs"]:
            return _mismatch("alignment.primary_missing", key, basis, expected, row)
        if row["backup_missing_seqs"] != expected["backup_missing_seqs"]:
            return _mismatch("alignment.backup_missing", key, basis, expected, row)
    return None


def _check_gap_integrity(report):
    for interval in report["gap_intervals"]:
        actual_count = len(interval["records"])
        summed = sum(
            len(seqs) for seqs in interval["per_element_missing_seqs"].values()
        )
        key = (interval["site_id"], ",".join(interval["elements"]))
        if interval["record_count"] != actual_count or actual_count != summed:
            basis = {
                "gap_id": interval["gap_id"],
                "reported_record_count": interval["record_count"],
                "actual_record_count": actual_count,
                "per_element_sum": summed,
            }
            return _mismatch("gap.record_count", key, basis, actual_count, interval["record_count"])
    return None


def _check_gaps(project, report):
    reference = {(item["end"], item["site_id"], item["start_time"]): item for item in reference_gap_intervals(project)}
    for interval in report["gap_intervals"]:
        key = (interval["end"], interval["site_id"], interval["start_time"])
        expected = reference.get(key)
        basis = {
            "gap_id": interval["gap_id"],
            "align_key": ["end", "site_id", "start_time"],
            "per_element_missing_seqs": interval["per_element_missing_seqs"],
        }
        if expected is None:
            return _mismatch("gap.interval", key, basis, "reference interval missing", interval)
        actual = (
            interval["end_time"],
            interval["duration_ms"],
            interval["record_count"],
            tuple(interval["elements"]),
            tuple(
                sorted(
                    (element, tuple(seqs))
                    for element, seqs in interval["per_element_missing_seqs"].items()
                )
            ),
            interval["adjudication"]["violated"],
            interval["adjudication"]["time_excess_ms"],
            interval["adjudication"]["record_excess"],
        )
        expected_tuple = (
            expected["end_time"],
            expected["duration_ms"],
            expected["record_count"],
            expected["elements"],
            expected["per_element_missing_seqs"],
            expected["violated"],
            expected["time_excess_ms"],
            expected["record_excess"],
        )
        if actual != expected_tuple:
            return _mismatch("gap.interval", key, basis, expected_tuple, actual)
    return None


def _check_recovery(project, report):
    reference = {
        (item["event_id"], item["site_id"]): item for item in reference_recovery(project)
    }
    for settlement in report["recovery_settlements"]:
        for row in settlement["site_rows"]:
            key = (settlement["event_id"], row["site_id"])
            expected = reference[key]
            basis = {
                "event_id": settlement["event_id"],
                "active_end": row["active_end"],
                "missing_seqs_by_element": row["missing_seqs_by_element"],
                "last_committed_seq": row["last_committed_seq"],
            }
            actual = (
                row["rpo_met"],
                row["lag_ms"],
                row["time_diff_ms"],
                row["lag_records"],
                row["record_diff"],
            )
            expected_tuple = (
                expected["rpo_met"],
                expected["lag_ms"],
                expected["time_diff_ms"],
                expected["lag_records"],
                expected["record_diff"],
            )
            if actual != expected_tuple:
                return _mismatch("recovery.settlement", key, basis, expected_tuple, actual)
    return None


def _mismatch(stage, key, basis, expected, actual):
    site_id = key[0] if isinstance(key, tuple) else key
    element = key[1] if isinstance(key, tuple) and len(key) > 1 else None
    return {
        "ok": False,
        "first_mismatch": {
            "stage": stage,
            "site_id": site_id,
            "element": element,
            "alignment_basis": basis,
            "expected": expected,
            "actual": actual,
        },
    }

