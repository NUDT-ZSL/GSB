"""一次性批处理流水线与命令行入口。

用法::

    python -m cover_preview.pipeline --manifest samples/manifest.json --output-dir output
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _ensure_utf8_console() -> None:
    """Windows 控制台默认 GBK 代码页时切换为 UTF-8，避免中文摘要乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

from .compose import preview_filename, probe_candidate, render_preview
from .fonts import FontError, resolve_font
from .layout import layout_copy
from .models import SkippedEntry, VideoEntry, load_manifest

RESULTS_JSON = "results.json"
PREVIEW_GLOB = "*_preview.png"


def _clean_previews(preview_dir: Path) -> None:
    """清理上一轮固定命名的预览图，保证重复运行不产生副本/残留。"""
    for stale in preview_dir.glob(PREVIEW_GLOB):
        stale.unlink()


def process_entry(
    entry: VideoEntry, preview_dir: Path, font_path: Path
) -> dict:
    """处理单个视频，写入预览图并返回结果记录。"""
    decoded = []
    unavailable: list[dict] = []
    for candidate in entry.candidates:
        img, reason = probe_candidate(candidate)
        decoded.append((img, reason))
        if not candidate.available:
            unavailable.append({"path": candidate.raw_path, "reason": candidate.reason})

    layout = layout_copy(entry, font_path)
    preview = render_preview(entry, decoded, layout, font_path)

    filename = preview_filename(entry.video_id)
    preview.save(preview_dir / filename, format="PNG")

    return {
        "video_id": entry.video_id,
        "layout_mode": layout.mode,
        "final_font_size": layout.font_size,
        "unavailable_candidates": unavailable,
        "all_candidates_unavailable": len(unavailable) == len(entry.candidates),
        "candidate_count": len(entry.candidates),
        "preview_file": filename,
    }


def run_batch(
    manifest_path: str | Path,
    output_dir: str | Path,
    font: str | Path | None = None,
) -> dict:
    """执行整批处理，返回摘要字典。"""
    manifest_path = Path(manifest_path).resolve()
    output_dir = Path(output_dir).resolve()
    preview_dir = output_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    _clean_previews(preview_dir)

    entries, skipped, manifest_font = load_manifest(manifest_path)
    font_path = resolve_font(font or manifest_font)

    results: list[dict] = []
    failures = 0
    for entry in entries:
        try:
            results.append(process_entry(entry, preview_dir, font_path))
        except Exception as exc:  # 单条失败不影响其他视频
            failures += 1
            skipped.append(
                SkippedEntry(entry.video_id, f"处理异常：{type(exc).__name__}: {exc}", entry.order)
            )

    results.sort(key=lambda item: item["video_id"])
    skipped.sort(key=lambda item: item.video_id)

    payload = {
        "font": str(font_path),
        "results": results,
        "skipped": [
            {"video_id": item.video_id, "reason": item.reason} for item in skipped
        ],
        "summary": {
            "success": len(results),
            "skipped": len(skipped),
            "preview_count": len(results),
            "failures": failures,
            "output_dir": str(output_dir),
            "previews_dir": str(preview_dir),
            "results_json": str(output_dir / RESULTS_JSON),
        },
    }
    with (output_dir / RESULTS_JSON).open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="竖屏短视频封面候选帧对比预览批处理"
    )
    parser.add_argument("--manifest", required=True, help="清单 JSON 路径")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出目录（默认：清单同级 ../output）",
    )
    parser.add_argument("--font", default=None, help="本地字体文件路径")
    args = parser.parse_args(argv)
    _ensure_utf8_console()

    manifest_path = Path(args.manifest).resolve()
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else (manifest_path.parent.parent / "output").resolve()
    )

    try:
        payload = run_batch(manifest_path, output_dir, args.font)
    except (FontError, FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"批处理启动失败：{exc}", file=sys.stderr)
        return 1

    summary = payload["summary"]
    print("=" * 56)
    print("封面候选帧批处理完成，摘要如下")
    print("=" * 56)
    print(f"成功视频数：{summary['success']}")
    print(f"跳过条目数：{summary['skipped']}")
    print(f"处理异常数：{summary['failures']}")
    print(f"预览图数量：{summary['preview_count']}")
    print(f"输出目录  ：{summary['output_dir']}")
    print(f"预览图目录：{summary['previews_dir']}")
    print(f"结果 JSON ：{summary['results_json']}")
    if payload["skipped"]:
        print("-" * 56)
        print("跳过明细：")
        for item in payload["skipped"]:
            print(f"  - {item['video_id']}：{item['reason']}")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
