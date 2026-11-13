"""修复前后差异分析模块。

对同一语料的两轮执行结果（修复前/修复后）输出三类结论：
    new_crashes         新增崩溃（修复前非崩溃 -> 修复后崩溃）
    disappeared_crashes 消失崩溃（修复前崩溃 -> 修复后非崩溃）
    behavior_flips      行为翻转（其余结局不一致：业务码变化或崩溃类型变化）
全部按用例ID升序，计数与明细可复算，顺序确定。
"""
from __future__ import annotations

CRASH_CATS = ("length_oob", "parse_timeout", "unrecoverable_abort")


def _is_crash(rec: dict) -> bool:
    return rec["category"] in CRASH_CATS


def _outcome(rec: dict) -> str:
    if _is_crash(rec):
        return f"crash:{rec['category']}"
    return f"code:{rec['code']}"


def diff(before_results: list[dict], after_results: list[dict]) -> dict:
    b = {r["id"]: r for r in before_results}
    a = {r["id"]: r for r in after_results}
    new_crashes, disappeared, flips = [], [], []
    for cid in sorted(set(b) | set(a)):
        rb, ra = b.get(cid), a.get(cid)
        if rb is None or ra is None:
            # 一侧缺失视为行为翻转，保证可复算
            flips.append({
                "id": cid,
                "before": _outcome(rb) if rb else "missing",
                "after": _outcome(ra) if ra else "missing",
            })
            continue
        cb, ca = _is_crash(rb), _is_crash(ra)
        if not cb and ca:
            new_crashes.append({"id": cid, "before": _outcome(rb), "after": _outcome(ra)})
        elif cb and not ca:
            disappeared.append({"id": cid, "before": _outcome(rb), "after": _outcome(ra)})
        elif _outcome(rb) != _outcome(ra):
            flips.append({"id": cid, "before": _outcome(rb), "after": _outcome(ra)})
    return {
        "new_crashes": new_crashes,
        "disappeared_crashes": disappeared,
        "behavior_flips": flips,
        "counts": {
            "new_crashes": len(new_crashes),
            "disappeared_crashes": len(disappeared),
            "behavior_flips": len(flips),
        },
    }
