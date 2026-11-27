"""人工构造样本：覆盖中间缺失、阈值边界与切换后部分补传路径。"""

from .generator import BACKUP, PRIMARY
from .levels import CADENCE_MS

SAMPLE_POLICY = {
    "L1": {
        "level": "L1",
        "name": "核心预警站",
        "rpo_ms": 300_000,
        "max_gap_records": 3,
        "elements": ("TEMP", "HUM"),
    },
    "L2": {
        "level": "L2",
        "name": "重要观测站",
        "rpo_ms": 600_000,
        "max_gap_records": 6,
        "elements": ("TEMP",),
    },
    "LT": {
        "level": "LT",
        "name": "阈值边界样本站",
        "rpo_ms": 120_000,
        "max_gap_records": 3,
        "elements": ("TEMP",),
    },
}


def _wrap(sample_id, sites, manifest, primary_landed, backup_landed, events):
    return {
        "schema_version": "1.0",
        "generator": {
            "seed": f"sample-{sample_id}",
            "n_sites": len(sites),
            "batches_per_element": 10,
            "cadence_ms": CADENCE_MS,
            "merge_gap_ms": 2 * CADENCE_MS,
            "base_time_ms": 1_000_000,
        },
        "level_policy": SAMPLE_POLICY,
        "sites": sites,
        "events": events,
        "manifest": manifest,
        "ends": {
            PRIMARY: {"end": PRIMARY, "landed": primary_landed, "last_commit": {}},
            BACKUP: {"end": BACKUP, "landed": backup_landed, "last_commit": {}},
        },
    }


def build_sample_same_commit_middle_missing():
    """主备最后提交位点同为 seq=6，但主端中间缺失 seq 3/4/5。"""
    base = 1_000_000
    sites = [
        {"site_id": "ST-A1", "level": "L1", "elements": ["TEMP"]},
        {"site_id": "ST-A2", "level": "L2", "elements": ["TEMP"]},
    ]
    manifest, primary_landed, backup_landed = [], [], []
    # ST-A1：备端有 seq1..7，主端缺 3/4/5/6（跨中断窗），最后位点都为 7。
    # 仅看最后提交位点两边一致，但主端中间存在 4 个缺失批次。
    for seq in range(1, 8):
        ref_time = base + (seq - 1) * CADENCE_MS
        manifest.append(
            {"site_id": "ST-A1", "element": "TEMP", "seq": seq,
             "write_time": ref_time, "level": "L1"}
        )
        if seq not in (3, 4, 5, 6):
            primary_landed.append(
                {"site_id": "ST-A1", "element": "TEMP", "seq": seq,
                 "write_time": ref_time, "level": "L1"}
            )
        backup_landed.append(
            {"site_id": "ST-A1", "element": "TEMP", "seq": seq,
             "write_time": ref_time + 2_000, "level": "L1"}
        )
    # ST-A2：主备都缺 seq 4（两端同时缺失，需要重建而非补传），最后位点同为 6。
    for seq in range(1, 7):
        manifest.append(
            {"site_id": "ST-A2", "element": "TEMP", "seq": seq,
             "write_time": base + (seq - 1) * CADENCE_MS, "level": "L2"}
        )
        if seq != 4:
            for landed, offset in ((primary_landed, 0), (backup_landed, 1_000)):
                landed.append(
                    {"site_id": "ST-A2", "element": "TEMP", "seq": seq,
                     "write_time": base + (seq - 1) * CADENCE_MS + offset, "level": "L2"}
                )
    events = [
        {"event_id": "EVT-1", "type": "interrupt", "side": PRIMARY,
         "at": base + 2 * CADENCE_MS, "until": base + 5 * CADENCE_MS},
        {"event_id": "EVT-2", "type": "switchover",
         "at": base + 5 * CADENCE_MS, "active_end_after": BACKUP},
    ]
    return _wrap("A", sites, manifest, primary_landed, backup_landed, events)


