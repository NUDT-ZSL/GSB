"""任务 4：令牌桶限速、并发控制与运行统计。"""
from __future__ import annotations

import json
import os
import threading
import time

from recon.ratelimit import (
    ConcurrencyGate, IntervalLimiter, SlidingWindowRateLimiter, _WaitStats,
)


def test_rest_sliding_window_qps(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0, result.stderr
    profile = server.profile()
    rest_arrivals = sorted(
        e["ts_ms"] for e in profile["arrivals"] if e["source"] == "rest"
    )
    assert len(rest_arrivals) >= 3
    # 任意连续 1000ms 窗口内 REST 请求数 <= 5
    for i, start in enumerate(rest_arrivals):
        window = [t for t in rest_arrivals[i:] if t < start + 1000]
        assert len(window) <= 5, f"1s 窗口内出现 {len(window)} 个 REST 请求"


def test_ts_min_interval(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0
    profile = server.profile()
    ts_arrivals = sorted(
        e["ts_ms"] for e in profile["arrivals"] if e["source"] == "ts"
    )
    assert len(ts_arrivals) >= 3
    gaps = [b - a for a, b in zip(ts_arrivals, ts_arrivals[1:])]
    # 容忍 5ms 调度/网络抖动
    assert min(gaps) >= 45, f"最小 TS 间隔 {min(gaps)}ms 低于 45ms"


def test_concurrency_one_inflight(workspace):
    handle = workspace.start_server(scenario="concurrency1")
    cfg = json.load(open(workspace.config("concurrency1"), encoding="utf-8"))
    # 清理可能残留的控制文件，避免误触发受控中断
    if os.path.exists(cfg["control_file"]):
        os.remove(cfg["control_file"])
    import subprocess
    import sys
    from pathlib import Path
    proc = subprocess.Popen(
        [sys.executable, "-m", "recon.cli", "--config",
         workspace.config("concurrency1")],
        cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    observed_max = 0
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                with open(cfg["state_path"], "r", encoding="utf-8") as fh:
                    state = json.load(fh)
                observed_max = max(observed_max, state["stats"]["in_flight"])
            except (OSError, json.JSONDecodeError, KeyError):
                pass
            time.sleep(0.02)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
        handle.shutdown()
    assert proc.returncode == 0
    assert observed_max <= 1, f"观测到在途请求峰值 {observed_max} 超过并发 1"


def test_stats_fields_and_interruptible_wait():
    shutdown = threading.Event()
    stats = _WaitStats()
    limiter = SlidingWindowRateLimiter(2, shutdown, stats)
    for _ in range(3):
        limiter.acquire()
    snap = stats.snapshot()
    assert snap["completed_requests"] == 0  # 原语不计数，由引擎累加
    assert snap["in_flight"] == 0
    assert snap["wait_ms"] >= 0

    # 等待可被中断：先占满唯一许可，第二次获取必须阻塞等待
    gate = ConcurrencyGate(1, shutdown, stats)
    gate.acquire()  # 占用唯一许可且不释放

    def releaser():
        time.sleep(0.1)
        shutdown.set()  # 仅触发中断，许可仍被占用

    threading.Thread(target=releaser).start()
    from recon.errors import StoppedError
    try:
        gate.acquire()  # 阻塞等待，随后被 shutdown 打断
        raise AssertionError("应当被中断")
    except StoppedError:
        pass
    gate.release()


def test_interval_limiter_spacing():
    shutdown = threading.Event()
    stats = _WaitStats()
    interval = IntervalLimiter(50, shutdown, stats)
    stamps = []
    for _ in range(4):
        interval.acquire()
        stamps.append(time.monotonic() * 1000)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 45  # 抖动容忍
