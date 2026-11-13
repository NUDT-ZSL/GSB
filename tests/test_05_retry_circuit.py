"""任务 5：分级重试退避、不可重试错误与熔断。"""
from __future__ import annotations


def test_rest_retries_two_500_then_success(workspace):
    handle = workspace.start_server(scenario="retry_500")
    try:
        result = workspace.run_collector("retry_500")
        assert result.returncode == 0, result.stderr
        report = workspace.latest_report()
        rest = report["sources"]["rest"]
        assert rest["status"] == "success"
        assert rest["retry_count"] == 2
        assert rest["circuit_open"] is False
    finally:
        handle.shutdown()


def test_ts_auth_fatal_others_success(workspace):
    handle = workspace.start_server(scenario="ts_auth")
    try:
        result = workspace.run_collector("ts_auth")
        # 部分源失败：进程仍可结束，报告顶层 partial
        report = workspace.latest_report()
        ts = report["sources"]["ts"]
        assert ts["status"] == "failed"
        assert ts["error_code"] == "E_AUTH"
        # 不可重试：retry_count 必须为 0
        assert ts["retry_count"] == 0
        assert report["sources"]["rest"]["status"] == "success"
        assert report["sources"]["csv"]["status"] == "success"
        assert report["run_status"] == "partial"
    finally:
        handle.shutdown()


def test_rest_circuit_breaker_opens(workspace):
    handle = workspace.start_server(scenario="circuit")
    try:
        result = workspace.run_collector("circuit")
        report = workspace.latest_report()
        rest = report["sources"]["rest"]
        assert rest["status"] == "failed"
        assert rest["circuit_open"] is True
        assert rest["error_code"] == "E_CIRCUIT_OPEN"
        # 连续 6 次 503：超过 5 次阈值，重试次数受 max_retries 与熔断共同约束
        assert rest["retry_count"] >= 5
        # 其他源不受影响
        assert report["sources"]["csv"]["status"] == "success"
    finally:
        handle.shutdown()


def test_backoff_is_exponential_with_jitter():
    from recon.retry import RetryExecutor
    captured = []
    rng_seed = 42
    ex = RetryExecutor(
        max_retries=4, backoff_base_ms=100,
        sleep_fn=lambda s: captured.append(s), rng_seed=rng_seed,
    )
    delays = [ex.backoff_ms(i) for i in range(4)]
    # 抖动因子 [0.5,1.0)：指数项 base*2^i
    import random
    rng = random.Random("recon-backoff-42")
    expected = [100 * (2 ** i) * (0.5 + rng.random() * 0.5) for i in range(4)]
    assert delays == expected
    # 指数趋势（去掉抖动后基数翻倍）
    assert delays[1] > delays[0]
