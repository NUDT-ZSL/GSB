"""时间表示的语义迁移。

v1 相对回退秒数在注入逻辑时钟下换算为绝对回退时刻：
    fallback_abs_ms = clock_ms + fallback_seconds * SCALE
记录换算标度 SCALE 与舍入方向 ROUNDING；v2 绝对时刻保持原值。
迁移决策持久化到 SQLite，同一逻辑时钟下重复判定直接复用持久化结果，
保证逐位一致；计划中显式标记 time_source，回切窗口比较不被重复换算。
"""

from .errors import BizError, ErrCode

SCALE_MS_PER_SECOND = 1000
ROUNDING = "exact"  # 整数乘法无舍入；标度与方向仍显式记录


def migrate_plan(plan, clock_ms, store):
    """对计划内所有服务执行时间迁移，并把迁移决策事务化持久化。

    幂等：同一 (service, clock_ms) 已有持久化决策时直接复用，
    不重复换算、不重复写入。
    """
    if not isinstance(clock_ms, int):
        raise BizError(ErrCode.E_BAD_CONFIG, "注入逻辑时钟必须为整数毫秒", got=clock_ms)
    with store.transaction():
        for name in sorted(plan.services):
            svc = plan.services[name]
            persisted = store.get_migration(name, clock_ms)
            if persisted is not None:
                svc.fallback_abs_ms = persisted["fallback_abs_ms"]
                svc.time_source = persisted["time_source"]
                continue
            if svc.source_version == "v1":
                svc.fallback_abs_ms = clock_ms + svc.fallback_seconds * SCALE_MS_PER_SECOND
                svc.time_source = "migrated-v1"
            else:
                svc.fallback_abs_ms = svc.effective_ms
                svc.time_source = "absolute-v2"
            store.put_migration(
                service=name,
                clock_ms=clock_ms,
                fallback_seconds=svc.fallback_seconds,
                fallback_abs_ms=svc.fallback_abs_ms,
                scale=SCALE_MS_PER_SECOND,
                rounding=ROUNDING,
                time_source=svc.time_source,
            )
    return plan
