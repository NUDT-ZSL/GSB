"""跨模块一致性自检：任一不一致返回 E_INCONSISTENT 并指出首个偏差。"""

from .errors import E_INCONSISTENT, MemError
from .migrate import apply_completion_records
from .model import canonical_bytes, projection_bytes, v1_projection, v2_projection


def _fail(check, detail):
    raise MemError(E_INCONSISTENT, f"自检失败[{check}]: {detail}")


def _by_session(entries):
    grouped = {}
    for e in entries:
        grouped.setdefault(e.session_id, []).append(e)
    for group in grouped.values():
        group.sort(key=lambda e: (e.turn_start, e.turn_end))
    return grouped


def check_three_format_views(entries_from_v1, entries_from_v2, entries_from_v3):
    """三格式视图下同一会话的记忆条目集合一致（按各格式可表达投影）。"""
    views = [
        ("v1->v3", _by_session(entries_from_v1)),
        ("v2->v3", _by_session(entries_from_v2)),
        ("v3", _by_session(entries_from_v3)),
    ]
    sessions = sorted(views[0][1])
    for name, grouped in views[1:]:
        if sorted(grouped) != sessions:
            _fail("three_format_views", f"会话集合不一致: {name}={sorted(grouped)} vs {sessions}")
    for session_id in sessions:
        base = projection_bytes(views[0][1][session_id], v1_projection)
        for name, grouped in views[1:]:
            other = projection_bytes(grouped[session_id], v1_projection)
            if base != other:
                _fail(
                    "three_format_views",
                    f"会话 {session_id} 的 v1 投影在 {name} 视图下首个偏差",
                )
        v2_base = projection_bytes(views[1][1][session_id], v2_projection)
        v2_other = projection_bytes(views[2][1][session_id], v2_projection)
        if v2_base != v2_other:
            _fail(
                "three_format_views",
                f"会话 {session_id} 的 v2 投影在 v3 视图下首个偏差",
            )
    return True


def check_migration_replay(v1_entries, completion_records, migrated_entries):
    """迁移补全记录可重放出同一内部表示（逐字节一致）。"""
    replayed = apply_completion_records(v1_entries, completion_records)
    if len(replayed) != len(migrated_entries):
        _fail("migration_replay", "重放条目数与迁移结果不一致")
    for idx, (a, b) in enumerate(zip(replayed, migrated_entries)):
        if canonical_bytes(a) != canonical_bytes(b):
            _fail(
                "migration_replay",
                f"第 {idx} 条（{b.key()}）重放结果与迁移结果首个字节偏差",
            )
    return True


def check_invalidation_report(report, all_window_ids, all_entry_ids):
    """重算集合与复用集合无交集，且并集覆盖全部窗口/条目（含受影响集合）。"""
    for kind, universe in (("windows", all_window_ids), ("entries", all_entry_ids)):
        recompute = set(report[f"recompute_{kind}"])
        reuse = set(report[f"reuse_{kind}"])
        inter = recompute & reuse
        if inter:
            _fail("invalidation", f"{kind} 重算与复用集合存在交集: {sorted(inter)[:3]}")
        union = recompute | reuse
        if union != set(universe):
            _fail(
                "invalidation",
                f"{kind} 重算与复用并集未覆盖全集: 缺失 {sorted(set(universe) - union)[:3]}",
            )
        affected = set(report[f"affected_{kind}"])
        if not affected <= recompute:
            _fail(
                "invalidation",
                f"{kind} 受影响集合未被重算集合覆盖: {sorted(affected - recompute)[:3]}",
            )
    return True
