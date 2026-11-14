"""Command-line entry point: ``python -m frame_export <spec.json> <outdir>``.

Exit codes:
  0  complete      — every frame was exported
  2  partial       — some frames were exported, some were not
  3  unexportable  — the description is unusable or no frame was exported
  1  usage error   — bad command-line invocation
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Sequence

from . import __version__
from .batch import VERDICT_COMPLETE, VERDICT_PARTIAL, VERDICT_UNEXPORTABLE, build_batch
from .errors import ExportError
from .report import (
    build_empty_report_json,
    build_preview_html,
    build_report,
    build_report_json,
)
from .spec import parse_spec

MANIFEST_NAME = ".frame_export_manifest.json"
REPORT_NAME = "report.json"
PREVIEW_NAME = "preview.html"
FRAMES_DIR = "frames"

EXIT_BY_VERDICT = {
    VERDICT_COMPLETE: 0,
    VERDICT_PARTIAL: 2,
    VERDICT_UNEXPORTABLE: 3,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m frame_export",
        description="按动画描述 JSON 导出过场动画帧序列、核对报告与预览页面。",
    )
    parser.add_argument("spec", help="动画描述 JSON 路径")
    parser.add_argument("outdir", help="导出包输出目录")
    parser.add_argument(
        "--resampling",
        default="lanczos",
        choices=["nearest", "bilinear", "bicubic", "lanczos"],
        help="缩放算法（默认 lanczos）",
    )
    parser.add_argument(
        "--timestamp",
        action="store_true",
        help="在报告中记录生成时间；默认不写入时间，保证连续运行输出一致。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="输出目录非空且不是本工具生成的导出包时仍然写入（会覆盖同名文件）。",
    )
    parser.add_argument("--version", action="version", version=f"frame_export {__version__}")
    return parser


def _prepare_outdir(outdir: Path, force: bool) -> None:
    if outdir.exists() and not outdir.is_dir():
        raise ExportError(
            "output_not_directory",
            f"输出路径已存在且不是目录：{outdir}",
            {"outdir": str(outdir)},
        )
    if not outdir.exists():
        outdir.mkdir(parents=True)
        return
    entries = list(outdir.iterdir())
    if not entries:
        return
    manifest_path = outdir / MANIFEST_NAME
    if not manifest_path.is_file() and not force:
        raise ExportError(
            "output_not_empty",
            f"输出目录非空且未发现本工具的清单：{outdir}。"
            "请换一个空目录，或确认后加 --force。",
            {"outdir": str(outdir)},
        )
    _remove_managed_files(outdir)


def _remove_managed_files(outdir: Path) -> None:
    """Delete only files the previous run created, never user files."""
    manifest_path = outdir / MANIFEST_NAME
    managed: list[str] = []
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            managed = [str(item) for item in data.get("files", [])]
        except (OSError, ValueError):
            managed = [REPORT_NAME, PREVIEW_NAME]
    else:
        managed = [REPORT_NAME, PREVIEW_NAME]
    root = outdir.resolve()
    for relative in managed:
        target = (outdir / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if target.is_file() or target.is_symlink():
            try:
                target.unlink()
            except OSError:
                pass
    frames_dir = outdir / FRAMES_DIR
    if frames_dir.is_dir():
        shutil.rmtree(frames_dir, ignore_errors=True)


def _write_package(outdir: Path, batch, *, timestamp: str | None) -> tuple[dict, str]:
    report = build_report(batch, timestamp=timestamp)
    report_json = build_report_json(batch, timestamp=timestamp)
    preview_html = build_preview_html(batch, report)

    files: list[str] = [REPORT_NAME, PREVIEW_NAME]
    frames_dir = outdir / FRAMES_DIR
    if batch.succeeded:
        frames_dir.mkdir(exist_ok=True)
    for result in batch.frames:
        if not result.succeeded or result.png_bytes is None or result.png_relpath is None:
            continue
        relative = result.png_relpath.split("/", 1)[1]
        assert relative.endswith(".png")
        (frames_dir / relative).write_bytes(result.png_bytes)
        files.append(result.png_relpath)

    (outdir / REPORT_NAME).write_text(report_json, encoding="utf-8", newline="\n")
    (outdir / PREVIEW_NAME).write_text(preview_html, encoding="utf-8", newline="\n")

    manifest = {
        "tool": "frame_export",
        "version": __version__,
        "source": batch.spec.display_path,
        "verdict": report["verdict"],
        "files": files,
    }
    (outdir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return report, preview_html


def _write_failure_package(
    outdir: Path,
    *,
    error: ExportError,
    spec_display: str,
    timestamp: str | None,
) -> None:
    """When the description cannot be parsed, still leave readable artifacts."""
    report_json = build_empty_report_json(
        code=error.code,
        message=error.message,
        details=error.details,
        description=spec_display,
        timestamp=timestamp,
    )
    preview = _failure_preview(error)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / REPORT_NAME).write_text(report_json, encoding="utf-8", newline="\n")
    (outdir / PREVIEW_NAME).write_text(preview, encoding="utf-8", newline="\n")
    manifest = {
        "tool": "frame_export",
        "version": __version__,
        "source": spec_display,
        "verdict": VERDICT_UNEXPORTABLE,
        "files": [REPORT_NAME, PREVIEW_NAME],
    }
    (outdir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _failure_preview(error: ExportError) -> str:
    import html

    detail_rows = "".join(
        f"<li><code>{html.escape(str(key))}</code>: {html.escape(str(value))}</li>"
        for key, value in error.details.items()
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>无法导出 — frame_export</title>
<style>
body {{ background:#14161a; color:#e7ecf3; font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;
       margin:0; padding:48px; }}
.box {{ max-width:760px; margin:0 auto; background:#1e222a; border:1px solid #c4474d;
        border-radius:12px; padding:26px 30px; }}
h1 {{ font-size:20px; margin:0 0 8px; color:#ef8b90; }}
code {{ background:#262c36; padding:2px 6px; border-radius:4px; }}
ul {{ color:#9aa6b8; }}
</style></head>
<body><div class="box">
<h1>无法导出</h1>
<p>{html.escape(error.message)}</p>
<p>错误代码：<code>{html.escape(error.code)}</code></p>
<ul>{detail_rows}</ul>
<p style="color:#9aa6b8">请修正描述 JSON 后重新运行：<code>python -m frame_export &lt;描述.json&gt; &lt;输出目录&gt;</code></p>
</div></body></html>
"""


