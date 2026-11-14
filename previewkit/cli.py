"""命令行入口与主流程编排。

退出码：
    0  成功（可能含未完成字幕，详见报告 status: partial）
    2  输入格式非法 / 参数非法 —— 不产生任何预览文件
    3  环境或输出冲突（无 ffmpeg / 字体 / 输出文件已存在且未加 --overwrite）
    1  其他未预期错误
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

from .models import EnvironmentProblem, InputFormatError, PreviewError
from .planner import KIND_GAP, plan_frames, frame_count
from .renderer import Renderer, find_font, stable_palette_index
from .report import build_report, write_report
from .subtitles import SubtitleIndex, classify_all
from .timeline import build_timeline, canonical_order, total_duration_ms
from .video import encode, find_ffmpeg, pick_codec
from .verify import verify_video

VIDEO_STEM = "preview"
REPORT_STEM = "report"
SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")


def _slug(text: str) -> str:
    cleaned = SAFE_NAME.sub("-", text).strip("-._")
    return cleaned or "input"


def default_names(shots_path: Path, subtitles_path: Path) -> tuple[str, str]:
    """固定规则：preview_<shots名>__<字幕名>.<扩展名> / report_<同名>.json。"""
    tag = f"{_slug(shots_path.stem)}__{_slug(subtitles_path.stem)}"
    return f"{VIDEO_STEM}_{tag}.mkv", f"{REPORT_STEM}_{tag}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preview-render",
        description=("读取镜头清单与字幕表 JSON，生成可逐帧核对的无损字幕预览片与 JSON 报告。"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m previewkit --shots samples/shots.json "
            "--subtitles samples/subtitles.json --out-dir out\n"
        ),
    )
    parser.add_argument("--shots", required=True, help="镜头清单 JSON 路径")
    parser.add_argument("--subtitles", required=True, help="字幕表 JSON 路径")
    parser.add_argument("--out-dir", default="out", help="输出目录（默认 out）")
    parser.add_argument("--width", type=int, default=640, help="画面宽度，默认 640")
    parser.add_argument("--height", type=int, default=360, help="画面高度，默认 360")
    parser.add_argument("--fps", type=int, default=10, help="帧率，默认 10")
    parser.add_argument("--video-name", default=None, help="预览视频文件名（默认按固定规则生成）")
    parser.add_argument("--report-name", default=None, help="报告文件名（默认按固定规则生成）")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件路径")
    parser.add_argument("--font", default=None, help="中文字体文件路径（.ttf/.ttc）")
    parser.add_argument("--no-meta", action="store_true", help="不绘制顶部帧号/时间码信息条")
    parser.add_argument(
        "--no-subtitles-overlay",
        action="store_true",
        help="不绘制字幕（仅用于排查画面问题，报告仍按字幕表生成）",
    )
    parser.add_argument(
        "--no-frame-index",
        action="store_true",
        help="报告中省略逐帧索引（默认包含，便于逐帧核对）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖输出目录中已存在的同名预览视频/报告（默认拒绝并报错）",
    )
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.width < 16 or args.height < 16:
        parser.error("--width / --height 不得小于 16")
    if args.width % 2 or args.height % 2:
        parser.error("--width / --height 必须是偶数（编码器要求）")
    if not (1 <= args.fps <= 120):
        parser.error("--fps 必须在 1–120 之间")
    for name in ("video_name", "report_name"):
        value = getattr(args, name)
        if value:
            candidate = Path(value)
            if candidate.name != value or value in (".", ".."):
                parser.error(f"--{name.replace('_', '-')} 只能是文件名，不能包含路径")


class _PreparedOutputs:
    """产物占位；失败时清理本次新建的文件与目录。

    只删 track() 过的路径（本次要写的产物）。若目录是本次新建的且已空，
    一并删除，保证非法输入不会留下任何「半成品」痕迹。
    """

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.paths: list[Path] = []
        self._created_dir = False

    def __enter__(self) -> "_PreparedOutputs":
        if not self.out_dir.exists():
            self.out_dir.mkdir(parents=True)
            self._created_dir = True
        elif not self.out_dir.is_dir():
            raise EnvironmentProblem(f"输出路径已存在且不是目录：{self.out_dir}")
        return self

    def track(self, path: Path) -> Path:
        self.paths.append(path)
        return path

    def commit(self) -> None:
        self.paths.clear()

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            for path in self.paths:
                for candidate in (path, path.with_name(path.stem + ".part" + path.suffix)):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
            if self._created_dir:
                try:
                    if not any(self.out_dir.iterdir()):
                        self.out_dir.rmdir()
                except OSError:
                    pass
        return False


def run(args: argparse.Namespace) -> int:
    shots_path = Path(args.shots)
    subtitles_path = Path(args.subtitles)
    out_dir = Path(args.out_dir)

    # 1) 读入 + 校验（这一步失败不应在输出目录留下任何东西）
    from .loader import load_inputs

    shots, subtitles = load_inputs(shots_path, subtitles_path)

    # 2) 时间轴
    placed, gaps, conflicts = build_timeline(shots)
    total_ms = total_duration_ms(placed)
    if total_ms <= 0:
        raise InputFormatError("所有镜头区间都无效，素材总长度为 0")
    ordered = canonical_order(shots)
    shot_colors = {s.shot_id: stable_palette_index(s.shot_id) for s in ordered}

    # 3) 字幕
    composed, unfinished = classify_all(subtitles, placed, gaps)
    subtitle_index = SubtitleIndex(composed)

    # 4) 帧规划
    frames = plan_frames(placed, gaps, total_ms, args.fps, subtitle_index, shot_colors)
    expected_count = frame_count(total_ms, args.fps)
    assert len(frames) == expected_count

    # 边界帧（镜头切点前后、空档首尾），报告与核对都会用到
    boundary_frames: dict[str, list[int]] = {}
    for i in range(1, len(frames)):
        prev, cur = frames[i - 1], frames[i]
        if prev.kind != cur.kind or prev.shot_id != cur.shot_id:
            label = f"{prev.shot_id or 'GAP'}->{cur.shot_id or 'GAP'}"
            boundary_frames[label] = [prev.index, cur.index]
    for f in frames:
        if f.kind == KIND_GAP:
            boundary_frames.setdefault(
                f"gap:{f.segment_start_ms}-{f.segment_end_ms}", [f.index, f.index]
            )

    # 5) 环境检查放在写文件之前
    ffmpeg = find_ffmpeg(args.ffmpeg)
    codec, _ = pick_codec(ffmpeg)
    font = find_font(args.font)
    renderer = Renderer(
        args.width,
        args.height,
        font,
        show_meta=not args.no_meta,
        show_subtitles=not args.no_subtitles_overlay,
    )

    default_video, default_report = default_names(shots_path, subtitles_path)
    video_name = args.video_name or default_video
    report_name = args.report_name or default_report
    if video_name == report_name:
        raise EnvironmentProblem("预览视频与报告不能同名")

    with _PreparedOutputs(out_dir) as prepared:
        video_path = out_dir / video_name
        report_path = out_dir / report_name
        existing = [p for p in (video_path, report_path) if p.exists()]
        if existing and not args.overwrite:
            raise EnvironmentProblem(
                "输出文件已存在："
                + "、".join(p.name for p in existing)
                + "；确认覆盖请加 --overwrite"
            )
        # 先全部写到临时名，最后一步才原子替换成正式文件名。
        # 好处：中途失败时既不留半成品，也不会破坏已存在的旧产物。
        video_tmp = prepared.track(out_dir / f".{video_name}.tmp.mkv")
        report_tmp = prepared.track(out_dir / f".{report_name}.tmp.json")

        # 6) 渲染 + 编码
        def _frames():
            for spec in frames:
                yield renderer.render(spec, total_ms)

        encode(ffmpeg, _frames(), args.fps, args.width, args.height, video_tmp)

        # 7) 抽帧核对（针对真正落盘的文件，而不是内存里的绘制结果）
        verification = verify_video(
            ffmpeg=ffmpeg,
            video_path=video_tmp,
            frames=frames,
            renderer=renderer,
            total_ms=total_ms,
            boundary_frames=boundary_frames,
        )
        verification["video_sha256"] = _sha256(video_tmp)
        if not verification["ok"]:
            raise EnvironmentProblem(
                "生成后核对失败：解码出的帧与规划不一致，详见报告 verification 字段"
            )

        # 8) 报告
        report = build_report(
            shots=shots,
            placed=placed,
            gaps=gaps,
            conflicts=conflicts,
            composed=composed,
            unfinished=unfinished,
            frames=frames,
            fps=args.fps,
            width=args.width,
            height=args.height,
            total_ms=total_ms,
            video_path=video_path,
            report_path=report_path,
            shots_source=shots_path.as_posix(),
            subtitles_source=subtitles_path.as_posix(),
            codec=codec,
            include_frame_index=not args.no_frame_index,
            verification=verification,
        )
        write_report(report, report_tmp)

        # 9) 两个产物都完整后才替换为正式文件名
        os.replace(video_tmp, video_path)
        os.replace(report_tmp, report_path)
        prepared.commit()

    # 10) 终端摘要
    _print_summary(report, video_path, report_path)
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _print_summary(report: dict, video_path: Path, report_path: Path) -> None:
    s = report["summary"]
    print(
        f"预览视频 : {video_path}  ({report['settings']['frame_count']} 帧, "
        f"{report['settings']['duration_s']}s, {report['settings']['codec']})"
    )
    print(f"报告     : {report_path}")
    print(
        f"状态     : {report['status']}"
        + ("" if report["status"] == "complete" else "（存在未完成项，详见报告）")
    )
    print(
        f"镜头     : {s['shots_total']} 条，已渲染 {s['shots_rendered']}，"
        f"完全被重叠覆盖 {s['shots_fully_overlapped']}"
    )
    print(
        f"字幕     : 已合成 {s['subtitles_composed']} / {s['subtitles_total']}，"
        f"跨切点 {s['subtitles_spanning_cuts']}，未完成 {s['subtitles_unfinished']}"
    )
    print(f"时间轴   : 冲突 {s['conflicts']} 对，空档 {s['gaps']} 段（{s['gap_frames']} 帧空画面）")
    verification = report.get("verification")
    if verification:
        state = "通过" if verification["ok"] else "失败"
        print(
            f"逐帧核对 : {state}，抽样 {verification['sampled_count']} / "
            f"{verification['expected_frame_count']} 帧，"
            f"解码得到 {verification['decoded_frame_count']} 帧"
        )
    if report["subtitles"]["unfinished_by_reason"]:
        print("未完成明细：")
        for group in report["subtitles"]["unfinished_by_reason"]:
            print(
                f"  - {group['reason']}（{group['label']}）: {group['count']} 条 "
                f"→ 字幕下标 {[i['subtitle_index'] for i in group['items']]}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(args, parser)
    try:
        return run(args)
    except InputFormatError as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        return exc.exit_code
    except EnvironmentProblem as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        return exc.exit_code
    except PreviewError as exc:  # 兜底
        print(f"错误：{exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证退出码非零且有可读信息
        print(f"错误：未预期异常 {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
