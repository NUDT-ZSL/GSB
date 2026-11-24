from __future__ import annotations

import copy
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from animrecalc.edits import load_edit_script
from animrecalc.engine import AnimEngine
from animrecalc.pipeline import load_project_files, project_version
from animrecalc.sample_gen import generate_sample_tree
from animrecalc.storage import ChunkStore


def compact_report(report: dict) -> dict:
    keys = [
        "ok",
        "status",
        "mode",
        "total_chunks",
        "frame_count",
        "edit_count",
        "recomputed_chunks",
        "reused_chunks",
        "ported_chunks",
        "first_mismatch",
        "recovery_mode",
    ]
    return {key: report.get(key) for key in keys if key in report}


def main() -> int:
    acceptance_dir = ROOT / "artifacts" / "acceptance"
    if acceptance_dir.exists():
        shutil.rmtree(acceptance_dir)
    acceptance_dir.mkdir(parents=True)

    sample_dir = ROOT / "samples"
    generate_sample_tree(sample_dir)
    project = load_project_files(
        sample_dir / "skeleton.json",
        sample_dir / "clips.json",
        sample_dir / "state_machine.json",
        sample_dir / "chunk_spec.json",
    )
    edits = load_edit_script(sample_dir / "edit_script.json")

    checks: list[dict] = []

    def check(name: str, condition: bool, details: dict | None = None) -> None:
        checks.append({"name": name, "passed": bool(condition), "details": details or {}})
        if not condition:
            raise AssertionError(f"acceptance check failed: {name}")

    unittest_log = acceptance_dir / "unittest.log"
    with unittest_log.open("w", encoding="utf-8") as log:
        test_proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_acceptance", "-v"],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    test_output = unittest_log.read_text(encoding="utf-8", errors="replace")
    check(
        "unittest_suite",
        test_proc.returncode == 0,
        {"returncode": test_proc.returncode, "tail": "\n".join(test_output.splitlines()[-12:])},
    )

    inc_state = acceptance_dir / "incremental-state"
    engine = AnimEngine(project, inc_state, max_workers=4)
    baseline = engine.run_or_resume_full()
    check("baseline_completed", baseline["status"] == "completed" and baseline["verification"]["consistent"], compact_report(baseline))

    incremental = engine.run_edit_script(edits)
    check(
        "incremental_consistent",
        incremental["status"] == "completed" and incremental["ok"] and incremental["first_mismatch"] is None,
        compact_report(incremental),
    )
    step_summaries = []
    for step in incremental["step_reports"]:
        summary = {
            "edit_index": step["edit_index"],
            "operation": step["operation"],
            "dirty_chunks": step["dirty_chunks"],
            "recomputed_chunks": step["recomputed_chunks"],
            "reused_chunks": step["reused_chunks"],
            "ported_chunks": step["ported_chunks"],
            "consistent": step["consistency"]["consistent"],
            "max_abs_diff": step["consistency"]["max_abs_diff"],
        }
        step_summaries.append(summary)
        check(f"step_{step['edit_index']}_consistent", summary["consistent"], summary)

    key_step = incremental["step_reports"][0]
    check(
        "key_closure_is_partial_and_includes_descendants",
        0 < key_step["recomputed_chunks"] < incremental["total_chunks"]
        and "lower_arm_r" in key_step["closure"]["affected_bones"],
        {"dirty": key_step["dirty_chunks"], "bones": key_step["closure"]["affected_bones"]},
    )
    duration_step = incremental["step_reports"][2]
    check(
        "duration_ripple_ports_suffix",
        duration_step["ported_chunks"] > 0 and duration_step["dirty_chunks"] != list(range(incremental["total_chunks"])),
        {"dirty": duration_step["dirty_chunks"], "ported": duration_step["ported_chunks"]},
    )

    interrupt_state = acceptance_dir / "interrupt-state"
    interrupted = AnimEngine(project, interrupt_state, max_workers=1, interrupt_after_chunks=2).run_or_resume_full()
    check("graceful_interrupt_exit_boundary", interrupted["status"] == "interrupted", compact_report(interrupted))
    marker = json.loads((interrupt_state / "INTERRUPTED.json").read_text(encoding="utf-8"))
    check("interrupt_marker_written", marker["completed_count"] == 2 and marker["exit_code"] == 75, marker)

    resumed = AnimEngine(project, interrupt_state, max_workers=3).run_or_resume_full()
    check(
        "same_version_resume_reuses_valid_chunks",
        resumed["status"] == "completed"
        and resumed["reused_chunks"] == 2
        and resumed["verification"]["consistent"],
        compact_report(resumed),
    )

    store = ChunkStore(interrupt_state)
    fault_index = 5
    fault_path = store.chunk_path(fault_index)
    fault_payload = json.loads(fault_path.read_text(encoding="utf-8"))
    fault_payload["pose"][0][0][0] += 0.375
    fault_path.write_text(json.dumps(fault_payload), encoding="utf-8")
    ok, reason, _ = store.verify_chunk(fault_index, project_version(project))
    check("digest_fault_detected", not ok and reason == "content digest mismatch", {"chunk": fault_index, "reason": reason})
    recovered = AnimEngine(project, interrupt_state, max_workers=2).run_or_resume_full()
    check(
        "digest_fault_falls_back_to_recompute",
        recovered["status"] == "completed"
        and recovered["recomputed_chunks"] == 1
        and recovered["verification"]["consistent"],
        compact_report(recovered),
    )

    version_state = acceptance_dir / "version-state"
    AnimEngine(project, version_state, max_workers=2).run_or_resume_full()
    edited_project = copy.deepcopy(project)
    edited_project["clips"][1]["duration_ms"] = 850
    version_report = AnimEngine(edited_project, version_state, max_workers=2).run_or_resume_full()
    archive_count = len(list((version_state / "archive").rglob("chunk_*.json")))
    check(
        "version_mismatch_archives_and_full_recomputes",
        version_report["recovery_mode"] == "global_version_mismatch_full_recompute"
        and archive_count >= version_report["total_chunks"]
        and version_report["verification"]["consistent"],
        {"archive_chunks": archive_count, **compact_report(version_report)},
    )

    report = {
        "ok": all(item["passed"] for item in checks),
        "python": sys.version,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "seed": 20261125,
        "version_id": project_version(project),
        "sample": {
            "bones": len(project["skeleton"]["bones"]),
            "clips": len(project["clips"]),
            "chunks": baseline["total_chunks"],
            "frames": baseline["frame_count"],
            "events": baseline["verification"]["event_count"],
        },
        "checks": checks,
        "incremental_summary": compact_report(incremental),
        "incremental_steps": step_summaries,
        "interrupt_summary": compact_report(interrupted),
        "resume_summary": compact_report(resumed),
        "digest_recovery_summary": compact_report(recovered),
        "version_mismatch_summary": compact_report(version_report),
        "archive_chunk_files": archive_count,
        "unittest_log": "artifacts/acceptance/unittest.log",
    }
    report_path = ROOT / "artifacts" / "acceptance_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        "# 实际验收结果",
        "",
        f"- 结论：{'全部通过' if report['ok'] else '存在失败'}",
        f"- Python：`{sys.version.split()[0]}`；NumPy：`{np.__version__}`；平台：`{platform.platform()}`",
        f"- 固定种子：`20261125`；版本摘要：`{project_version(project)}`",
        f"- 样本规模：{report['sample']['bones']} 骨、{report['sample']['clips']} 片段、{report['sample']['frames']} 帧、{report['sample']['chunks']} 块、{report['sample']['events']} 个转移事件",
        f"- 单元测试：返回码 `{test_proc.returncode}`，日志 `artifacts/acceptance/unittest.log`",
        "",
        "## 增量闭包",
        "",
        "| 步骤 | 算子 | 失效块 | 重算 | 复用 | 移植 | 最大姿态差 |",
        "|---:|---|---|---:|---:|---:|---:|",
    ]
    for step in step_summaries:
        op = step["operation"].get("op", "")
        target = step["operation"].get("clip") or step["operation"].get("transition_id") or ""
        md_lines.append(
            f"| {step['edit_index']} | `{op}` {target} | `{step['dirty_chunks']}` | {step['recomputed_chunks']} | {step['reused_chunks']} | {step['ported_chunks']} | {step['max_abs_diff']:.3g} |"
        )
    md_lines.extend(
        [
            "",
            "## 恢复与持久化",
            "",
            f"- 可处理中断：完成 2 块后退出码 `75`，标记 `INTERRUPTED.json` 已写入。",
            f"- 同版本恢复：重算 {resumed['recomputed_chunks']} 块、复用 {resumed['reused_chunks']} 块，一致性 `{resumed['verification']['consistent']}`。",
            f"- 摘要篡改：`E_DIGEST` 检出后仅重算 1 块，恢复一致性 `{recovered['verification']['consistent']}`。",
            f"- 版本变化：归档 {archive_count} 个旧块文件并全量重算，一致性 `{version_report['verification']['consistent']}`。",
            "- 真实 `SIGTERM` 子进程与恢复路径包含在 `tests/test_acceptance.py`；Windows 分支使用 `CTRL_BREAK_EVENT`。",
            "",
        ]
    )
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "validation-report.md").write_text("\n".join(md_lines), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(report_path)}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
