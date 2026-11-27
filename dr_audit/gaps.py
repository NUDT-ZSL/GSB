"""缺失批次归并、超标裁决与补传 / 重建动作清单。"""

from .generator import BACKUP, PRIMARY
from .levels import MERGE_GAP_MS, level_rank


def build_landed_indexes(project):
    def index(records):
        result = {}
        for record in records:
            result[(record["site_id"], record["element"], record["seq"])] = record
        return result

    return {
        PRIMARY: index(project["ends"][PRIMARY]["landed"]),
        BACKUP: index(project["ends"][BACKUP]["landed"]),
    }


def build_gap_intervals(project):
    """按端、站点归并缺失批次为连续数据缺口区间。

    逻辑清单（manifest）是权威批次全集；落盘缺失（含两端同时缺失）
    均构成该端缺口。相邻缺失批次逻辑时刻差不超过 merge_gap_ms 时归并为
    同一区间，因此区间会自然跨越中断事件边界，不会被事件切断。
    """
    merge_gap_ms = project["generator"].get("merge_gap_ms", MERGE_GAP_MS)
    level_policy = project["level_policy"]
    landed_indexes = build_landed_indexes(project)

    missing_by_end = {PRIMARY: {}, BACKUP: {}}
    for record in project["manifest"]:
        site_id = record["site_id"]
        for end in (PRIMARY, BACKUP):
            key = (site_id, record["element"], record["seq"])
            if key not in landed_indexes[end]:
                missing_by_end[end].setdefault(site_id, []).append(record)

    intervals = []
    interval_counter = 0
    for end in (PRIMARY, BACKUP):
        for site in project["sites"]:
            site_id = site["site_id"]
            records = sorted(
                missing_by_end[end].get(site_id, []),
                key=lambda item: (item["write_time"], item["element"], item["seq"]),
            )
            for chunk in _merge_records(records, merge_gap_ms):
                interval_counter += 1
                intervals.append(
                    _build_interval(
                        interval_counter, end, site, chunk, level_policy, project["events"]
                    )
                )
    return intervals


def _merge_records(records, merge_gap_ms):
    if not records:
        return []
    chunks = []
    current = [records[0]]
    previous_time = records[0]["write_time"]
    for record in records[1:]:
        if record["write_time"] - previous_time <= merge_gap_ms:
            current.append(record)
        else:
            chunks.append(current)
            current = [record]
        previous_time = record["write_time"]
    chunks.append(current)
    return chunks


def _build_interval(counter, end, site, chunk, level_policy, events):
    site_id = site["site_id"]
    level = site["level"]
    start_time = min(item["write_time"] for item in chunk)
    end_time = max(item["write_time"] for item in chunk)
    elements = sorted({item["element"] for item in chunk})
    per_element = {}
    for item in chunk:
        per_element.setdefault(item["element"], []).append(item["seq"])
    per_element = {element: sorted(seqs) for element, seqs in sorted(per_element.items())}
    crossed_events = [
        event["event_id"]
        for event in events
        if start_time < event["at"] <= end_time
        or (event.get("until") is not None and start_time < event["until"] <= end_time)
    ]
    policy = level_policy[level]
    duration_ms = end_time - start_time
    record_count = len(chunk)
    adjudication = adjudicate(duration_ms, record_count, policy)
    return {
        "gap_id": f"GAP-{counter:04d}",
        "end": end,
        "site_id": site_id,
        "level": level,
        "start_time": start_time,
        "end_time": end_time,
        "duration_ms": duration_ms,
        "record_count": record_count,
        "elements": elements,
        "per_element_missing_seqs": per_element,
        "records": [
            {
                "site_id": item["site_id"],
                "element": item["element"],
                "seq": item["seq"],
                "write_time": item["write_time"],
            }
            for item in chunk
        ],
        "crossed_event_ids": crossed_events,
        "adjudication": adjudication,
    }


def adjudicate(duration_ms, record_count, policy):
    """超标裁决：阈值采用严格大于，恰好等于阈值判定为未超标。"""
    rpo_ms = policy["rpo_ms"]
    max_gap_records = policy["max_gap_records"]
    time_excess = max(0, duration_ms - rpo_ms)
    record_excess = max(0, record_count - max_gap_records)
    violated = duration_ms > rpo_ms or record_count > max_gap_records
    return {
        "violated": violated,
        "rpo_ms": rpo_ms,
        "rpo_unit": "ms",
        "max_gap_records": max_gap_records,
        "record_unit": "batch",
        "duration_ms": duration_ms,
        "record_count": record_count,
        "time_excess_ms": time_excess,
        "record_excess": record_excess,
        "duration_within_limit": duration_ms <= rpo_ms,
        "records_within_limit": record_count <= max_gap_records,
        "basis": [
            {
                "metric": "gap_duration_ms",
                "observed": duration_ms,
                "threshold": rpo_ms,
                "unit": "ms",
                "violated": duration_ms > rpo_ms,
                "excess": time_excess,
            },
            {
                "metric": "gap_record_count",
                "observed": record_count,
                "threshold": max_gap_records,
                "unit": "batch",
                "violated": record_count > max_gap_records,
                "excess": record_excess,
            },
        ],
    }


def build_actions(intervals, project):
    """为超标缺口生成补传 / 重建动作并排序。"""
    landed_indexes = build_landed_indexes(project)
    level_policy = project["level_policy"]
    actions = []
    action_counter = 0
    for interval in intervals:
        if not interval["adjudication"]["violated"]:
            continue
        peer = BACKUP if interval["end"] == PRIMARY else PRIMARY
        for element, seqs in sorted(interval["per_element_missing_seqs"].items()):
            backfill_seqs = []
            rebuild_seqs = []
            for seq in seqs:
                key = (interval["site_id"], element, seq)
                if key in landed_indexes[peer]:
                    backfill_seqs.append(seq)
                else:
                    rebuild_seqs.append(seq)
            for action_type, target_seqs in (
                ("BACKFILL", backfill_seqs),
                ("REBUILD", rebuild_seqs),
            ):
                if not target_seqs:
                    continue
                action_counter += 1
                priority = _priority(interval, level_policy)
                actions.append(
                    {
                        "action_id": f"ACT-{action_counter:04d}",
                        "action_type": action_type,
                        "priority": priority,
                        "site_id": interval["site_id"],
                        "level": interval["level"],
                        "element": element,
                        "target_end": interval["end"],
                        "source_end": peer if action_type == "BACKFILL" else None,
                        "target_seqs": target_seqs,
                        "record_count": len(target_seqs),
                        "gap_id": interval["gap_id"],
                        "gap_start_time": interval["start_time"],
                        "gap_duration_ms": interval["duration_ms"],
                        "trigger_basis": {
                            "gap_id": interval["gap_id"],
                            "rpo_ms": interval["adjudication"]["rpo_ms"],
                            "max_gap_records": interval["adjudication"]["max_gap_records"],
                            "time_excess_ms": interval["adjudication"]["time_excess_ms"],
                            "record_excess": interval["adjudication"]["record_excess"],
                        },
                    }
                )
    actions.sort(
        key=lambda item: (
            item["priority"],
            item["gap_start_time"],
            item["site_id"],
            item["element"],
            item["action_type"],
        )
    )
    for index, action in enumerate(actions, start=1):
        action["action_id"] = f"ACT-{index:04d}"
    return actions


def _priority(interval, level_policy):
    rank = level_rank(level_policy, interval["level"])
    rpo_ms = interval["adjudication"]["rpo_ms"]
    if interval["duration_ms"] > 2 * rpo_ms:
        rank = max(1, rank - 1)
    return f"P{rank}"

