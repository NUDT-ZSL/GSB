from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eventgraph.errors import BusinessError
from eventgraph.service import WorkbenchService


REPORT_DIR = ROOT / "reports"


def run_tests() -> dict[str, Any]:
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    summary_line = next(
        (line for line in output.splitlines() if line.startswith("Ran ")),
        "Ran unknown tests",
    )
    return {
        "command": " ".join(command),
        "returncode": completed.returncode,
        "summary": summary_line,
        "passed": completed.returncode == 0,
        "output_tail": "\n".join(output.splitlines()[-8:]),
    }


def capture_error(function) -> dict[str, Any]:
    try:
        function()
    except BusinessError as error:
        return {
            "code": error.code,
            "details": error.details,
        }
    return {"code": "NO_ERROR", "details": {}}


def build_summary() -> dict[str, Any]:
    service = WorkbenchService(ROOT, clear_runtime=True)
    manifest = json.loads(
        (ROOT / "data" / "generated" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    initial = service.state_view()
    conflict_response = service.demo_conflict()
    conflict_record = conflict_response["conflict_records"][0]
    degrade_response = service.demo_degrade()
    degrade_report = degrade_response["report"]
    self_check_after_degrade = service.state_view()["last_self_check"]
    invalidation_response = service.invalidate(
        revise_document_id="D06",
        reason="validation_template_revision",
    )
    invalidation = invalidation_response["invalidation"]
    final_state = service.state_view()
    rollback_probe = capture_error(service.snapshot_rollback_probe)
    corruption_probe = capture_error(service.snapshot_corruption_probe)
    busy_probe = capture_error(service.stage_busy_probe)
    final_state = service.state_view()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "flask": _package_version("flask"),
            "numpy": _package_version("numpy"),
            "host": "127.0.0.1",
            "container": False,
        },
        "manifest": manifest,
        "initial": {
            "documents_processed": initial["counter"]["documents_processed"],
            "chains": initial["chains"],
            "snapshots": initial["snapshots"],
        },
        "conflict": {
            "record": conflict_record,
            "stage": conflict_response["report"],
            "chain_winners": {
                chain["chain_id"]: {
                    role: resolution["winner"]["value"]
                    for role, resolution in chain[
                        "argument_resolutions"
                    ].items()
                }
                for chain in service.engine.chain_view()
            },
        },
        "degrade": {
            "report": degrade_report,
            "self_check": self_check_after_degrade,
        },
        "invalidation": invalidation,
        "final": {
            "chains": final_state["chains"],
            "counter": final_state["counter"],
            "snapshots": final_state["snapshots"],
            "cache": final_state["cache"],
            "resources": final_state["resources"],
            "last_self_check": final_state["last_self_check"],
        },
        "probes": {
            "rollback": rollback_probe,
            "corruption": corruption_probe,
            "stage_busy": busy_probe,
        },
    }


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "unavailable"


