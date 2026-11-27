"""主备两端按站点 / 要素 / 批次序号对齐。"""

from .generator import BACKUP, PRIMARY


def index_landed(landed_records):
    """以 (site_id, element, seq) 为键索引已落盘批次。"""
    index = {}
    for record in landed_records:
        key = (record["site_id"], record["element"], record["seq"])
        if key in index:
            raise ValueError(f"已落盘批次出现重复键: {key}")
        index[key] = record
    return index


def align_project(project):
    """对齐主备落盘清单，输出逐站点逐要素的两类缺失集合。

    对齐键固定为站点编号 + 要素 + 批次序号；同一批次两端写入时刻
    不一致时不影响对齐，只在写入时刻差异字段中如实记录。
    """
    primary_index = index_landed(project["ends"][PRIMARY]["landed"])
    backup_index = index_landed(project["ends"][BACKUP]["landed"])
    all_keys = sorted(set(primary_index) | set(backup_index), key=_sort_key)

    rows = {}
    for key in all_keys:
        site_id, element, seq = key
        primary_record = primary_index.get(key)
        backup_record = backup_index.get(key)
        primary_only = primary_record is not None and backup_record is None
        backup_only = backup_record is not None and primary_record is None
        if not (primary_only or backup_only):
            continue
        row = {
            "site_id": site_id,
            "element": element,
            "seq": seq,
            "primary_present": primary_record is not None,
            "backup_present": backup_record is not None,
            "primary_write_time": _write_time(primary_record),
            "backup_write_time": _write_time(backup_record),
            "write_time_diff_ms": _write_time_diff(primary_record, backup_record),
            "missing_on": BACKUP if primary_only else PRIMARY,
            "direction": "primary_missing" if backup_only else "backup_missing",
        }
        rows.setdefault((site_id, element), []).append(row)

    aligned = []
    for site in project["sites"]:
        site_id = site["site_id"]
        for element in site["elements"]:
            rows_for_key = sorted(rows.get((site_id, element), []), key=lambda item: item["seq"])
            primary_missing = [item["seq"] for item in rows_for_key if item["direction"] == "primary_missing"]
            backup_missing = [item["seq"] for item in rows_for_key if item["direction"] == "backup_missing"]
            present_keys = {
                key
                for key in all_keys
                if key[0] == site_id and key[1] == element
            }
            present_seqs = sorted(key[2] for key in present_keys)
            aligned.append(
                {
                    "site_id": site_id,
                    "element": element,
                    "level": site["level"],
                    "present_seqs": present_seqs,
                    "primary_present_seqs": sorted(
                        key[2]
                        for key in primary_index
                        if key[0] == site_id and key[1] == element
                    ),
                    "backup_present_seqs": sorted(
                        key[2]
                        for key in backup_index
                        if key[0] == site_id and key[1] == element
                    ),
                    "primary_missing_seqs": primary_missing,
                    "backup_missing_seqs": backup_missing,
                    "detail_rows": rows_for_key,
                }
            )
    return aligned


def _sort_key(key):
    site_id, element, seq = key
    return site_id, element, seq


def _write_time(record):
    return None if record is None else record["write_time"]


def _write_time_diff(primary_record, backup_record):
    if primary_record is None or backup_record is None:
        return None
    return primary_record["write_time"] - backup_record["write_time"]

