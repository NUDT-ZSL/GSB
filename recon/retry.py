"""分级重试、带抖动指数退避与熔断。

可重试：连接错误、HTTP 429、HTTP 5xx、CSV 部分写入（E_CSV_PARTIAL）。
不可重试：HTTP 400/401、JSON 解码失败、字段校验失败——立即停止当前源。
同一源连续失败超过 5 次 -> 熔断（open），源状态 failed，其他源继续。
退避抖动由固定种子 RNG 决定，保证测试可复现。
"""
from __future__ import annotations

import random
import threading

from .errors import CircuitOpenError, ReconError, StoppedError

CIRCUIT_FAIL_THRESHOLD = 5  # 连续失败超过 5 次即熔断（第 6 次触发 open）


class CircuitBreaker:
    def __init__(self, threshold: int = CIRCUIT_FAIL_THRESHOLD):
        self.threshold = threshold
        self.consecutive_failures = 0
        self.opened = False
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self.consecutive_failures > self.threshold:
                self.opened = True

    def ensure_closed(self) -> None:
        if self.opened:
            raise CircuitOpenError("熔断器已打开")


class RetryExecutor:
    """对一次 fetch 调用执行重试策略。

    返回 (result, retry_count, last_error)。不可重试错误直接抛出 ReconError。
    sleep_fn 由引擎注入（可中断、统计等待时长）。
    """

    def __init__(self, max_retries: int, backoff_base_ms: float,
                 sleep_fn, rng_seed: int = 42):
        self.max_retries = max_retries
        self.backoff_base_ms = backoff_base_ms
        self.sleep_fn = sleep_fn
        self._rng = random.Random(f"recon-backoff-{rng_seed}")

    def backoff_ms(self, attempt: int) -> float:
        # 指数退避 base * 2^attempt，抖动因子 [0.5, 1.0)
        jitter = 0.5 + self._rng.random() * 0.5
        return self.backoff_base_ms * (2 ** attempt) * jitter

    def call(self, fn, breaker: CircuitBreaker, on_retry=None):
        attempt = 0
        retries = 0
        last_error: Exception | None = None
        while True:
            breaker.ensure_closed()
            try:
                result = fn()
                breaker.record_success()
                return result, retries, None
            except StoppedError:
                raise
            except ReconError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
                breaker.record_failure()
                if breaker.opened:
                    raise CircuitOpenError(
                        f"连续失败超过 {breaker.threshold} 次，熔断打开"
                    ) from exc
                if attempt >= self.max_retries:
                    raise
                delay = self.backoff_ms(attempt) / 1000.0
                if on_retry is not None:
                    on_retry(attempt + 1, exc.code, delay)
                self.sleep_fn(delay)
                attempt += 1
                retries += 1
