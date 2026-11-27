"""站点保留等级策略与全局节拍配置。"""

CADENCE_MS = 60_000
MERGE_GAP_MS = 2 * CADENCE_MS
BASE_TIME_MS = 1_700_000_000_000

ELEMENTS = ("TEMP", "HUM", "WIND", "PRES", "RAIN", "RBR")

LEVEL_POLICY = {
    "L1": {
        "level": "L1",
        "name": "核心预警站",
        "rpo_ms": 300_000,
        "max_gap_records": 3,
        "elements": ("TEMP", "HUM", "WIND", "RBR"),
    },
    "L2": {
        "level": "L2",
        "name": "重要观测站",
        "rpo_ms": 600_000,
        "max_gap_records": 6,
        "elements": ("TEMP", "HUM", "WIND"),
    },
    "L3": {
        "level": "L3",
        "name": "一般区域站",
        "rpo_ms": 1_200_000,
        "max_gap_records": 12,
        "elements": ("TEMP", "RAIN"),
    },
    "L4": {
        "level": "L4",
        "name": "普通观测站",
        "rpo_ms": 1_800_000,
        "max_gap_records": 18,
        "elements": ("TEMP",),
    },
}

LEVEL_ORDER = ("L1", "L2", "L3", "L4")

# 生成器站点等级循环，保证不同种子下等级分布稳定且包含全部等级。
SITE_LEVEL_CYCLE = ("L1", "L2", "L3", "L1", "L2", "L4", "L3", "L2")


def policy_for(level_policy, level):
    if level not in level_policy:
        raise KeyError(f"未知站点保留等级: {level}")
    return level_policy[level]


def level_rank(level_policy, level):
    ordered = _ordered_levels(level_policy)
    return ordered.index(level) + 1


def _ordered_levels(level_policy):
    custom = [lvl for lvl in LEVEL_ORDER if lvl in level_policy]
    extra = sorted(lvl for lvl in level_policy if lvl not in LEVEL_ORDER)
    return tuple(custom + extra)

