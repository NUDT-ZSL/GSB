"""确定性演示：并发采集、去重、会话恢复、强制终止后续跑、渲染超时。

可重复执行：每次运行使用独立临时数据库，输出规范化的汇总 JSON，
两次运行的汇总（不含时间戳/PID）完全一致。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import httpx
import uvicorn

from app.mock_site import TOTAL_TICKETS, create_app as create_mock_app

MOCK_PORT = 8100
CONSOLE_PORT = 8200
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"
CONSOLE_BASE = f"http://127.0.0.1:{CONSOLE_PORT}"

TASK_CONFIG = {
    "concurrency": 4,
    "max_retries": 4,
    "backoff_base_ms": 50,
    "render_timeout_ms": 8000,
    "session_replay_limit": 10,
    "source": "demo",
}

MOCK_TOGGLES = {
    "render_delay_ms": 250,
    "session_ttl_requests": 6,
    "flaky_503": {"detail:TK-1003": 2, "detail:TK-1007": 99},
}


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"timeout waiting for {url}")


class MockServer:
    def __init__(self):
        config = uvicorn.Config(create_mock_app(), host="127.0.0.1",
                                port=MOCK_PORT, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        wait_http(f"{MOCK_BASE}/__config")
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)

    def configure(self, toggles: dict) -> None:
        r = httpx.post(f"{MOCK_BASE}/__config", json={"toggles": toggles}, timeout=5)
        r.raise_for_status()


class ConsoleProc:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.proc: subprocess.Popen | None = None

    def start(self):
        env = dict(os.environ, MOCK_BASE_URL=MOCK_BASE, CONSOLE_DB_PATH=self.db_path)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.console", "--port", str(CONSOLE_PORT),
             "--db", self.db_path],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_http(f"{CONSOLE_BASE}/api/tasks")

    def kill_hard(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)


def create_task(config: dict) -> int:
    r = httpx.post(f"{CONSOLE_BASE}/api/tasks",
                   json={"config": config, "autostart": True}, timeout=10)
    r.raise_for_status()
    return r.json()["task_id"]


def get_summary(task_id: int) -> dict:
    return httpx.get(f"{CONSOLE_BASE}/api/tasks/{task_id}", timeout=10).json()


def wait_terminal(task_id: int, states=("succeeded", "failed", "paused"),
                  timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = get_summary(task_id)
        if s["task"]["status"] in states:
            return s
        time.sleep(0.3)
    raise RuntimeError(f"task {task_id} not terminal within {timeout}s")


def resume(task_id: int) -> None:
    r = httpx.post(f"{CONSOLE_BASE}/api/tasks/{task_id}/resume", timeout=10)
    r.raise_for_status()


def canonical(summary: dict) -> dict:
    """规范化汇总：剔除时间戳等不确定字段，用于跨运行比对。"""
    failures = {}
    for f in summary["item_failures"]:
        failures.setdefault(f["ticket_id"], f["code"])
    return {
        "status": summary["task"]["status"],
        "error_code": summary["task"]["error_code"],
        "stored_items": summary["stored_items"],
        "distinct_items": summary["distinct_items"],
        "checkpoints": summary["checkpoints"],
        "parse_status": summary["parse_status"],
        "failure_by_ticket": failures,
        "snapshots": summary["snapshots"],
    }


def scenario_clean(db: str) -> dict:
    """场景 A：干净运行至成功（含 503 退避恢复与会话自动重登）。"""
    console = ConsoleProc(db)
    console.start()
    try:
        tid = create_task(TASK_CONFIG)
        s = wait_terminal(tid)
        assert s["task"]["status"] == "succeeded", s["task"]
        return canonical(s)
    finally:
        console.stop()


def scenario_kill_resume(db: str) -> dict:
    """场景 B：运行中 SIGKILL 控制台进程，重启后仅凭库中状态续跑。"""
    console = ConsoleProc(db)
    console.start()
    tid = create_task(TASK_CONFIG)
    deadline = time.time() + 60
    while time.time() < deadline:
        if get_summary(tid)["stored_items"] >= 8:
            break
        time.sleep(0.1)
    console.kill_hard()  # 强制终止，不承诺补写退出报告
    console.start()      # 重启：running 遗留任务被恢复为 paused
    s = get_summary(tid)
    assert s["task"]["status"] == "paused", s["task"]
    resume(tid)
    try:
        s = wait_terminal(tid)
        assert s["task"]["status"] == "succeeded", s["task"]
        assert s["stored_items"] == s["distinct_items"], "存在重复入库"
        return canonical(s)
    finally:
        console.stop()


def scenario_session_exhausted(db: str, mock: MockServer) -> dict:
    """场景 C：会话持续失效 -> E_SESSION 且任务保持可恢复。"""
    mock.configure({**MOCK_TOGGLES, "session_fail_always": True})
    console = ConsoleProc(db)
    console.start()
    try:
        tid = create_task({**TASK_CONFIG, "session_replay_limit": 3})
        s = wait_terminal(tid)
        assert s["task"]["status"] == "paused", s["task"]
        assert s["task"]["error_code"] == "E_SESSION", s["task"]
        mock.configure({**MOCK_TOGGLES, "session_fail_always": False,
                        "session_ttl_requests": 1000})
        resume(tid)
        s = wait_terminal(tid)
        assert s["task"]["status"] == "succeeded", s["task"]
        return canonical(s)
    finally:
        console.stop()


def scenario_render_timeout(db: str) -> dict:
    """场景 D：渲染超时 -> E_RENDER_TIMEOUT 并记录观测快照。"""
    console = ConsoleProc(db)
    console.start()
    try:
        tid = create_task({**TASK_CONFIG, "render_timeout_ms": 80, "max_retries": 1})
        s = wait_terminal(tid)
        assert s["task"]["status"] == "failed", s["task"]
        assert s["task"]["error_code"] == "E_RENDER_TIMEOUT", s["task"]
        assert s["snapshots"] >= 1, "未记录观测快照"
        return canonical(s)
    finally:
        console.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="uid4-demo-")
    results: dict[str, dict] = {}
    with MockServer() as mock:
        mock.configure(MOCK_TOGGLES)
        results["A_clean"] = scenario_clean(os.path.join(workdir, "a.db"))
        mock.configure(MOCK_TOGGLES)
        results["B_kill_resume"] = scenario_kill_resume(os.path.join(workdir, "b.db"))
        mock.configure(MOCK_TOGGLES)
        results["C_session"] = scenario_session_exhausted(os.path.join(workdir, "c.db"), mock)
        mock.configure(MOCK_TOGGLES)
        results["D_render_timeout"] = scenario_render_timeout(os.path.join(workdir, "d.db"))

    a, b = results["A_clean"], results["B_kill_resume"]
    assert a["stored_items"] == b["stored_items"], (a, b)
    assert a["distinct_items"] == b["distinct_items"], (a, b)
    assert a["status"] == b["status"] == "succeeded", (a, b)
    assert a["failure_by_ticket"] == b["failure_by_ticket"], (a, b)
    assert a["stored_items"] == TOTAL_TICKETS - 1, a  # TK-1007 为条目级失败
    assert a["failure_by_ticket"] == {"TK-1007": "E_HTTP_503"}, a

    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    print("DEMO OK: 场景 A/B 汇总一致，去重、会话恢复、渲染超时与强制终止续跑均符合预期")
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"workdir: {workdir}")


if __name__ == "__main__":
    main()
