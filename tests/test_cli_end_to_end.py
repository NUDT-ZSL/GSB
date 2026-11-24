"""End-to-end CLI behaviour on the fixed-seed samples."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from terrain_engine import cli


def run_cli(root: Path, *arguments: str) -> tuple[int, str]:
    from io import StringIO
    import contextlib

    buffer = StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(list(arguments) + ["--root", str(root)])
    return code, buffer.getvalue()


def test_render_and_publish(sample_dir, workspace):
    code, output = run_cli(
        workspace, "render", str(sample_dir / "task.json"))
    assert code == 0, output
    payload = json.loads(output)
    report_path = Path(payload["report_path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))["report"]
    assert report["publication"]["width"] == 96
    assert (workspace / "outputs" / "image.pgm").exists()
    assert (workspace / "outputs" / "depth.f32").exists()


def test_second_render_reuses_all_tiles(sample_dir, workspace):
    run_cli(workspace, "render", str(sample_dir / "task.json"))
    code, output = run_cli(
        workspace, "render", str(sample_dir / "task.json"))
    assert code == 0
    report_path = Path(json.loads(output)["report_path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))["report"]
    assert len(report["reused_tiles"]) == 9


def test_preview_then_full_render(sample_dir, workspace):
    code, output = run_cli(
        workspace, "preview", str(sample_dir / "task.json"),
        "r0001_c0001")
    assert code == 0
    assert json.loads(output)["result"]["status"] == "rendered"
    run_cli(workspace, "render", str(sample_dir / "task.json"))
    code, output = run_cli(
        workspace, "preview", str(sample_dir / "task.json"),
        "r0001_c0001")
    assert json.loads(output)["result"]["status"] == "reused"


def test_injection_cli_continues_legal_tiles(sample_dir, workspace):
    # The divisibility injection is a task-level failure exercised elsewhere;
    # here only per-tile injections are enabled.
    import json as _json
    injections = _json.loads(
        (sample_dir / "injections.json").read_text(encoding="utf-8"))
    filtered = sample_dir / "tile_injections.json"
    filtered.write_text(json.dumps({
        key: value for key, value in injections.items()
        if key != "tile_not_divisible"}), encoding="utf-8")
    code, _ = run_cli(
        workspace, "render", str(sample_dir / "task.json"),
        "--injections", str(filtered),
        "--no-publish")
    # Two injected tiles fail but the command still reports success for the
    # run as a whole; failures are structured inside the report.
    assert code == 0


def test_invalid_task_returns_error_envelope(sample_dir, workspace):
    bad = sample_dir / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    code, output = run_cli(workspace, "render", str(bad))
    assert code == 2
    payload = json.loads(output)
    assert payload["error"]["code"] == "E_PARSE"


def test_append_cli_rerenders_only_invalidated_tiles(sample_dir, workspace):
    run_cli(workspace, "render", str(sample_dir / "task.json"))
    code, output = run_cli(
        workspace, "append", str(sample_dir / "task.json"),
        "--edits", str(sample_dir / "edits.json"), "--batch", "1")
    assert code == 0, output
    report = json.loads(
        Path(json.loads(output)["report_path"]).read_text(
            encoding="utf-8"))["report"]
    assert report["rerender_count"] >= 1
    assert report["reuse_count"] + report["rerender_count"] == 9

    # Second append advances the persisted cursor and only touches newly
    # invalidated tiles.
    code, output = run_cli(
        workspace, "append", str(sample_dir / "task.json"),
        "--edits", str(sample_dir / "edits.json"), "--batch", "3")
    assert code == 0
    report = json.loads(
        Path(json.loads(output)["report_path"]).read_text(
            encoding="utf-8"))["report"]
    assert report["rerender_count"] >= 1


def test_append_rejects_stale_cursor(sample_dir, workspace):
    run_cli(workspace, "render", str(sample_dir / "task.json"))
    run_cli(workspace, "append", str(sample_dir / "task.json"),
            "--edits", str(sample_dir / "edits.json"), "--batch", "2")
    code, output = run_cli(
        workspace, "append", str(sample_dir / "task.json"),
        "--edits", str(sample_dir / "edits.json"), "--batch", "1")
    assert code == 2
    assert json.loads(output)["error"]["code"] == "E_SCHEMA"

