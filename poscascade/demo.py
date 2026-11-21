from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .canonical import atomic_write_text, canonical_json
from .diagnostics import collect_error_scenarios
from .samples import generate_samples
from .state import build_state


def _safe_remove(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if root.resolve() not in resolved.parents and resolved != root.resolve():
        raise ValueError(f"refusing to remove path outside demo root: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_cli(
    root: Path,
    args: list[str],
    *,
    expected_codes: set[int],
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root) + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    process = subprocess.run(
        [sys.executable, "-m", "poscascade.cli", *args],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode not in expected_codes:
        raise RuntimeError(
            "unexpected CLI return code "
            f"{process.returncode} for {' '.join(args)}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return {
        "returncode": process.returncode,
        "stdout_tail": process.stdout[-1600:],
        "stderr_tail": process.stderr[-1600:],
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_demo(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    demo_root = root / "runs" / "demo"
    data_dir = root / "data"
    _safe_remove(demo_root, root / "runs")
    demo_root.mkdir(parents=True, exist_ok=True)
    generate_samples(data_dir)

    common = [
        "--input",
        str(data_dir / "lines.jsonl"),
        "--config",
        str(data_dir / "config_default.json"),
    ]

    full_dir = demo_root / "full"
    full_cmd = [
        "run",
        *common,
        "--dict",
        str(data_dir / "dict_v1.json"),
        "--labels",
        str(data_dir / "labels_v1.json"),
        "--store",
        str(full_dir / "snapshots.sqlite3"),
        "--out",
        str(full_dir / "out"),
    ]
    full_process = _run_cli(root, full_cmd, expected_codes={0})

    recovery_dir = demo_root / "recovery"
    recovery_args = [
        "run",
        *common,
        "--dict",
        str(data_dir / "dict_v1.json"),
        "--labels",
        str(data_dir / "labels_v1.json"),
        "--store",
        str(recovery_dir / "snapshots.sqlite3"),
        "--out",
        str(recovery_dir / "out"),
    ]
    crash_process = _run_cli(
        root,
        [*recovery_args, "--crash-after", "segment"],
        expected_codes={137},
    )
    resume_process = _run_cli(root, recovery_args, expected_codes={0})

    full_annotations = full_dir / "out" / "annotations.jsonl"
    recovered_annotations = recovery_dir / "out" / "annotations.jsonl"
    full_summary = full_dir / "out" / "summary.json"
    recovered_summary = recovery_dir / "out" / "summary.json"
    recovery_equal = (
        full_annotations.read_bytes() == recovered_annotations.read_bytes()
        and full_summary.read_bytes() == recovered_summary.read_bytes()
    )
    recovery_meta = _load_json(recovery_dir / "out" / "run_meta.json")

    upgrade_dir = demo_root / "upgrade"
    v1_args = [
        "run",
        *common,
        "--dict",
        str(data_dir / "dict_v1.json"),
        "--labels",
        str(data_dir / "labels_v1.json"),
        "--store",
        str(upgrade_dir / "snapshots.sqlite3"),
        "--out",
        str(upgrade_dir / "out"),
    ]
    _run_cli(root, v1_args, expected_codes={0})
    v2_upgrade_args = [
        "run",
        *common,
        "--dict",
        str(data_dir / "dict_v2.json"),
        "--labels",
        str(data_dir / "labels_v2.json"),
        "--store",
        str(upgrade_dir / "snapshots.sqlite3"),
        "--out",
        str(upgrade_dir / "out"),
    ]
    _run_cli(root, v2_upgrade_args, expected_codes={0})

    v2_fresh_dir = demo_root / "v2_fresh"
    _run_cli(
        root,
        [
            "run",
            *common,
            "--dict",
            str(data_dir / "dict_v2.json"),
            "--labels",
            str(data_dir / "labels_v2.json"),
            "--store",
            str(v2_fresh_dir / "snapshots.sqlite3"),
            "--out",
            str(v2_fresh_dir / "out"),
        ],
        expected_codes={0},
    )
    upgrade_equal = (
        (upgrade_dir / "out" / "annotations.jsonl").read_bytes()
        == (v2_fresh_dir / "out" / "annotations.jsonl").read_bytes()
        and (upgrade_dir / "out" / "summary.json").read_bytes()
        == (v2_fresh_dir / "out" / "summary.json").read_bytes()
    )
    upgrade_meta = _load_json(upgrade_dir / "out" / "run_meta.json")
    upgrade_recovery = upgrade_meta.get("recovery_report", {})
    upgrade_state = build_state(
        store_path=upgrade_dir / "snapshots.sqlite3",
        output_dir=upgrade_dir / "out",
        data_dir=data_dir,
    )

    low_dir = demo_root / "low_budget"
    _run_cli(
        root,
        [
            "run",
            "--input",
            str(data_dir / "lines.jsonl"),
            "--dict",
            str(data_dir / "dict_v1.json"),
            "--labels",
            str(data_dir / "labels_v1.json"),
            "--config",
            str(data_dir / "config_low_budget.json"),
            "--store",
            str(low_dir / "snapshots.sqlite3"),
            "--out",
            str(low_dir / "out"),
        ],
        expected_codes={0},
    )
    low_meta = _load_json(low_dir / "out" / "run_meta.json")

    strict_dir = demo_root / "strict_limit"
    _run_cli(
        root,
        [
            "run",
            "--input",
            str(data_dir / "lines.jsonl"),
            "--dict",
            str(data_dir / "dict_v1.json"),
            "--labels",
            str(data_dir / "labels_v1.json"),
            "--config",
            str(data_dir / "config_strict.json"),
            "--store",
            str(strict_dir / "snapshots.sqlite3"),
            "--out",
            str(strict_dir / "out"),
        ],
        expected_codes={2},
    )
    strict_errors = _load_json(strict_dir / "out" / "errors.json")

    error_chain = collect_error_scenarios(data_dir)
    errors_dir = demo_root / "error_chain"
    errors_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        errors_dir / "report.json",
        canonical_json(error_chain) + "\n",
    )

    degradation_categories = sorted(
        {item["category"] for item in low_meta.get("degradations", [])}
    )
    recovery_report = recovery_meta.get("recovery_report", {})
    affected = upgrade_meta.get("affected", {})
    recomputed = upgrade_meta.get("recomputed", {})
    reused = upgrade_meta.get("reused", {})

    checks = {
        "full_run_ok": full_process["returncode"] == 0,
        "hard_kill_returncode_137": crash_process["returncode"] == 137,
        "resume_ok": resume_process["returncode"] == 0,
        "recovery_byte_identical": recovery_equal,
        "upgrade_byte_identical_to_fresh_v2": upgrade_equal,
        "low_budget_has_degradation": bool(degradation_categories),
        "strict_limit_returns_E_RESOURCE_LIMIT": any(
            item.get("code") == "E_RESOURCE_LIMIT" for item in strict_errors
        ),
        "error_chain_all_expected": bool(error_chain["ok"]),
        "all_selfchecks_ok": all(
            meta.get("selfcheck", {}).get("code") == "OK"
            for meta in (recovery_meta, upgrade_meta, low_meta)
        ),
    }

    report = {
        "ok": all(checks.values()),
        "checks": checks,
        "seed": _load_json(data_dir / "manifest.json")["seed"],
        "termination_method": "os._exit(137) after committed segment snapshot",
        "full_run": {
            "annotations_sha256": _sha256(full_annotations),
            "summary_sha256": _sha256(full_summary),
        },
        "recovery": {
            "crash_returncode": crash_process["returncode"],
            "last_committed_stage_at_resume": recovery_report.get(
                "last_committed_stage_at_start"
            ),
            "resume_from_stage": recovery_report.get("resume_from_stage"),
            "annotations_sha256": _sha256(recovered_annotations),
            "summary_sha256": _sha256(recovered_summary),
            "byte_identical": recovery_equal,
        },
        "upgrade": {
            "reasons": upgrade_recovery.get("reasons"),
            "affected_counts": {stage: len(items) for stage, items in affected.items()},
            "recomputed_counts": {
                stage: len(items) for stage, items in recomputed.items()
            },
            "reused_counts": {stage: len(items) for stage, items in reused.items()},
            "dict_version": upgrade_meta.get("generation", {}).get("dict_version"),
            "labels_version": upgrade_meta.get("generation", {}).get("labels_version"),
            "generations": upgrade_state.get("generations", []),
            "byte_identical_to_fresh_v2": upgrade_equal,
            "annotations_sha256": _sha256(
                upgrade_dir / "out" / "annotations.jsonl"
            ),
            "summary_sha256": _sha256(upgrade_dir / "out" / "summary.json"),
        },
        "low_budget": {
            "degradation_count": len(low_meta.get("degradations", [])),
            "categories": degradation_categories,
            "selfcheck": low_meta.get("selfcheck", {}).get("code"),
        },
        "strict_limit": {
            "errors": strict_errors,
        },
        "error_chain": error_chain,
        "files": {
            "full_output": str(full_dir / "out"),
            "recovery_output": str(recovery_dir / "out"),
            "upgrade_output": str(upgrade_dir / "out"),
            "v2_fresh_output": str(v2_fresh_dir / "out"),
            "low_budget_output": str(low_dir / "out"),
            "strict_limit_output": str(strict_dir / "out"),
            "error_chain_report": str(errors_dir / "report.json"),
        },
    }
    atomic_write_text(
        demo_root / "demo_report.json",
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(demo_root / "demo_report.md", _render_markdown(report))
    return report


def _render_markdown(report: dict[str, Any]) -> str:
    checks = report["checks"]
    lines = [
        "# 本地级联演示报告",
        "",
        f"- 总体结果：{'通过' if report['ok'] else '失败'}",
        f"- 固定种子：`{report['seed']}`",
        f"- 强制终止：{report['termination_method']}",
        "",
        "## 验收检查",
        "",
    ]
    for name, passed in checks.items():
        lines.append(f"- `{name}`: {'PASS' if passed else 'FAIL'}")
    lines.extend(
        [
            "",
            "## 恢复对比",
            "",
            f"- 崩溃进程返回码：`{report['recovery']['crash_returncode']}`",
            f"- 恢复起点：`{report['recovery']['last_committed_stage_at_resume']}`",
            f"- 继续阶段：`{report['recovery']['resume_from_stage']}`",
            f"- 标注 SHA-256：`{report['recovery']['annotations_sha256']}`",
            f"- 汇总 SHA-256：`{report['recovery']['summary_sha256']}`",
            "",
            "## 版本替换",
            "",
            f"- 受影响：`{report['upgrade']['affected_counts']}`",
            f"- 重算：`{report['upgrade']['recomputed_counts']}`",
            f"- 复用：`{report['upgrade']['reused_counts']}`",
            f"- 与 v2 全新运行逐字节一致：`{report['upgrade']['byte_identical_to_fresh_v2']}`",
            "",
            "## 资源降级",
            "",
            f"- 降级条数：`{report['low_budget']['degradation_count']}`",
            f"- 降级类别：`{report['low_budget']['categories']}`",
            "",
            "## 异常链路",
            "",
        ]
    )
    for item in report["error_chain"]["scenarios"]:
        lines.append(
            f"- `{item['scenario']}` → `{item['actual_code']}`"
            + (
                f"（doc_id={item['doc_id']}, line_no={item['line_no']}）"
                if item["doc_id"] is not None
                else ""
            )
        )
    lines.append("")
    return "\n".join(lines)
