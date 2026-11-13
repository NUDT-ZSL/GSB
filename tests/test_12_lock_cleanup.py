"""任务 12：单实例互斥、资源清理、模拟服务停止计数。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tests.conftest import _wait_port

ROOT = Path(__file__).resolve().parent.parent


def _start_collector(workspace, config_name="base"):
    return subprocess.Popen(
        [sys.executable, "-m", "recon.cli",
         "--config", workspace.config(config_name)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_lock_held(lock_path: str, timeout: float = 10.0) -> bool:
    """等待锁文件被持有者锁定。

    用独立 FileLock 尝试获取：能拿到说明持有者尚未持锁（立即释放），
    拿不到（LockError）说明持有者已持锁。这是最可靠的跨进程判定。
    """
    from recon.errors import LockError
    from recon.locking import FileLock
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not os.path.exists(lock_path):
            time.sleep(0.1)
            continue
        probe = FileLock(lock_path, timeout_s=0.3)
        try:
            probe.acquire(remove_on_release=False)
        except LockError:
            return True  # 持有者已持锁
        else:
            # 持有者还没持锁：仅释放内核锁、关闭句柄，不删除锁文件
            probe._locked = False
            if probe._fh is not None:
                probe._kernel_unlock()
                probe._fh.close()
                probe._fh = None
        time.sleep(0.1)
    return False


def test_second_process_locked_then_relock(workspace):
    """真实双进程：第一个持锁运行，第二个 5 秒内 E_LOCKED 退出，退出后可重锁。"""
    handle = workspace.start_server()
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    # 低 QPS + 大间隔，确保首进程持锁后稳定处于限速等待
    slow = dict(cfg)
    slow["qps"] = 1
    slow["ts_min_interval_ms"] = 300
    slow_path = workspace.path / "slow.json"
    slow_path.write_text(json.dumps(slow), encoding="utf-8")
    p1 = subprocess.Popen(
        [sys.executable, "-m", "recon.cli", "--config", str(slow_path)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # 屏障：确认 p1 已实际持锁后再启动 p2，消除启动竞争窗口
        assert _wait_lock_held(cfg["lock_path"])
        t0 = time.monotonic()
        p2 = subprocess.run(
            [sys.executable, "-m", "recon.cli", "--config", str(slow_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        elapsed = time.monotonic() - t0
        assert p2.returncode != 0
        assert "E_LOCKED" in p2.stderr
        assert elapsed <= 5.5  # 5 秒超时内退出
    finally:
        p1.terminate()
        try:
            p1.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p1.kill()
        handle.shutdown()

    # 锁释放后新进程能在 5 秒内获取锁并正常运行
    handle2 = workspace.start_server()
    try:
        proc2 = _start_collector(workspace)
        _, stderr2 = proc2.communicate(timeout=60)
        assert proc2.returncode == 0, stderr2
    finally:
        handle2.shutdown()


def test_lock_file_relockable_after_exit(workspace, server):
    """进程退出后锁文件可能存在，但可被重新加锁。"""
    result = workspace.run_collector("base")
    assert result.returncode == 0
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    from recon.locking import FileLock
    lock = FileLock(cfg["lock_path"], timeout_s=5.0)
    lock.acquire()  # 必须能立即重新加锁
    lock.release()


def test_server_shutdown_frees_port_and_reports_count(workspace):
    handle = workspace.start_server()
    # 产生若干请求
    import requests
    for _ in range(3):
        requests.get(f"http://127.0.0.1:{workspace.port}/health", timeout=5)
    data = handle.shutdown()
    assert data["total_requests"] >= 3
    # 端口在 2 秒内不可连接
    assert _wait_port("127.0.0.1", workspace.port, timeout=2, expect_closed=True)


def test_resources_released_after_run(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    # 报告存在、状态成功，说明会话/连接正常关闭
    report = workspace.latest_report()
    assert report["run_status"] == "success"
    # SQLite 可被重新打开（连接已释放）
    import sqlite3
    conn = sqlite3.connect(cfg["db_path"])
    conn.execute("SELECT 1")
    conn.close()
