"""逐日逐工种资源需求聚合与超限标记。"""
from datetime import datetime, timezone

DAY_MS = 86_400_000


def day_label(day_index):
    return datetime.fromtimestamp(day_index * 86400,
                                  tz=timezone.utc).date().isoformat()


def daily_curve(project):
    """返回 {(日期, 工种): {demand, cap, over, activities}}。

    需求恰等于上限不标记超限；超限条目附造成超限的工序清单。
    """
    buckets = {}
    for a in project.activities.values():
        if a.ef_ms <= a.es_ms:
            continue
        first = a.es_ms // DAY_MS
        last = (a.ef_ms - 1) // DAY_MS
        for d in range(first, last + 1):
            for trade, qty in a.resources.items():
                slot = buckets.setdefault(d, {}).setdefault(
                    trade, {"demand": 0.0, "activities": set()})
                slot["demand"] += qty
                slot["activities"].add(a.activity_id)
    curve = {}
    for d, trades in buckets.items():
        label = day_label(d)
        for trade, slot in trades.items():
            cap = project.caps.get(trade)
            over = cap is not None and slot["demand"] > cap
            curve[(label, trade)] = {
                "demand": slot["demand"],
                "cap": cap,
                "over": over,
                "activities": sorted(slot["activities"]),
            }
    return curve
