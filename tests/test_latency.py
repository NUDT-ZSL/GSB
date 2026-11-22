"""需求 4：延迟装配、误差上界累计与精度敏感标记。"""
from chunkmeta.latency import assemble_latency

from .conftest import make_config


def test_latency_accumulates_stages():
    config = make_config()
    report = assemble_latency(
        [("cache_lookup", 0.5), ("digest_verify#0", 1.25), ("concat", 0.25)],
        config)
    assert report.total_fixed == 2000  # 2.0ms @ scale=1000
    assert report.within_budget
    assert not report.precision_sensitive
    assert report.error_bound_ms >= 0


def test_precision_sensitive_when_diff_within_tolerance():
    config = make_config(read_latency_budget_ms=50.0, compare_tolerance=0.05)
    # 总耗时 49.97ms，与预算差 0.03ms < 容差 0.05ms → 精度敏感
    report = assemble_latency([("digest_verify#0", 49.97)], config)
    assert report.within_budget
    assert report.precision_sensitive
    assert report.error_chain, "精度敏感时必须输出误差来源链"
    step = report.error_chain[0]
    assert step["stage"] == "digest_verify#0"
    assert step["position"] == 0
    assert "remainder" in step


def test_precision_sensitive_flips_conclusion_near_boundary():
    config = make_config(read_latency_budget_ms=50.0, compare_tolerance=0.05)
    over = assemble_latency([("digest_verify#0", 50.02)], config)
    assert not over.within_budget
    assert over.precision_sensitive  # 结论依赖 0.02ms 的差值


def test_error_bound_budget_exceeded():
    config = make_config(error_bound_budget=0.001, fixed_scale=10)
    stages = [(f"digest_verify#{i}", 1.0 / 3.0) for i in range(30)]
    report = assemble_latency(stages, config)
    assert report.error_budget_exceeded
    assert report.error_bound_ms > config.error_bound_budget
