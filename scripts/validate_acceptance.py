from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from relief_workbench.config import load_config
from relief_workbench.engine import WorkbenchEngine
from relief_workbench.sample import generate_sample

VALIDATION_ROOT = PROJECT_ROOT / "artifacts" / "validation"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _wait_for_tile(state_dir: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tiles_dir = state_dir / "tiles"
        if tiles_dir.exists() and any(tiles_dir.glob("*.tile")):
            return
        time.sleep(0.01)
    raise RuntimeError("timed out waiting for a persisted tile")


def _start_render(config_path: Path, state: Path, output: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "relief_workbench",
            "render",
            "--config",
            str(config_path),
            "--state",
            str(state),
            "--output",
            str(output),
            "--tile-delay-ms",
            "80",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _render_cli(config_path: Path, state: Path, output: Path) -> tuple[int, dict]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "relief_workbench",
            "render",
            "--config",
            str(config_path),
            "--state",
            str(state),
            "--output",
            str(output),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    payload = json.loads(completed.stdout)
    return completed.returncode, payload


def _signal_recovery(
    config_path: Path, truth: dict[str, Any], root: Path
) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for mode, kill_signal in (
        ("sigterm", signal.SIGTERM),
        ("sigkill", getattr(signal, "SIGKILL", signal.SIGTERM)),
    ):
        state = root / f"state-{mode}"
        output = root / f"out-{mode}"
        process = _start_render(config_path, state, output)
        try:
            _wait_for_tile(state)
            if mode == "sigterm":
                process.send_signal(kill_signal)
            else:
                process.kill()
            process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        marker_path = state / "interrupted.json"
        marker = (
            json.loads(marker_path.read_text(encoding="utf-8"))
            if marker_path.exists()
            else None
        )
        exit_code, resumed = _render_cli(config_path, state, output)
        records[mode] = {
            "first_exit_code": process.returncode,
            "marker_present": marker is not None,
            "marker": marker,
            "restart_exit_code": exit_code,
            "restart_ok": bool(resumed.get("ok")),
            "restart_reused_tiles": resumed.get("cache", {}).get(
                "reused_tiles"
            ),
            "restart_pixel_exact": (
                resumed.get("comparison", {}).get("max_abs_error") == 0
            ),
            "restart_matches_truth": (
                resumed.get("comparison", {}).get("assembled_sha256")
                == truth["gray_sha256"]
            ),
        }
    return records


def _gui_screenshot(config_path: Path, full_png: Path, path: Path) -> bool:
    try:
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication

        from relief_workbench.gui import ReliefWorkbenchWindow
    except Exception:
        return False
    app = QApplication.instance() or QApplication([])
    window = ReliefWorkbenchWindow(path.parent / "gui-state", full_png.parent)
    if not window.load_config_path(str(config_path)):
        return False
    image = QImage(str(full_png))
    if image.isNull() or window.canvas._pixmap_item is None:
        return False
    window.canvas.select_tile_and_pixel(1, 1)
    window.tabs.setCurrentWidget(window.diagnostic_table)
    window.resize(1440, 900)
    window.show()
    app.processEvents()
    saved = window.grab().save(str(path))
    window.close()
    app.processEvents()
    return bool(saved)


def main() -> int:
    if VALIDATION_ROOT.exists():
        shutil.rmtree(VALIDATION_ROOT)
    VALIDATION_ROOT.mkdir(parents=True)

    sample_root = VALIDATION_ROOT / "sample"
    truth = generate_sample(sample_root)
    config_path = Path(truth["config_path"])
    config = load_config(config_path)

    state = VALIDATION_ROOT / "state"
    output = VALIDATION_ROOT / "out"
    engine = WorkbenchEngine(config, state, output)
    started = time.perf_counter()
    preview = engine.preview_tile(1, 1)
    first_preview_seconds = time.perf_counter() - started

    cached_engine = WorkbenchEngine(config, state, output)
    started = time.perf_counter()
    cached_preview = cached_engine.preview_tile(1, 1)
    cached_preview_seconds = time.perf_counter() - started

    report = cached_engine.render_all()
    assert report["ok"] is True
    assert report["comparison"]["max_abs_error"] == 0
    assert report["comparison"]["assembled_sha256"] == truth["gray_sha256"]
    assert len(list((output / "tiles").glob("*.png"))) == config.grid_rows * config.grid_cols

    all_cached = WorkbenchEngine(
        config, state, VALIDATION_ROOT / "cached-out"
    )
    cached_report = all_cached.render_all()
    assert cached_report["cache"]["rendered_tiles"] == 0
    assert cached_report["cache"]["reused_tiles"] == config.grid_rows * config.grid_cols
    assert cached_report["comparison"]["max_abs_error"] == 0

    serial_config = replace(config, concurrency=1)
    serial_engine = WorkbenchEngine(
        serial_config,
        VALIDATION_ROOT / "serial-state",
        VALIDATION_ROOT / "serial-out",
    )
    serial_report = serial_engine.render_all()
    assert serial_report["comparison"]["max_abs_error"] == 0
    assert (
        serial_report["comparison"]["assembled_sha256"]
        == report["comparison"]["assembled_sha256"]
    )

    version_state = VALIDATION_ROOT / "version-state"
    version_output = VALIDATION_ROOT / "version-out"
    version_a = WorkbenchEngine(config, version_state, version_output)
    report_a = version_a.render_all()
    changed_config = replace(config, seed=config.seed + 1)
    version_b = WorkbenchEngine(changed_config, version_state, version_output)
    version_b.prepare()
    version_invalidated = version_b.store.purged
    report_b = version_b.render_all()
    assert version_invalidated
    assert report_b["cache"]["reused_tiles"] == 0
    assert (
        report_b["comparison"]["assembled_sha256"]
        != report_a["comparison"]["assembled_sha256"]
    )

    digest_state = VALIDATION_ROOT / "digest-state"
    digest_output = VALIDATION_ROOT / "digest-out"
    digest_engine = WorkbenchEngine(config, digest_state, digest_output)
    digest_engine.render_all()
    spec = digest_engine.renderer.spec(2, 2)
    tile_path = digest_engine.store.tile_path(spec)
    damaged = bytearray(tile_path.read_bytes())
    damaged[-1] ^= 0xFF
    tile_path.write_bytes(damaged)
    digest_restart = WorkbenchEngine(config, digest_state, digest_output)
    digest_report = digest_restart.render_all()
    assert digest_report["comparison"]["max_abs_error"] == 0
    assert digest_report["digest_repairs"]

    signals = _signal_recovery(config_path, truth, VALIDATION_ROOT)
    assert signals["sigterm"]["first_exit_code"] == 143
    assert signals["sigterm"]["marker_present"]
    assert signals["sigterm"]["restart_matches_truth"]
    assert signals["sigkill"]["restart_matches_truth"]

    screenshot_path = VALIDATION_ROOT / "gui-diagnostics.png"
    screenshot_ok = _gui_screenshot(
        config_path, Path(report["outputs"]["full_png"]), screenshot_path
    )

    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    summary = {
        "ok": tests.returncode == 0 and screenshot_ok,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "numpy": np.__version__,
        },
        "sample": {
            "version": config.version,
            "truth": truth,
            "preview_tile": {"row": 1, "col": 1},
            "first_preview_seconds": first_preview_seconds,
            "cached_preview_seconds": cached_preview_seconds,
            "preview_cache_hit_on_restart": cached_preview.cache_hit,
        },
        "direct_vs_stitched": report["comparison"],
        "cache": {
            "first_run": report["cache"],
            "fully_cached_run": cached_report["cache"],
        },
        "concurrency_determinism": {
            "concurrency_1_sha256": serial_report["comparison"]["assembled_sha256"],
            "concurrency_4_sha256": report["comparison"]["assembled_sha256"],
            "identical": (
                serial_report["comparison"]["assembled_sha256"]
                == report["comparison"]["assembled_sha256"]
            ),
        },
        "version_invalidation": {
            "purged": version_invalidated,
            "purge_reason": version_b.store.purge_reason,
            "old_sha256": report_a["comparison"]["assembled_sha256"],
            "new_sha256": report_b["comparison"]["assembled_sha256"],
            "reused_tiles": report_b["cache"]["reused_tiles"],
        },
        "digest_repair": {
            "events": digest_report["digest_repairs"],
            "pixel_exact": digest_report["comparison"]["max_abs_error"] == 0,
        },
        "termination_recovery": signals,
        "tests": {
            "returncode": tests.returncode,
            "summary": tests.stdout.strip().splitlines()[-1]
            if tests.stdout.strip()
            else "",
            "stderr": tests.stderr.strip(),
        },
        "gui": {
            "screenshot": str(screenshot_path),
            "screenshot_ok": screenshot_ok,
        },
        "report_json": report["outputs"]["report_json"],
        "full_png": report["outputs"]["full_png"],
    }
    _write_json(VALIDATION_ROOT / "acceptance_summary.json", summary)
    _write_json(
        VALIDATION_ROOT / "stitch_report.json",
        {
            "ok": report["ok"],
            "version": config.version,
            "canvas": {"width": config.width, "height": config.height},
            "grid": {"rows": config.grid_rows, "cols": config.grid_cols},
            "tile_count": report["tile_count"],
            "comparison": report["comparison"],
            "cache": report["cache"],
            "digest_repairs": report["digest_repairs"],
            "engine_report": report["outputs"]["report_json"],
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
