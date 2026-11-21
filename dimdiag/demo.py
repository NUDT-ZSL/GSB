"""Repeatable local demo and validation artifact generation."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .config import load_config
from .data import write_demo_sample
from .engine import DiagnosticEngine
from .errors import BusinessError
from .serialization import atomic_write_bytes, pretty_json_bytes


METHODS = ("pca", "random_projection", "pca_random_projection")


def run_demo(
    workspace_root: str | Path,
    *,
    config_path: str | Path | None = None,
    precision_mode: str | None = None,
    reset: bool = True,
) -> dict[str, Any]:
    root = Path(workspace_root)
    if config_path is None:
        default_config = root / "configs" / "demo.json"
        if not default_config.exists() or not (root / "data" / "demo.csv").exists():
            write_demo_sample(root)
        config_path = default_config
    source_config = load_config(config_path)
    if precision_mode is not None:
        source_config["precision"]["mode"] = precision_mode
    engine = DiagnosticEngine(root, base_dir=Path(config_path).parent)
    reset_info = engine.store.reset("demo_reset") if reset else None
    engine.clear_recent_errors()
    runs: list[dict[str, Any]] = []
    for method in METHODS:
        config = copy.deepcopy(source_config)
        config["method"] = method
        try:
            outcome = engine.run(
                config,
                base_dir=Path(config_path).parent,
                commit=True,
            )
            dry = engine.run(
                config,
                base_dir=Path(config_path).parent,
                commit=False,
            )
            deterministic = (
                outcome.observed.coordinate_checksum()
                == dry.observed.coordinate_checksum()
                and outcome.observed.model_checksum()
                == dry.observed.model_checksum()
            )
            runs.append(
                {
                    "method": method,
                    "ok": True,
                    "snapshot_version": outcome.snapshot["snapshot_version"],
                    "snapshot_checksum": outcome.snapshot["overall_checksum"],
                    "coordinate_checksum": outcome.observed.coordinate_checksum(),
                    "model_checksum": outcome.observed.model_checksum(),
                    "class_consistency": outcome.observed.class_consistency,
                    "variance_explained": outcome.observed.variance_explained,
                    "dominant_error_source": outcome.error_spectrum[
                        "dominant_source"
                    ],
                    "threshold_decisions": [
                        {
                            "stage": decision["stage"],
                            "decision": decision["decision_text"],
                            "value": decision.get("value"),
                            "threshold": decision["threshold"],
                            "strategy": decision["strategy"],
                        }
                        for decision in outcome.threshold_trace["decisions"]
                    ],
                    "selfcheck_passed": outcome.selfcheck["overall_passed"],
                    "selfcheck_categories": [
                        check["category"] for check in outcome.selfcheck["checks"]
                    ],
                    "deterministic_repeat": deterministic,
                    "degraded": outcome.observed.resource_plan["degraded"],
                }
            )
        except BusinessError as error:
            runs.append(
                {
                    "method": method,
                    "ok": False,
                    "error": error.to_dict()["error"],
                }
            )
    report = {
        "schema_version": 1,
        "workspace": str(root),
        "config_path": str(Path(config_path)),
        "precision": source_config["precision"],
        "reset": reset_info,
        "runs": runs,
        "all_passed": all(
            run.get("ok") and run.get("selfcheck_passed") for run in runs
        ),
        "environment": {
            "python": "3.10+",
            "numpy": _numpy_version(),
            "scipy": _scipy_version(),
        },
    }
    artifacts = root / "artifacts"
    atomic_write_bytes(
        artifacts / "demo_report.json",
        pretty_json_bytes(report),
    )
    atomic_write_bytes(
        artifacts / "demo_summary.md",
        _summary_markdown(report),
    )
    return report


def _numpy_version() -> str:
    import numpy as np

    return str(np.__version__)


def _scipy_version() -> str:
    import scipy

    return str(scipy.__version__)


def _summary_markdown(report: dict[str, Any]) -> bytes:
    lines = [
        "# DimDiag 本地演示结果",
        "",
        f"- 精度档：`{report['precision']['mode']}`",
        f"- 配置文件：`{report['config_path']}`",
        f"- 总体验证：**{'PASS' if report['all_passed'] else 'FAIL'}**",
        "",
        "| 方法 | 快照版本 | 坐标校验和 | 类一致性 | 方差解释率 | 主导误差源 | 自检 | 可复现 |",
        "|---|---:|---|---:|---:|---|---|---|",
    ]
    for run in report["runs"]:
        if not run.get("ok"):
            lines.append(
                f"| `{run['method']}` | - | - | - | - | `{run['error']['code']}` | FAIL | - |"
            )
            continue
        variance = (
            "-" if run["variance_explained"] is None else f"{run['variance_explained']:.6f}"
        )
        lines.append(
            "| `{method}` | {version} | `{coord}` | {consistency:.6f} | "
            "{variance} | `{source}` | {selfcheck} | {deterministic} |".format(
                method=run["method"],
                version=run["snapshot_version"],
                coord=run["coordinate_checksum"][:16],
                consistency=run["class_consistency"],
                variance=variance,
                source=run["dominant_error_source"],
                selfcheck="PASS" if run["selfcheck_passed"] else "FAIL",
                deterministic="PASS" if run["deterministic_repeat"] else "FAIL",
            )
        )
    lines.extend(
        [
            "",
            "详细逐级误差、门限留痕与快照校验和见 `artifacts/demo_report.json`。",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")

