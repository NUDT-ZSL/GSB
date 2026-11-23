import pytest

from recovery_engine.clock import LogicalClock
from recovery_engine.engine import Engine
from recovery_engine.errors import BizError, ErrCode
from recovery_engine.executor import SimulatedExecutor, SimulatedHealth
from tests.conftest import make_clock


def test_replay_determinism(store, plan, tmp_path):
    """同一逻辑时钟序列下重放得到逐位一致的裁决序列。"""
    from recovery_engine.store import Store
    from recovery_engine.engine import Engine as Eng
    from tests.conftest import V1_PATH, V2_DIR, V2_META

    snapshots = []
    for _ in range(2):
        s = Store(str(tmp_path / "replay.db") + str(len(snapshots)))
        eng = Eng(s, make_clock(), SimulatedExecutor(), SimulatedHealth())
        p = eng.build_plan(V1_PATH, V2_DIR, V2_META)
        eng.failover(p)
        eng.failback(p)
        snapshots.append((s.all_conclusions(), s.committed_actions(),
                          s.get_clock_waterline()))
    assert snapshots[0] == snapshots[1]


def test_clock_regress_raises_and_state_unchanged():
    clock = LogicalClock([1000, 2000, 1500], tolerance_ms=100)
    assert clock.now() == 1000
    clock.advance()
    assert clock.now() == 2000
    with pytest.raises(BizError) as exc:
        clock.advance()
    assert exc.value.code == ErrCode.E_CLOCK_REGRESS
    assert exc.value.details["last"] == 2000
    assert exc.value.details["observed"] == 1500
    assert clock.now() == 2000  # 状态不变


def test_clock_regress_within_tolerance_accepted():
    clock = LogicalClock([1000, 990], tolerance_ms=50)
    assert clock.advance() == 990


def test_waterline_regress_rejected_without_mutation(store, plan, engine):
    engine.failover(plan)
    before = store.committed_actions()
    waterline = store.get_clock_waterline()
    # 新时钟从远低于水位的值注入 → 拒绝且状态不变
    regressed = make_clock(start=waterline - 10000)
    engine2 = Engine(store, regressed, SimulatedExecutor(), SimulatedHealth())
    with pytest.raises(BizError) as exc:
        engine2.failover(plan)
    assert exc.value.code == ErrCode.E_CLOCK_REGRESS
    assert store.committed_actions() == before