def run(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    spec_path = Path(args.spec)
    outdir = Path(args.outdir)
    timestamp = None
    if args.timestamp:
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    spec_display = _display_path(spec_path)
    try:
        _prepare_outdir(outdir, args.force)
        spec = parse_spec(spec_path)
        spec_display = spec.display_path
        batch = build_batch(spec, args.resampling)
        report, _ = _write_package(outdir, batch, timestamp=timestamp)
    except ExportError as error:
        if error.code in ("output_not_directory", "output_not_empty"):
            print(f"[frame_export] 错误：{error.message}", file=sys.stderr)
            return 1
        try:
            _write_failure_package(
                outdir,
                error=error,
                spec_display=spec_display,
                timestamp=timestamp,
            )
        except OSError as write_error:
            print(f"[frame_export] 错误：{error.message}", file=sys.stderr)
            print(f"[frame_export] 且无法写入导出包：{write_error}", file=sys.stderr)
            return 3
        print(f"[frame_export] 无法导出：{error.message}", file=sys.stderr)
        print(f"[frame_export] 已写出失败报告：{outdir / REPORT_NAME}", file=sys.stderr)
        return 3
    except Exception as error:  # noqa: BLE001 - never leave a blank result
        traceback.print_exc()
        message = f"导出过程发生未预期错误：{type(error).__name__}: {error}"
        try:
            _write_failure_package(
                outdir,
                error=ExportError("internal_error", message),
                spec_display=spec_display,
                timestamp=timestamp,
            )
        except OSError:
            pass
        print(f"[frame_export] {message}", file=sys.stderr)
        return 3

    summary = report["summary"]
    print(
        f"[frame_export] 结论：{report['verdict_label']} | "
        f"导出 {summary['frames_exported']}/{summary['frames_total']} 帧 | "
        f"累计停留 {report['summary']['duration_ms_exported']} ms"
    )
    print(f"[frame_export] 报告：{outdir / REPORT_NAME}")
    print(f"[frame_export] 预览：{outdir / PREVIEW_NAME}")
    if batch.failed:
        for frame in batch.failed:
            print(
                f"[frame_export] 未完成：位置 {frame.ordinal} 序号 {frame.spec.index} "
                f"{frame.status} — {frame.error_message}",
                file=sys.stderr,
            )
    return EXIT_BY_VERDICT[batch.verdict]


def _display_path(path: Path) -> str:
    try:
        text = os.path.relpath(os.path.abspath(path), os.getcwd())
    except ValueError:
        text = str(path)
    return Path(text).as_posix()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except SystemExit as exc:  # argparse
        return int(exc.code) if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
