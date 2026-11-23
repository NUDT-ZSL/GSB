from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .report import load_report
from .workspace import Workspace


STAGE_LABELS = {
    "validate_plan": "校验/计划",
    "block_stats": "分块统计",
    "normalize": "归一化",
    "output": "写回",
}


def _short(value: Any, length: int = 16) -> str:
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= length else text[:length] + "…"


def _load(root: str | Path | Workspace) -> tuple[Workspace, dict[str, Any]]:
    workspace = root if isinstance(root, Workspace) else Workspace(root)
    workspace.initialize()
    path = workspace.paths.latest_report
    if not path.exists():
        raise BusinessError(
            "E_INCONSISTENT",
            "no report exists; run a job batch first",
            field=str(path),
            stage="workbench",
        )
    return workspace, load_report(path)


def print_summary(root: str | Path | Workspace) -> dict[str, Any]:
    workspace, report = _load(root)
    invalidation = report.get("invalidation", {})
    consistency = report.get("consistency", {})
    print("Transformer 算子增量诊断工作台")
    print("=" * 78)
    print(
        f"status={report.get('status')}  mode={report.get('mode')}  "
        f"run={report.get('run_id')}"
    )
    print(
        f"affected_jobs={len(invalidation.get('affected_jobs', []))}  "
        f"recomputed_nodes={len(invalidation.get('recomputed_nodes', []))}  "
        f"reused_nodes={len(invalidation.get('reused_nodes', []))}"
    )
    print(
        f"byte_identical={consistency.get('all_byte_identical')}  "
        f"errors={len(report.get('errors', []))}  "
        f"html={workspace.paths.html_report}"
    )
    for job_id, job in sorted(report.get("jobs", {}).items()):
        nodes = job.get("nodes", {})
        chain = "  ".join(
            f"{STAGE_LABELS[stage]}:{nodes.get(stage, {}).get('status', 'missing')}"
            for stage in STAGE_LABELS
        )
        print(f"  {job_id:<24} {str(job.get('status', '?')):<8} {chain}")
    return report


def print_graph(report: dict[str, Any]) -> None:
    for job_id, job in sorted(report.get("jobs", {}).items()):
        print(f"\n[{job_id}] {job.get('spec', {}).get('op_kind')}  status={job.get('status')}")
        nodes = job.get("nodes", {})
        for index, (stage, label) in enumerate(STAGE_LABELS.items()):
            node = nodes.get(stage, {})
            marker = "  " if index == 0 else "→ "
            status = node.get("status", "missing")
            extra = f" {node.get('error_code')}" if node.get("error_code") else ""
            print(
                f"  {marker}{label:<10} [{status:<10}] "
                f"v={node.get('artifact_version', '—')} "
                f"hash={_short(node.get('content_hash'))}{extra}"
            )


def print_job(report: dict[str, Any], job_id: str) -> None:
    job = report.get("jobs", {}).get(job_id)
    if job is None:
        print(f"job not found: {job_id}")
        return
    print(json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True))


def print_errors(report: dict[str, Any]) -> None:
    for error in report.get("errors", []):
        print(
            f"{error.get('code')} stage={error.get('stage')} "
            f"job={error.get('job_id')} field={error.get('field')}: {error.get('message')}"
        )
    chains = report.get("degradation_chains", [])
    if chains:
        print("\n降级链路:")
        for chain in chains:
            print(
                f"  {chain.get('job_id')} {chain.get('upstream_stage')}->{chain.get('stage')} "
                f"category={chain.get('category')} upstream={chain.get('upstream_code')}"
            )
    recent = report.get("recent_error_codes", [])
    if recent:
        print("\n最近业务错误码:")
        for item in recent:
            print(
                f"  {item.get('at')} {item.get('code')} "
                f"{item.get('stage')} {item.get('job_id') or 'global'}"
            )


def print_snapshots(report: dict[str, Any]) -> None:
    for item in report.get("snapshot_inventory", []):
        print(
            f"{item.get('kind'):<10} {item.get('key'):<24} "
            f"v={item.get('version')} checksum={_short(item.get('content_checksum'))} "
            f"replacement={item.get('replacement')}"
        )


def run_interactive(root: str | Path | Workspace, *, force: bool = False) -> None:
    workspace, report = _load(root)
    print_summary(root)
    interactive = force or sys.stdin.isatty()
    if not interactive:
        print(f"\n静态 HTML 工作台: {workspace.paths.html_report}")
        return
    print(
        "\n命令: graph | jobs | job <id> | errors | snapshots | verify | "
        "html | reload | help | quit"
    )
    while True:
        try:
            command = input("workbench> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not command:
            continue
        parts = command.split(maxsplit=1)
        verb = parts[0].lower()
        argument = parts[1] if len(parts) > 1 else ""
        try:
            if verb in {"q", "quit", "exit"}:
                break
            if verb in {"h", "help"}:
                print(
                    "graph: 计算图 | jobs: job 列表 | job <id>: 详情 | errors: 错误/降级 | "
                    "snapshots: 快照 | verify: 运行自检 | html: HTML 路径 | reload: 重载"
                )
            elif verb == "graph":
                print_graph(report)
            elif verb == "jobs":
                for job_id, job in sorted(report.get("jobs", {}).items()):
                    print(f"{job_id:<24} {job.get('status')} affected={job.get('affected')}")
            elif verb == "job":
                print_job(report, argument)
            elif verb == "errors":
                print_errors(report)
            elif verb == "snapshots":
                print_snapshots(report)
            elif verb == "html":
                print(workspace.paths.html_report)
            elif verb == "reload":
                workspace, report = _load(root)
                print("report reloaded")
            elif verb == "verify":
                from .selfcheck import run_selfcheck

                result = run_selfcheck(workspace)
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"unknown command: {verb}; use help")
        except BusinessError as exc:
            print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))

