"""任务 10：端到端中断恢复验收。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from recon.storage import Storage

ROOT = Path(__file__).resolve().parent.parent


def _start_collector(workspace, config_name="interrupt"):
    return subprocess.Popen(
        [sys.executable, "-m", "recon.cli",
         "--config", workspace.config(config_name)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_end_to_end_interrupt_resume(workspace):
    # 不中断基线哈希
    base_wd = workspace.path.parent / (workspace.path.name + "_e2e_base")
    base_port = __import__("tests.conftest", fromlist=["_free_port"])._free_port()
    from recon import generator
    from tests.conftest import Workspace
    base_manifest = generator.generate(str(base_wd), seed=42, port=base_port)
    base_ws = Workspace(base_wd, base_port, base_manifest)
    base_server = base_ws.start_server()
    try:
        assert base_ws.run_collector("base").returncode == 0
    finally:
        base_server.shutdown()
    base_cfg = json.load(open(base_ws.config("base"), encoding="utf-8"))
    truth_hash = Storage(base_cfg["db_path"]).main_hash()

    # 中断场景：武装门保证停在 REST 第 2 页提交之后、TS 开始之前
    handle = workspace.start_server(scenario="interrupt")
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    workspace.write_control("stop")
    proc = _start_collector(workspace)
    try:
        proc.communicate(timeout=30)
        assert proc.returncode != 0
        aborted = workspace.latest_report(aborted=True)
        interrupted_run_id = aborted["run_id"]

        storage = Storage(cfg["db_path"])
        rest_progress = storage.get_progress("rest")
        last_committed_page = json.loads(rest_progress["cursor_value"])
        storage.close()

        workspace.remove_control()
        done = workspace.run_collector("interrupt")
        assert done.returncode == 0, done.stderr
    finally:
        if proc.poll() is None:
            proc.kill()
        handle.shutdown()

    # 最终数据库内容与不中断真值完全一致
    storage = Storage(cfg["db_path"])
    assert storage.main_hash() == truth_hash

    final = workspace.latest_report()
    # 恢复溯源
    assert final["resumed_from_run_id"] == interrupted_run_id
    assert final["resume_count"] == 1
    # REST 恢复后首个处理游标 = 中断前最后提交游标的下一页码
    first_cursor = final["resume_detail"]["first_cursors"]["rest"]
    assert first_cursor == last_committed_page  # 进度表存的就是"下一游标"
    assert first_cursor == 3  # 第 2 页提交后下一游标为第 3 页
    storage.close()
