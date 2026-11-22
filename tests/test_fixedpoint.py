"""需求 3：受控定点换算的确定性、余量保留与累加参与。"""
from fractions import Fraction

from chunkmeta.fixedpoint import FixedAccumulator, to_fixed


def test_conversion_is_deterministic_bit_exact():
    a = to_fixed(0.1, 1000, stage="s")
    b = to_fixed(0.1, 1000, stage="s")
    assert a == b  # 同一输入重复换算逐位一致
    assert a.fixed == 100
    # 0.1 的二进制浮点略大于 1/10，放大后余量为正、方向向下
    assert a.direction == "down"
    assert a.remainder > 0


def test_remainder_and_direction_recorded():
    rec = to_fixed(1.0 / 3.0, 10, stage="occ")
    assert rec.fixed == 3
    assert rec.direction == "down"
    assert rec.remainder > 0
    assert rec.original == 1.0 / 3.0  # 原始值保留用于溯源
    rec2 = to_fixed(0.25, 10, stage="occ")
    assert rec2.fixed == 3
    assert rec2.direction == "up"
    assert rec2.remainder == Fraction(-1, 2)


def test_sub_unit_remainders_participate_in_accumulation():
    acc = FixedAccumulator(scale=10)
    for _ in range(3):
        acc.add(to_fixed(1.0 / 3.0, 10, stage="occ"))
    # 每项余量约 1/3 个定点单位，逐项保留并参与累加
    assert acc.total_fixed == 9
    single = to_fixed(1.0 / 3.0, 10, stage="occ").remainder
    assert acc.remainder_sum == 3 * single
    assert acc.true_scaled == Fraction(9) + 3 * single
    assert acc.error_bound == 3 * abs(single)
    assert abs(acc.remainder_sum - 1) < Fraction(1, 1000)


def test_error_bound_grows_with_each_step():
    acc = FixedAccumulator(scale=100)
    acc.add(to_fixed(0.555, 100, stage="a"))
    acc.add(to_fixed(0.555, 100, stage="b"))
    assert len(acc.steps) == 2
    assert acc.error_bound > 0
    assert acc.error_bound_units() == acc.error_bound / 100