def build_sample_threshold_boundary():
    """缺口时长 / 记录数恰好等于阈值时不得判超标，超过一档即超标。"""
    base = 2_000_000
    sites = [
        {"site_id": "ST-B1", "level": "LT", "elements": ["TEMP"]},
        {"site_id": "ST-B2", "level": "LT", "elements": ["TEMP"]},
    ]
    manifest, primary_landed, backup_landed = [], [], []
    # ST-B1：主端缺 seq 2/3/4，三批、跨 3 个节拍 = 180000ms，恰等于 LT 阈值。
    equal_missing = {2, 3, 4}
    for seq in range(1, 8):
        manifest.append(
            {"site_id": "ST-B1", "element": "TEMP", "seq": seq,
             "write_time": base + (seq - 1) * CADENCE_MS, "level": "LT"}
        )
        if seq not in equal_missing:
            primary_landed.append(
                {"site_id": "ST-B1", "element": "TEMP", "seq": seq,
                 "write_time": base + (seq - 1) * CADENCE_MS, "level": "LT"}
            )
        backup_landed.append(
            {"site_id": "ST-B1", "element": "TEMP", "seq": seq,
             "write_time": base + (seq - 1) * CADENCE_MS + 500, "level": "LT"}
        )
    # ST-B2：主端缺 seq 2/3/4/5，四批，记录数超 3；时长 240000ms 也超 180000ms。
    # 其中 seq2/3 备端可补传，seq4/5 两端均缺失需重建。
    over_missing = {2, 3, 4, 5}
    backup_missing = {4, 5}
    for seq in range(1, 8):
        manifest.append(
            {"site_id": "ST-B2", "element": "TEMP", "seq": seq,
             "write_time": base + (seq - 1) * CADENCE_MS, "level": "LT"}
        )
        if seq not in over_missing:
            primary_landed.append(
                {"site_id": "ST-B2", "element": "TEMP", "seq": seq,
                 "write_time": base + (seq - 1) * CADENCE_MS, "level": "LT"}
            )
        if seq not in backup_missing:
            backup_landed.append(
                {"site_id": "ST-B2", "element": "TEMP", "seq": seq,
                 "write_time": base + (seq - 1) * CADENCE_MS + 500, "level": "LT"}
            )
    events = [
        {"event_id": "EVT-1", "type": "interrupt", "side": PRIMARY,
         "at": base + CADENCE_MS, "until": base + 5 * CADENCE_MS},
        {"event_id": "EVT-2", "type": "switchover",
         "at": base + 5 * CADENCE_MS, "active_end_after": BACKUP},
    ]
    return _wrap("B", sites, manifest, primary_landed, backup_landed, events)


def build_sample_partial_backfill():
    """切换到备端后恢复主端，主端仅部分补传，结算 RPO 达标与未达标两站。"""
    base = 3_000_000
    sites = [
        {"site_id": "ST-C1", "level": "L1", "elements": ["TEMP"]},
        {"site_id": "ST-C2", "level": "L1", "elements": ["TEMP"]},
    ]
    manifest, primary_landed, backup_landed = [], [], []
    # 中断窗 T+3c .. T+6c，切换在 T+6c，恢复在 T+9c。
    # ST-C1：主端中断窗缺 seq4/5/6/7，恢复后仅补回 seq6/7，仍缺 4/5。
    primary_missing_c1 = {4, 5}
    for seq in range(1, 11):
        manifest.append(
            {"site_id": "ST-C1", "element": "TEMP", "seq": seq,
             "write_time": base + (seq - 1) * CADENCE_MS, "level": "L1"}
        )
        if seq not in primary_missing_c1:
            primary_landed.append(
                {"site_id": "ST-C1", "element": "TEMP", "seq": seq,
                 "write_time": base + (seq - 1) * CADENCE_MS, "level": "L1"}
            )
        backup_landed.append(
            {"site_id": "ST-C1", "element": "TEMP", "seq": seq,
             "write_time": base + (seq - 1) * CADENCE_MS + 1_000, "level": "L1"}
        )
    # ST-C2：主端中断窗缺 seq4/5，恢复后全部补回，RPO 达标。
    primary_missing_c2 = set()
    for seq in range(1, 11):
        manifest.append(
            {"site_id": "ST-C2", "element": "TEMP", "seq": seq,
             "write_time": base + (seq - 1) * CADENCE_MS, "level": "L1"}
        )
        if seq not in primary_missing_c2:
            primary_landed.append(
                {"site_id": "ST-C2", "element": "TEMP", "seq": seq,
                 "write_time": base + (seq - 1) * CADENCE_MS, "level": "L1"}
            )
        backup_landed.append(
            {"site_id": "ST-C2", "element": "TEMP", "seq": seq,
             "write_time": base + (seq - 1) * CADENCE_MS + 1_000, "level": "L1"}
        )
    events = [
        {"event_id": "EVT-1", "type": "interrupt", "side": PRIMARY,
         "at": base + 3 * CADENCE_MS, "until": base + 6 * CADENCE_MS},
        {"event_id": "EVT-2", "type": "switchover",
         "at": base + 6 * CADENCE_MS, "active_end_after": BACKUP},
        {"event_id": "EVT-3", "type": "recovery", "side": PRIMARY,
         "at": base + 9 * CADENCE_MS, "active_end_after": PRIMARY},
    ]
    return _wrap("C", sites, manifest, primary_landed, backup_landed, events)


def all_samples():
    return [
        build_sample_same_commit_middle_missing(),
        build_sample_threshold_boundary(),
        build_sample_partial_backfill(),
    ]

