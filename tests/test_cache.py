"""需求 5/8：缓存逐出裁决、双通路真值对照与容量错误码。"""
import pytest

from chunkmeta.cache import MetadataCache
from chunkmeta.errors import E_CACHE_CAPACITY, StoreError


def test_zero_or_negative_capacity_rejected():
    for bad in (0, -1):
        with pytest.raises(StoreError) as exc:
            MetadataCache(capacity=bad, scale=1000)
        assert exc.value.code == E_CACHE_CAPACITY
        assert "capacity" in exc.value.details


def test_eviction_follows_lru_clock_then_occupancy():
    cache = MetadataCache(capacity=2, scale=1000)
    cache.put("a", "x" * 100, clock=10)
    cache.put("b", "y", clock=20)
    evicted = cache.put("c", "z", clock=30)
    assert evicted == ["a"]  # 最旧逻辑时钟先淘汰
    cache.touch("c", 5)      # c 变为最旧
    evicted = cache.put("d", "w", clock=40)
    assert evicted == ["c"]


def test_dual_path_agrees_at_fine_scale():
    cache = MetadataCache(capacity=3, scale=1000)
    cache.put("alpha", "12", clock=10)
    cache.put("zeta", "1", clock=10)
    fixed_seq, decimal_seq, report, mode = cache.dual_path_order()
    assert fixed_seq == decimal_seq
    assert not report.diverged
    assert mode == "precise"


def test_dual_path_divergence_at_coarse_scale():
    # 粗标度下 0.333.. 与 0.25 都舍入到 3（并列），decimal 通路可分
    cache = MetadataCache(capacity=2, scale=10)
    cache.put("alpha", "12", clock=10)   # 4B -> 0.333.. 单位
    cache.put("zeta", "1", clock=10)     # 3B -> 0.25   单位
    fixed_seq, decimal_seq, report, mode = cache.dual_path_order()
    assert fixed_seq == ["alpha", "zeta"]       # 定点并列 → 按键名次序
    assert decimal_seq == ["zeta", "alpha"]     # 真值 0.25 < 0.333..
    assert report.diverged
    assert report.first_divergence_index == 0
    assert report.fixed_entry == "alpha"
    assert report.decimal_entry == "zeta"
    assert report.fixed_intermediate["occupancy_fixed"] == 3
    assert report.decimal_intermediate["occupancy_decimal"] == "0.25"
    assert len(report.conversion_steps) == 2  # 参与换算步骤可溯源


def test_cache_batch_mode_switch():
    cache = MetadataCache(capacity=20, scale=1000, batch_threshold=4)
    for i in range(6):
        cache.put(f"k{i}", "v", clock=i)
    _f, _d, _r, mode = cache.dual_path_order()
    assert mode == "batch"
