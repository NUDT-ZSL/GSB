from recovery_engine.migrate import ROUNDING, SCALE_MS_PER_SECOND, migrate_plan
from recovery_engine.merge import merge
from recovery_engine.v1_loader import load_v1
from recovery_engine.v2_loader import load_v2
from tests.conftest import V1_PATH, V2_DIR, V2_META


def fresh_plan():
    return merge(load_v1(V1_PATH), load_v2(V2_DIR, V2_META))


def test_v1_relative_seconds_migrated_to_absolute(store):
    plan = migrate_plan(fresh_plan(), 10000, store)
    # web: 10000 + 30 * 1000 = 40000
    assert plan.services["web"].fallback_abs_ms == 40000
    assert plan.services["web"].time_source == "migrated-v1"
    # legacy: 10000 + 0 = 10000
    assert plan.services["legacy"].fallback_abs_ms == 10000


def test_v2_absolute_kept(store):
    plan = migrate_plan(fresh_plan(), 10000, store)
    assert plan.services["api"].fallback_abs_ms == 9000
    assert plan.services["api"].time_source == "absolute-v2"


def test_migration_decision_persisted_with_scale_and_rounding(store):
    migrate_plan(fresh_plan(), 10000, store)
    row = store.get_migration("web", 10000)
    assert row["fallback_seconds"] == 30
    assert row["fallback_abs_ms"] == 40000
    assert row["scale"] == SCALE_MS_PER_SECOND
    assert row["rounding"] == ROUNDING
    assert row["time_source"] == "migrated-v1"


def test_repeated_migration_byte_identical(store):
    first = migrate_plan(fresh_plan(), 10000, store).to_stable_json()
    second = migrate_plan(fresh_plan(), 10000, store).to_stable_json()
    assert first == second


def test_no_double_conversion(store):
    """回切窗口比较不被重复换算：同一时钟下重复迁移结果不变。"""
    migrate_plan(fresh_plan(), 10000, store)
    plan = migrate_plan(fresh_plan(), 10000, store)
    assert plan.services["web"].fallback_abs_ms == 40000  # 而非 40000+30000
