#!/usr/bin/env python3
"""Run local acceptance checks and write a machine-readable result."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from surface_stitch.generator import generate_sample  # noqa: E402
from surface_stitch.storage import archive_changed_version  # noqa: E402


def run_command(args: List[str], expect_code: int = 0) -> Dict[str, Any]:
    process = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload: Any = None
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        payload = None
    result = {
        "args": args,
        "returncode": process.returncode,
        "expected_returncode": expect_code,
        "stdout": payload if payload is not None else process.stdout[-4000:],
        "stderr_tail": process.stderr[-4000:],
        "ok": process.returncode == expect_code,
    }
    if process.returncode != expect_code:
        raise AssertionError(json.dumps(result, ensure_ascii=False, indent=2)[:8000])
    return result


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def png_pixels(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path), dtype=np.uint8)


def main() -> int:
    sample_dir = ROOT / "samples" / "generated"
    acceptance_dir = ROOT / "artifacts" / "acceptance"
    if acceptance_dir.exists():
        shutil.rmtree(acceptance_dir)
    acceptance_dir.mkdir(parents=True)

    generation = generate_sample(sample_dir)
    python = sys.executable
    module = [python, "-m", "surface_stitch"]

    serial_out = acceptance_dir / "serial-output"
    serial_state = acceptance_dir / "serial-state"
    parallel_out = acceptance_dir / "parallel-output"
    parallel_state = acceptance_dir / "parallel-state"
    interrupt_out = acceptance_dir / "interrupt-output"
    interrupt_state = acceptance_dir / "interrupt-state"
    resume_out = acceptance_dir / "resume-output"
    tamper_out = acceptance_dir / "tamper-output"

    commands: List[Dict[str, Any]] = []
    commands.append(run_command(module + [
        "run", "--input", str(sample_dir),
        "--output-dir", str(serial_out), "--state-dir", str(serial_state),
        "--workers", "1",
    ]))
    commands.append(run_command(module + [
        "run", "--input", str(sample_dir),
        "--output-dir", str(parallel_out), "--state-dir", str(parallel_state),
        "--workers", "3",
    ]))
    commands.append(run_command(module + [
        "run", "--input", str(sample_dir),
        "--output-dir", str(interrupt_out), "--state-dir", str(interrupt_state),
        "--workers", "1", "--interrupt-after", "1",
    ], expect_code=130))

    marker = read_json(interrupt_state / "INTERRUPTED.json")
    interrupted_manifests = sorted(
        path.name for path in (interrupt_state / "tiles").glob("*.manifest.json")
    )

    commands.append(run_command(module + [
        "run", "--input", str(sample_dir),
        "--output-dir", str(acceptance_dir / "resume-output-unused"),
        "--state-dir", str(interrupt_state), "--workers", "2",
    ]))
    # The CLI intentionally does not replace same-version reports; use a fresh
    # output directory for the post-tamper verification report.
    tile_02_manifest = interrupt_state / "tiles" / "tile_02.manifest.json"
    tampered = read_json(tile_02_manifest)
    tampered["array_digest"] = "e" * 64
    tile_02_manifest.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
    commands.append(run_command(module + [
        "run", "--input", str(sample_dir),
        "--output-dir", str(tamper_out), "--state-dir", str(interrupt_state),
        "--workers", "1",
    ]))

    invalid_sample = acceptance_dir / "invalid-sample"
    shutil.copytree(sample_dir, invalid_sample)
    project_path = invalid_sample / "project.json"
    invalid_project = read_json(project_path)
    invalid_project["canvas"]["width"] = -320
    project_path.write_text(json.dumps(invalid_project), encoding="utf-8")
    invalid_out = acceptance_dir / "invalid-output"
    invalid_command = run_command(module + [
        "run", "--input", str(invalid_sample),
        "--output-dir", str(invalid_out),
        "--state-dir", str(acceptance_dir / "invalid-state"),
    ], expect_code=2)

    serial_report = read_json(serial_out / "registration_verification_report.json")
    parallel_report = read_json(parallel_out / "registration_verification_report.json")
    tamper_report = read_json(tamper_out / "registration_verification_report.json")

    serial_pixels = png_pixels(serial_out / "mosaic.png")
    parallel_pixels = png_pixels(parallel_out / "mosaic.png")
    pixel_identical = bool(np.array_equal(serial_pixels, parallel_pixels))

    tile02 = next(tile for tile in tamper_report["tiles"] if tile["tile_id"] == "tile_02")
    digest_recovered = "E_DIGEST" in tile02["business_codes"] and tile02["persistence_status"] == "processed"

    archive_state = acceptance_dir / "archive-state"
    archived = archive_changed_version(archive_state, "version-a")
    (archive_state / "tiles").mkdir(exist_ok=True)
    (archive_state / "tiles" / "old.manifest.json").write_text("{}", encoding="utf-8")
    archived_next = archive_changed_version(archive_state, "version-b")

    pytest_process = subprocess.run(
        [python, "-m", "pytest", "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    checks = {
        "sample_generated": True,
        "registration_integer_error_zero": serial_report["registration_summary"]["max_integer_error_px"] == 0,
        "registration_subpixel_within_tolerance": (
            serial_report["registration_summary"]["max_subpixel_error_px"]
            <= serial_report["registration_summary"]["subpixel_tolerance_px"]
        ),
        "conflict_arbitration_count_12": serial_report["conflict_count"] == 12,
        "all_conflicts_located": all(
            item.get("v1", {}).get("source")
            and item.get("v2", {}).get("source")
            and item.get("tile_id")
            and len(item.get("pixel_xy", [])) == 2
            for item in serial_report["conflicts"]
        ),
        "serial_row_reference_matches": serial_report["fusion_consistency"][
            "weighted_and_row_serial_pixels_identical"
        ] is True,
        "worker_counts_pixel_identical": pixel_identical,
        "interrupt_exit_130": marker["exit_code"] == 130,
        "interrupt_exactly_one_manifest": interrupted_manifests == ["tile_00.manifest.json"],
        "digest_tamper_recovered": digest_recovered,
        "invalid_input_returns_E_code": (
            invalid_command["stdout"].get("error", {}).get("code", "").startswith("E_")
        ),
        "invalid_input_has_no_mosaic": not (invalid_out / "mosaic.png").exists(),
        "version_archive_created": archived is None and archived_next is not None,
        "pytest_returncode_zero": pytest_process.returncode == 0,
    }

    result = {
        "ok": all(value for key, value in checks.items() if key != "version_archive_created")
        and checks["version_archive_created"],
        "checks": checks,
        "versions": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "pillow": Image.__version__,
        },
        "registration_summary": serial_report["registration_summary"],
        "radiometry_summary": serial_report["radiometry_summary"],
        "fusion_consistency": serial_report["fusion_consistency"],
        "parallel_fusion_digest": parallel_report["fusion_consistency"]["fused_sha256"],
        "conflict_count": serial_report["conflict_count"],
        "tombstone_count": len(serial_report["mask_tombstones"]),
        "same_id_revisions": serial_report["same_id_revisions"],
        "resource_usage_serial": serial_report["resource_usage"],
        "resource_usage_parallel": parallel_report["resource_usage"],
        "interrupt": {
            "marker": marker,
            "manifests_at_interrupt": interrupted_manifests,
        },
        "digest_recovery_tile": {
            "tile_id": "tile_02",
            "business_codes": tile02["business_codes"],
            "status": tile02["persistence_status"],
        },
        "invalid_input_code": invalid_command["stdout"]["error"]["code"],
        "pytest_tail": (pytest_process.stdout + pytest_process.stderr)[-2000:],
        "commands": [
            {
                "args": " ".join(item["args"][:4]) + " ...",
                "returncode": item["returncode"],
                "ok": item["ok"],
            }
            for item in commands
        ],
    }
    (ROOT / "artifacts" / "acceptance_report.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    markdown = [
        "# 实际验收记录",
        "",
        f"- 总体结果：{'通过' if result['ok'] else '未通过'}",
        f"- Python：{result['versions']['python']}；numpy：{result['versions']['numpy']}；Pillow：{result['versions']['pillow']}",
        f"- 整数偏移最大误差：{serial_report['registration_summary']['max_integer_error_px']} px",
        f"- 亚像素最大误差：{serial_report['registration_summary']['max_subpixel_error_px']:.6f} px；容差：{serial_report['registration_summary']['subpixel_tolerance_px']} px",
        f"- 掩码冲突：{serial_report['conflict_count']} 条；墓碑：{len(serial_report['mask_tombstones'])} 条",
        f"- 亮度/对比度校正前后重叠区 MAE：{serial_report['radiometry_summary']['mean_overlap_mae_before']:.4f} -> {serial_report['radiometry_summary']['mean_overlap_mae_after']:.4f}",
        f"- 串行行序参考与加权融合逐像素一致：{checks['serial_row_reference_matches']}",
        f"- workers=1 与 workers=3 PNG 逐像素一致：{pixel_identical}",
        f"- 中断退出码：{marker['exit_code']}；中断时清单：{', '.join(interrupted_manifests)}",
        f"- E_DIGEST 篡改恢复：{digest_recovered}",
        f"- 非法输入业务码：{result['invalid_input_code']}，且未生成拼接图",
        f"- pytest 退出码：{pytest_process.returncode}",
        "",
        "## 检查项",
        "",
    ]
    markdown.extend(f"- `{name}`：{'PASS' if ok else 'FAIL'}" for name, ok in checks.items())
    (ROOT / "docs" / "VALIDATION.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")

    print(json.dumps({"ok": result["ok"], "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
