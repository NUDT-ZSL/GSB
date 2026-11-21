from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .config import PipelineConfig
from .errors import PipelineError, error
from .jobs import generate_sample_jobs, write_jobs_jsonl
from .snapshots import canonical_json
from .verify import verify_run


def _config_arguments(config: PipelineConfig) -> list[str]:
    return [
        "--atol",
        repr(config.atol),
        "--rtol",
        repr(config.rtol),
        "--max-stage-jobs",
        str(config.max_stage_jobs),
        "--max-blocks",
        str(config.max_block_count),
        "--max-accum-bytes",
        str(config.max_accum_bytes),
        "--subnormal-mode",
        config.subnormal_mode,
        "--rounding-mode",
        config.rounding_mode,
    ]


def _run_child(arguments: list[str], project_root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(project_root)
        if not existing
        else str(project_root) + os.pathsep + existing
    )
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "bmm_pipeline.cli", *arguments],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _parse_child_json(process: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise error(
            "E_INCONSISTENT",
            f"{label} 子进程未返回有效 JSON 报告",
            details={
                "returncode": process.returncode,
                "stdout": process.stdout[-2000:],
                "stderr": process.stderr[-2000:],
            },
        ) from exc
    return value if isinstance(value, dict) else {}


def _load_arrays(run_dir: Path, summary: dict[str, Any]) -> dict[str, np.ndarray]:
    arrays: dict[str, np.ndarray] = {}
    for job in summary.get("jobs", []):
        path = run_dir / "results" / job["output_file"]
        arrays[job["job_id"]] = np.asarray(np.load(path, allow_pickle=False))
    return arrays


def run_demo(
    work_dir: str | Path,
    *,
    config: PipelineConfig | None = None,
    jobs_path: str | Path | None = None,
    fault_after_blocks: int = 20,
) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parent.parent
    root = Path(work_dir).resolve()
    custom_jobs = Path(jobs_path).resolve() if jobs_path is not None else None
    if root == project_root or root in project_root.parents:
        raise error(
            "E_JOB_INVALID",
            "演示工作目录不能是项目根目录或其父目录",
            field="work_dir",
            details={"work_dir": str(root)},
        )
    if custom_jobs is not None and root in custom_jobs.parents:
        raise error(
            "E_JOB_INVALID",
            "自定义任务清单不能位于将被清理的演示工作目录内",
            field="jobs",
            details={"jobs": str(custom_jobs), "work_dir": str(root)},
        )
    if root.exists():
        if not root.is_dir():
            raise error(
                "E_JOB_INVALID",
                "演示工作目录必须是目录",
                field="work_dir",
                details={"work_dir": str(root)},
            )
        entries = list(root.iterdir())
        marker = root / ".bmm_demo"
        if entries and not marker.exists():
            raise error(
                "E_JOB_INVALID",
                "拒绝清理非演示目录；请使用空目录或带 .bmm_demo 标记的目录",
                field="work_dir",
                details={"work_dir": str(root)},
            )
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".bmm_demo").write_text("bmm-pipeline demo\n", encoding="utf-8")
    effective_config = config or PipelineConfig()
    if custom_jobs is None:
        sample_path = root / "jobs.jsonl"
        write_jobs_jsonl(sample_path, generate_sample_jobs())
    else:
        sample_path = custom_jobs
    baseline_run = root / "baseline"
    restarted_run = root / "restarted"
    baseline_command = [
        "run",
        "--jobs",
        str(sample_path),
        "--run-dir",
        str(baseline_run),
        *_config_arguments(effective_config),
    ]
    crash_command = [
        "run",
        "--jobs",
        str(sample_path),
        "--run-dir",
        str(restarted_run),
        "--fault-after-blocks",
        str(fault_after_blocks),
        *_config_arguments(effective_config),
    ]
    resume_command = [
        "run",
        "--jobs",
        str(sample_path),
        "--run-dir",
        str(restarted_run),
        *_config_arguments(effective_config),
    ]
    baseline_process = _run_child(baseline_command, project_root)
    if baseline_process.returncode != 0:
        raise error(
            "E_INCONSISTENT",
            "基线完整运行失败",
            details={
                "returncode": baseline_process.returncode,
                "stdout": baseline_process.stdout[-2000:],
                "stderr": baseline_process.stderr[-2000:],
            },
        )
    crash_process = _run_child(crash_command, project_root)
    if crash_process.returncode == 0:
        raise error(
            "E_INCONSISTENT",
            "故障注入未触发硬退出；请减小 --fault-after-blocks",
            details={"fault_after_blocks": fault_after_blocks},
        )
    resume_process = _run_child(resume_command, project_root)
    if resume_process.returncode != 0:
        raise error(
            "E_INCONSISTENT",
            "强制终止后的恢复运行失败",
            details={
                "returncode": resume_process.returncode,
                "stdout": resume_process.stdout[-2000:],
                "stderr": resume_process.stderr[-2000:],
            },
        )
    recovery_report = _parse_child_json(resume_process, "恢复")
    baseline_summary_path = baseline_run / "final_results.json"
    restarted_summary_path = restarted_run / "final_results.json"
    baseline_summary = json.loads(baseline_summary_path.read_text(encoding="utf-8"))
    restarted_summary = json.loads(restarted_summary_path.read_text(encoding="utf-8"))
    summary_identical = canonical_json(baseline_summary) == canonical_json(
        restarted_summary
    )
    baseline_arrays = _load_arrays(baseline_run, baseline_summary)
    restarted_arrays = _load_arrays(restarted_run, restarted_summary)
    elementwise: dict[str, bool] = {}
    for job_id, baseline_array in baseline_arrays.items():
        other = restarted_arrays.get(job_id)
        elementwise[job_id] = bool(
            other is not None
            and baseline_array.shape == other.shape
            and np.array_equal(baseline_array, other, equal_nan=True)
        )
    verification = verify_run(restarted_run)
    recovery = recovery_report.get("recovery_report", {})
    committed_stages = [row["stage"] for row in recovery.get("committed_stages", [])]
    rebuilt_stages = recovery.get("rebuilt_stages", [])
    discarded_blocks = int(recovery.get("discarded_blocks", 0))
    recovery_shape_ok = (
        "validation" in committed_stages
        and "planning" in committed_stages
        and "accumulation" not in committed_stages
        and rebuilt_stages[:2] == ["accumulation", "aggregation"]
        and discarded_blocks >= fault_after_blocks
    )
    ok = (
        summary_identical
        and all(elementwise.values())
        and verification.ok
        and recovery_shape_ok
    )
    report: dict[str, Any] = {
        "ok": ok,
        "fault_after_blocks": fault_after_blocks,
        "crash_exit_code": crash_process.returncode,
        "committed_stages": committed_stages,
        "resumed_from_stage": recovery.get("resumed_from_stage"),
        "rebuilt_stages": rebuilt_stages,
        "discarded_blocks": discarded_blocks,
        "summary_byte_identical": summary_identical,
        "elementwise_identical": elementwise,
        "verification_ok": verification.ok,
        "verification_checks": verification.checks_run,
        "job_count": len(baseline_arrays),
        "baseline_final_summary": str(baseline_summary_path),
        "restarted_final_summary": str(restarted_summary_path),
    }
    (root / "demo_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not ok:
        raise error(
            "E_INCONSISTENT",
            "强制终止恢复结果与完整运行不一致",
            field="demo",
            details=report,
        )
    return report
