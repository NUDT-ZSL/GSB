"""任务 2：可持久化进度状态机与强制中断恢复。"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from recon import generator
from recon.storage import Storage
from tests.conftest import Workspace, _free_port


def _start_collector(workspace, config_name="interrupt"):
    import sys
    cmd = [sys.executable, "-m", "recon.cli",
           "--config", workspace.config(config_name)]
    return subprocess.Popen(
        cmd, cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )


def _wait_rest_batches(workspace, target: int, timeout: float = 30.0) -> None:
    db = workspace.manifest["configs"]["base"]
    cfg = json.load(open(db, encoding="utf-8"))
    deadline = time.monotonic() + timeout
    storage = Storage(cfg["db_path"])
    try:
        while time.monotonic() < deadline:
            progress = storage.get_progress("rest")
            if progress and progress["committed_batches"] >= target:
                return
            time.sleep(0.1)
        raise AssertionError(f"REST 已提交批次未在超时内达到 {target}")
    finally:
        storage.close()


def test_resume_after_interrupt_no_loss_no_dup(workspace):
    # interrupt 场景：REST 第 3 页请求服务端延迟 1500ms，
    # 采集器在第 2 页提交后、发起/等待第 3 页期间必能观察到控制文件
    handle = workspace.start_server(scenario="interrupt")
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    # 预置 stop：窗口1（页1、2）提交结束进入窗口2 的 _check_stop 时立即中断
    workspace.write_control("stop")
    proc = _start_collector(workspace)
    try:
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode != 0  # 中断退出码非零
        aborted = workspace.latest_report(aborted=True)
        assert aborted["run_status"] == "aborted"
        first_run_id = aborted["run_id"]

        # 中断点校验：REST 恰好提交 2 批，TS/CSV 尚未产生进度
        storage = Storage(cfg["db_path"])
        assert storage.get_progress("ts") is None
        assert storage.get_progress("csv") is None
        rest_progress = storage.get_progress("rest")
        assert rest_progress["committed_batches"] == 2
        storage.close()

        # 重启采集进程直至完成（控制文件需移除）
        workspace.remove_control()
        done = workspace.run_collector("interrupt")
        assert done.returncode == 0, done.stderr
        final_report = workspace.latest_report()
        assert final_report["run_status"] == "success"
        assert final_report["resumed_from_run_id"] == first_run_id
    finally:
        if proc.poll() is None:
            proc.kill()
        handle.shutdown()

    # 最终主表每个自然键仅一次
    storage = Storage(cfg["db_path"])
    truth = workspace.truth()
    rows = storage.main_rows()
    serials = [r["serial_no"] for r in rows]
    assert len(serials) == len(set(serials))
    assert storage.main_hash() == truth["content_hash"]

    # 两段运行 committed_batches 之和 == 不中断完整运行的总批次数
    # 在独立工作区取得不中断基线（验收口径：与完整运行比较，而非硬编码）
    base_wd = workspace.path.parent / (workspace.path.name + "_baseline")
    base_port = _free_port()
    base_manifest = generator.generate(str(base_wd), seed=42, port=base_port)
    base_ws = Workspace(base_wd, base_port, base_manifest)
    base_server = base_ws.start_server()
    try:
        base_proc = base_ws.run_collector("base")
        assert base_proc.returncode == 0, base_proc.stderr
    finally:
        base_server.shutdown()
    base_cfg = json.load(open(base_ws.config("base"), encoding="utf-8"))
    base_storage = Storage(base_cfg["db_path"])
    baseline_total = len(base_storage.batch_rows())
    base_storage.close()

    batches = storage.batch_rows()
    by_run: dict[str, int] = {}
    for b in batches:
        by_run[b["run_id"]] = by_run.get(b["run_id"], 0) + 1
    assert sum(by_run.values()) == baseline_total

    # 进度表每源仅一行，游标为最后已提交批次的下一游标
    progress = storage.all_progress()
    assert set(progress) == {"rest", "ts", "csv"}
    assert json.loads(progress["rest"]["cursor_value"]) == 4
    storage.close()


def test_force_kill_recovers_via_sqlite(workspace):
    """强制终止（无 aborted 报告）后，依据 SQLite 持久化进度恢复。"""
    handle = workspace.start_server(scenario="interrupt")
    proc = _start_collector(workspace)
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    try:
        _wait_rest_batches(workspace, 2)
        # 强制 kill：不给清理机会，不产生 aborted 报告
        proc.kill()
        proc.wait(timeout=10)
        reports_dir = Path(cfg["report_dir"])
        aborted_before = list(reports_dir.glob("report_*.aborted.json"))
        assert aborted_before == []  # 强杀没有 aborted 报告

        workspace.remove_control()
        done = workspace.run_collector("interrupt")
        assert done.returncode == 0, done.stderr
        report = workspace.latest_report()
        # 通过 run_meta 恢复，resumed_from 指向被强杀运行的 run_id
        storage = Storage(cfg["db_path"])
        assert report["resumed_from_run_id"] is not None
        assert storage.main_hash() == workspace.truth()["content_hash"]
        storage.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        handle.shutdown()