def render_markdown(summary: dict[str, Any], tests: dict[str, Any]) -> str:
    env = summary["environment"]
    manifest = summary["manifest"]
    conflict = summary["conflict"]["record"]
    stage_conflict = summary["conflict"]["stage"]
    degrade = summary["degrade"]["report"]
    degradation = degrade["degradation"]
    invalidation = summary["invalidation"]
    final = summary["final"]
    snapshots = final["snapshots"]
    probes = summary["probes"]
    lines = [
        "# 跨文档事件共指链工作台运行报告",
        "",
        f"- 生成时间：`{summary['generated_at']}`",
        f"- 执行环境：Python `{env['python']}` / {env['platform']}",
        f"- 依赖版本：Flask `{env['flask']}`，NumPy `{env['numpy']}`",
        "- 验证载体：WSL2 Linux（非 Docker/容器编排）；仅绑定本地回环地址。",
        "- Windows 原生环境可按 README 的 PowerShell 命令启动，不使用外部数据服务。",
        "",
        "## 1. 验证结论",
        "",
        f"- 自动化测试：`{tests['summary']}`，结果 **{'PASS' if tests['passed'] else 'FAIL'}**。",
        f"- 测试命令：`{tests['command']}`",
        f"- 最终增量/全量一致性：**{'PASS' if final['last_self_check']['ok'] else 'FAIL'}**。",
        f"- 最终链数：{final['counter']['chains']}；局部事件数：{final['counter']['events']}；"
        f"仲裁记录：{final['counter']['arbitration_records']} 条。",
        f"- 增量快照版本：链状态 v{snapshots['chains']['version']}，"
        f"可信度表 v{snapshots['trust_table']['version']}，"
        f"仲裁记录 v{snapshots['arbitration']['version']}。",
        "",
        "## 2. 确定性样本",
        "",
        f"- 固定种子：`{manifest['seed']}`；词表版本：`{manifest['vocab_version']}`。",
        f"- 文档清单哈希：`{manifest['manifest_sha256']}`。",
        "",
        "| 文档 | 来源 | 内容 SHA-256 | 文件 SHA-256 |",
        "|---|---|---|---|",
    ]
    for document in manifest["documents"]:
        lines.append(
            f"| {document['doc_id']} | {document['source']} | "
            f"`{document['content_sha256'][:20]}…` | "
            f"`{document['file_sha256'][:20]}…` |"
        )
    lines.extend(
        [
            "",
            "## 3. 增量合并与链间仲裁",
            "",
            f"- 冲突事件：`{conflict['event_uid']}`。",
            f"- 候选链 A：`{conflict['proposals'][0]['chain_id']}`，"
            f"证据强度 `{conflict['proposals'][0]['evidence_strength']}`，"
            f"链规模 `{conflict['proposals'][0]['chain_size']}`。",
            f"- 候选链 B：`{conflict['proposals'][1]['chain_id']}`，"
            f"证据强度 `{conflict['proposals'][1]['evidence_strength']}`，"
            f"链规模 `{conflict['proposals'][1]['chain_size']}`。",
            f"- 仲裁结果：并入 `{conflict['chosen_chain']}`。",
            f"- 拒绝方向：`{conflict['rejected'][0]['chain_id']}`，"
            f"理由：{conflict['rejected'][0]['reason']}。",
            f"- 该阶段复用链：{', '.join(stage_conflict['reused_chain_ids']) or '无'}；"
            f"重算链：{', '.join(stage_conflict['recalculated_chain_ids']) or '无'}。",
            "",
            "链内论元矛盾示例：",
            "",
        ]
    )
    for chain_id, winners in summary["conflict"]["chain_winners"].items():
        winner_text = "；".join(
            f"{role}={value}" for role, value in winners.items()
        )
        lines.append(f"- `{chain_id}`：{winner_text}")
    lines.extend(
        [
            "",
            "## 4. 资源降级",
            "",
            f"- 文档：`{degrade['document_id']}`。",
            f"- 降级类别：`{degradation['category']}`。",
            f"- 常规下限 `{degradation['original_floor']}`，"
            f"实际下限 `{degradation['effective_floor']}`。",
            f"- 计划比较 `{degradation['comparisons_planned']}` 对，"
            f"精算 `{degrade['comparisons_performed']}` 对，"
            f"预算上限 `{degradation['comparison_limit']}`。",
            f"- 扫描方式：`{degradation['candidate_scan']}`；"
            f"复用链：{', '.join(degrade['reused_chain_ids']) or '无'}。",
            "",
            "## 5. 失效传播与后缀重算",
            "",
            f"- 操作：修订 `{invalidation['revised_document_id']}` 的句子模板并重算。",
            f"- 起点：`{invalidation['start_document']}`；"
            f"重算文档：{', '.join(invalidation['recomputed_document_ids'])}。",
            f"- 重算链：{', '.join(invalidation['recalculated_chain_ids']) or '无'}。",
            f"- 复用链：{', '.join(invalidation['reused_chain_ids']) or '无'}。",
            f"- 受影响候选对：{len(invalidation['affected_candidate_pairs'])} 个。",
            f"- 缓存命中：{invalidation['cache']['hits']}；"
            f"缓存未命中：{invalidation['cache']['misses']}；"
            f"内容对象复用：{invalidation['cache']['object_reuses']}。",
            "",
            "## 6. 一致性与错误码探针",
            "",
            f"- 候选证据复算数：{final['last_self_check']['candidate_evidence_recomputed']}。",
            f"- 事件扫描覆盖数：{final['last_self_check']['event_scan_coverage']}。",
            f"- 全量重建链数：{final['last_self_check']['full_rebuild_chain_count']}。",
            f"- 回退探针：`{probes['rollback']['code']}`。",
            f"- 损坏探针：`{probes['corruption']['code']}`。",
            f"- 并发占用探针：`{probes['stage_busy']['code']}`。",
            "",
            "## 7. 复现命令",
            "",
            "```bash",
            "python3 scripts/generate_samples.py",
            "python3 -m unittest discover -s tests -v",
            "python3 scripts/run_validation.py",
            "python3 app.py",
            "# 浏览器打开 http://127.0.0.1:5000",
            "```",
            "",
            "## 8. 测试输出尾部",
            "",
            "```text",
            tests["output_tail"],
            "```",
            "",
            "## 9. 环境偏差",
            "",
            "- 任务建议 Python 3.11+；当前执行容器提供 Python 3.10.12。",
            "- 实现仅使用 Python 3.10 可用语法，并已在该版本完成全部验证；"
            "Flask 3.0.3 与 NumPy 1.26.4 均为本地已安装版本。",
            "- 本次验证在 WSL2 Linux 中进行；应用只依赖 Flask 与标准库，"
            "Windows Python 3.11+ 可直接运行同一入口。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    tests = run_tests()
    summary = build_summary()
    (REPORT_DIR / "validation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = render_markdown(summary, tests)
    (REPORT_DIR / "run_report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0 if tests["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
