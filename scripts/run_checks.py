"""端到端实际检查：跑样例、抽帧、逐条核对 13 项需求，打印结果表。

用法：
    python scripts/run_checks.py            # 输出到 out/checks
    python scripts/run_checks.py --out-dir my_out

脚本只使用本地样例与本地 ffmpeg，不联网。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SAMPLES = REPO / "samples"
RESULTS: list[tuple[str, bool, str]] = []


def check(item: str, ok: bool, note: str) -> None:
    RESULTS.append((item, ok, note))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {item}  {note}")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "previewkit", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(REPO / "out" / "checks"))
    args = parser.parse_args()
    out = Path(args.out_dir)
    if out.exists():
        shutil.rmtree(out)

    main_dir = out / "main"
    proc = run(
        [
            "--shots",
            str(SAMPLES / "shots_main.json"),
            "--subtitles",
            str(SAMPLES / "subtitles_main.json"),
            "--out-dir",
            str(main_dir),
            "--width",
            "640",
            "--height",
            "360",
            "--fps",
            "10",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    report = load(main_dir / "report_shots_main__subtitles_main.json")
    frames = report["frame_index"]

    # 1 规范镜头序列
    canonical = report["shots"]["canonical"]
    starts = [s["start_ms"] for s in canonical]
    ids = [s["shot_id"] for s in canonical]
    check(
        "1 镜头序列",
        starts == sorted(starts) and ids == ["S01", "S02", "S03", "S04"],
        f"输入乱序 S03,S01,S04,S02 → 输出 {ids}，起始 {starts}",
    )

    # 2 字幕字段原样保留
    source = load(SAMPLES / "subtitles_main.json")["subtitles"]
    composed = report["subtitles"]["composed"]
    same = all(
        (entry["text"], entry["start_ms"], entry["end_ms"], entry["shot_id"])
        == (
            source[entry["subtitle_index"]]["text"],
            source[entry["subtitle_index"]]["start_ms"],
            source[entry["subtitle_index"]]["end_ms"],
            source[entry["subtitle_index"]].get("shot_id"),
        )
        for entry in composed
    )
    check("2 字幕字段", same and len(composed) == 4, "4 条保留的字幕与输入逐字段一致")

    # 3 跨切点连续
    before = next(f for f in frames if f["time_ms"] == 4700)
    after = next(f for f in frames if f["time_ms"] == 4800)
    check(
        "3 跨切点连续",
        before["subtitle_texts"] == after["subtitle_texts"] == ["这句字幕横跨切点，不能断"]
        and before["shot_id"] != after["shot_id"],
        f"{before['shot_id']}@{before['time_ms']} 与 {after['shot_id']}@{after['time_ms']} "
        f"均为同一文本",
    )

    # 4 时长与抽帧
    settings = report["settings"]
    duration_ok = settings["frame_count"] == settings["duration_ms"] * settings["fps"] // 1000
    sampled = [f for f in frames if f["time_ms"] in (300, 4700, 4800, 5300, 8700)]
    expectations = {
        300: ["第一镜：开场"],
        4700: ["这句字幕横跨切点，不能断"],
        4800: ["这句字幕横跨切点，不能断"],
        5300: ["这句字幕横跨切点，不能断", "第三镜：特写"],
        8700: ["结尾一句"],
    }
    text_ok = all(f["subtitle_texts"] == expectations[f["time_ms"]] for f in sampled)
    check(
        "4 时长与抽帧",
        duration_ok and text_ok,
        f"{settings['frame_count']} 帧 × {settings['fps']}fps = {settings['duration_ms']}ms；"
        f"抽查 {[f['time_ms'] for f in sampled]} 文本均与字幕表一致",
    )

    # 5 无效镜头编号
    unfinished = report["subtitles"]["unfinished"]
    ghost = [u for u in unfinished if u["text"] == "幽灵镜头里的字幕"]
    check(
        "5 无效镜头编号",
        len(ghost) == 1
        and ghost[0]["reason"] == "unknown_shot"
        and all("幽灵镜头里的字幕" not in f["subtitle_texts"] for f in frames),
        f"登记为 {ghost[0]['reason']}，{len(frames)} 帧中均未出现",
    )

    # 6 倒序 / 越界
    by_reason = {g["reason"]: g["count"] for g in report["subtitles"]["unfinished_by_reason"]}
    legal_texts = {"第一镜：开场", "这句字幕横跨切点，不能断", "第三镜：特写", "结尾一句"}
    all_legal_present = all(
        any(text in f["subtitle_texts"] for f in frames) for text in legal_texts
    )
    check(
        "6 倒序与越界",
        by_reason.get("reversed_range") == 1
        and by_reason.get("out_of_bounds") == 1
        and all_legal_present,
        f"reversed_range=1, out_of_bounds=1, empty_text={by_reason.get('empty_text', 0)}；"
        f"4 条合法字幕均在对应帧出现",
    )

    # 7 重叠冲突
    overlap_dir = out / "overlap"
    proc = run(
        [
            "--shots",
            str(SAMPLES / "shots_overlap.json"),
            "--subtitles",
            str(SAMPLES / "subtitles_overlap.json"),
            "--out-dir",
            str(overlap_dir),
            "--width",
            "640",
            "--height",
            "360",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    ov = load(overlap_dir / "report_shots_overlap__subtitles_overlap.json")
    conflict = ov["conflicts"][0]
    ov_frames = ov["frame_index"]
    at_2900 = next(f for f in ov_frames if f["time_ms"] == 2900)
    at_3000 = next(f for f in ov_frames if f["time_ms"] == 3000)
    check(
        "7 重叠冲突",
        (conflict["earlier_shot_id"], conflict["later_shot_id"]) == ("S_A", "S_B")
        and conflict["cut_at_ms"] == 3000
        and at_2900["shot_id"] == "S_A"
        and at_3000["shot_id"] == "S_B",
        "冲突对 S_A/S_B，重叠 2200–3000ms，切点 3000ms（更早镜头的结束时刻）",
    )

    # 8 空档
    gap_dir = out / "gap"
    proc = run(
        [
            "--shots",
            str(SAMPLES / "shots_gap.json"),
            "--subtitles",
            str(SAMPLES / "subtitles_gap.json"),
            "--out-dir",
            str(gap_dir),
            "--width",
            "640",
            "--height",
            "360",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    gp = load(gap_dir / "report_shots_gap__subtitles_gap.json")
    gap_frames = [f for f in gp["frame_index"] if f["kind"] == "gap"]
    check(
        "8 空档",
        len(gap_frames) == 10
        and all(f["shot_id"] is None and not f["subtitle_texts"] for f in gap_frames)
        and gp["gaps"][0]["start_ms"] == 4000,
        f"空档 4000–5000ms 共 {len(gap_frames)} 帧，无镜头无字幕，已写入 gaps 须知",
    )

    # 9 输出目录与文件名
    check(
        "9 参数与文件名",
        (main_dir / "preview_shots_main__subtitles_main.mkv").is_file()
        and (main_dir / "report_shots_main__subtitles_main.json").is_file(),
        "preview_<清单>__<字幕>.mkv / report_<清单>__<字幕>.json 落在 --out-dir",
    )

    # 10 报告结构
    vocab = {g["reason"] for g in report["subtitles"]["unfinished_by_reason"]}
    check(
        "10 报告结构",
        {"shots", "summary", "subtitles", "status"} <= set(report)
        and vocab
        <= {"reversed_range", "out_of_bounds", "empty_text", "unknown_shot", "outside_shot"},
        f"含 canonical 镜头列表、subtitles_composed={report['summary']['subtitles_composed']}、"
        f"分类 {sorted(vocab)}",
    )

    # 11 非法输入
    bad_dir = out / "invalid"
    proc = run(
        [
            "--shots",
            str(SAMPLES / "invalid" / "shots_broken_json.json"),
            "--subtitles",
            str(SAMPLES / "invalid" / "subtitles_ok.json"),
            "--out-dir",
            str(bad_dir),
        ]
    )
    check(
        "11 非法输入",
        proc.returncode == 2 and "不是合法 JSON" in proc.stderr and not bad_dir.exists(),
        f"退出码 {proc.returncode}；stderr 首行：{proc.stderr.strip().splitlines()[0]}；"
        f"输出目录未创建（含半成品检查）",
    )

    # 12 成功项独立可查
    shot_ids = {s["shot_id"] for s in report["shots"]["canonical"]}
    indexes = {entry["subtitle_index"] for entry in composed}
    check(
        "12 成功项独立",
        indexes == {0, 1, 2, 6} and shot_ids == {"S01", "S02", "S03", "S04"},
        f"失败 5 条的同时，成功字幕下标 {sorted(indexes)}、镜头 {sorted(shot_ids)} 仍可逐条核对",
    )

    # 13 重复运行一致
    rerun_dir = out / "rerun"
    proc = run(
        [
            "--shots",
            str(SAMPLES / "shots_main.json"),
            "--subtitles",
            str(SAMPLES / "subtitles_main.json"),
            "--out-dir",
            str(rerun_dir),
            "--width",
            "640",
            "--height",
            "360",
            "--fps",
            "10",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    same_video = sha(main_dir / "preview_shots_main__subtitles_main.mkv") == sha(
        rerun_dir / "preview_shots_main__subtitles_main.mkv"
    )
    same_report = sha(main_dir / "report_shots_main__subtitles_main.json") == sha(
        rerun_dir / "report_shots_main__subtitles_main.json"
    )
    check(
        "13 重复运行一致",
        same_video and same_report,
        f"视频 sha256 {'一致' if same_video else '不同'}，报告 sha256 "
        f"{'一致' if same_report else '不同'}",
    )

    # 抽几帧存图，便于人工复核
    png_dir = out / "frames"
    png_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = None
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        picks = {
            "main_cut_before": (main_dir / "preview_shots_main__subtitles_main.mkv", 47),
            "main_cut_after": (main_dir / "preview_shots_main__subtitles_main.mkv", 48),
            "main_shot1": (main_dir / "preview_shots_main__subtitles_main.mkv", 3),
            "gap_placeholder": (gap_dir / "preview_shots_gap__subtitles_gap.mkv", 45),
        }
        for name, (video, index) in picks.items():
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(video),
                    "-vf",
                    f"select=eq(n\\,{index})",
                    "-vsync",
                    "0",
                    "-frames:v",
                    "1",
                    str(png_dir / f"{name}.png"),
                ],
                check=True,
                timeout=300,
            )
        print(f"\n抽帧图片：{png_dir}")

    print()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"合计 {passed}/{len(RESULTS)} 项检查通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
