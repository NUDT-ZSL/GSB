"""任务 7：JSON 运行报告、Schema 校验、aborted 临时报告与恢复溯源。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from recon.report import validate_report

ROOT = Path(__file__).resolve().parent.parent


def _start_collector(workspace, config_name="interrupt"):
    return subprocess.Popen(
        [sys.executable, "-m", "recon.cli",
         "--config", workspace.config(config_name)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_normal_report_passes_schema(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0
    report = workspace.latest_report()
    errors = validate_report(report)
    assert errors == []
    # 必需字段齐备
    for field in ("run_id", "run_status", "started_at", "finished_at",
                  "sources", "resumed_from_run_id", "resume_count"):
        assert field in report
    for source in ("rest", "ts", "csv"):
        src = report["sources"][source]
        for field in ("status", "fetched", "duplicate_dropped", "retry_count",
                      "cursor", "error_code", "committed_batches"):
            assert field in src
    assert report["run_status"] == "success"
    assert report["resumed_from_run_id"] is None


def test_aborted_report_and_resume_link(workspace):
    handle = workspace.start_server(scenario="interrupt")
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    workspace.write_control("stop")
    proc = _start_collector(workspace)
    try:
        proc.communicate(timeout=30)
        assert proc.returncode != 0
        aborted = workspace.latest_report(aborted=True)
        assert aborted["run_status"] == "aborted"
        # 临时报告文件名包含 run_id
        files = list((workspace.path / "reports").glob(
            f"report_{aborted['run_id']}.aborted.json"))
        assert files
        assert validate_report(aborted) == []
        first_run_id = aborted["run_id"]

        workspace.remove_control()
        done = workspace.run_collector("interrupt")
        assert done.returncode == 0, done.stderr
        final = workspace.latest_report()
        assert final["run_status"] == "success"
        assert final["resumed_from_run_id"] == first_run_id
        assert final["resume_count"] == 1
        assert validate_report(final) == []
    finally:
        if proc.poll() is None:
            proc.kill()
        handle.shutdown()
