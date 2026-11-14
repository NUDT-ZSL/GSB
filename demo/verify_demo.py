"""本地验证脚本：逐条核对需求 1–14 的验收点。

用法：python demo/verify_demo.py
脚本独立于实现内部结构，只读取清单、说明 JSON 与 PNG 像素。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "demo" / "samples"
ASSETS = ROOT / "demo" / "assets"
OUT = ROOT / "demo" / "out"

SAMPLES_TO_RUN = (
    "main",
    "time_window",
    "panel_invalid",
    "tight_height",
    "empty_subtitles",
    "blank_subtitles",
    "no_panel",
    "broken_frames",
)

failures: list[str] = []
checks = 0


def check(condition: bool, message: str) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(message)
        print(f"  [失败] {message}")


def run(manifest: Path, out_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "subtitle_preview.cli",
            "--manifest",
            str(manifest),
            "--out",
            str(out_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def load_report(out_dir: Path) -> dict:
    return json.loads((out_dir / "render_report.json").read_text(encoding="utf-8"))


def load_manifest(name: str) -> dict:
    return json.loads((SAMPLES / name / "manifest.json").read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        image.load()
        return image.convert("RGB")


def diff_bbox(a: Image.Image, b: Image.Image):
    return ImageChops.difference(a, b).getbbox()


def rects_intersect(a: dict, b: dict) -> bool:
    return (
        a["left"] < b["right"]
        and b["left"] < a["right"]
        and a["top"] < b["bottom"]
        and b["top"] < a["bottom"]
    )


def contains(outer: dict, inner: dict) -> bool:
    return (
        outer["left"] <= inner["left"]
        and inner["right"] <= outer["right"]
        and outer["top"] <= inner["top"]
        and inner["bottom"] <= outer["bottom"]
    )


def source_frame(name: str, manifest: dict, time_ms: int) -> Path:
    entry = next(f for f in manifest["frames"] if f["time_ms"] == time_ms)
    return (SAMPLES / name / entry["path"]).resolve()


def verify_pixels(name: str) -> None:
    """需求 3/4/5/6/8：逐帧核对时间窗、面板不相交、未放置不渲染、坐标稳定。"""
    print(f"像素核对 {name}：")
    manifest = load_manifest(name)
    report = load_report(OUT / name)
    panel = report["panel"]["rect"]
    safe_area = report["safe_area"]

    # 每帧的实际改动区域（相对原帧）。
    changes: dict[int, tuple | None] = {}
    for frame in report["frames"]:
        if frame["output"] is None:
            continue
        changes[frame["index"]] = diff_bbox(
            open_rgb(source_frame(name, manifest, frame["time_ms"])),
            open_rgb(OUT / name / frame["output"]),
        )

    active_times: dict[int, list[dict]] = {}
    for entry in report["subtitles"]:
        if not entry["placed"]:
            continue
        for frame in entry["frames"]:
            active_times.setdefault(frame["index"], []).append(entry)

    for frame in report["frames"]:
        if frame["output"] is None:
            continue
        bbox = changes[frame["index"]]
        boxes = [e["box"] for e in active_times.get(frame["index"], [])]
        if not boxes:
            check(
                bbox is None,
                f"{name}: 帧 {frame['index']} 无生效字幕，但出现像素改动 {bbox}",
            )
            continue
        check(
            bbox is not None,
            f"{name}: 帧 {frame['index']} 有生效字幕，但像素无改动",
        )
        if bbox is None:
            continue
        union = {
            "left": min(b["left"] for b in boxes),
            "top": min(b["top"] for b in boxes),
            "right": max(b["right"] for b in boxes),
            "bottom": max(b["bottom"] for b in boxes),
        }
        check(
            contains(union, {"left": bbox[0], "top": bbox[1], "right": bbox[2], "bottom": bbox[3]}),
            f"{name}: 帧 {frame['index']} 绘制区域 {bbox} 越出字幕文本框并集",
        )
        check(
            panel is None
            or not rects_intersect(
                {"left": bbox[0], "top": bbox[1], "right": bbox[2], "bottom": bbox[3]}, panel
            ),
            f"{name}: 帧 {frame['index']} 绘制区域落入面板",
        )

    for entry in report["subtitles"]:
        if not entry["placed"]:
            for frame in entry["frames"]:
                check(
                    all(e["index"] != entry["index"] for e in active_times.get(frame["index"], [])),
                    f"{name}: 未放置字幕[{entry['index']}] 出现在生效列表",
                )
            continue
        check(contains(safe_area, entry["box"]), f"{name}: 字幕[{entry['index']}] 越出安全区")
        check(
            panel is None or not rects_intersect(entry["box"], panel),
            f"{name}: 字幕[{entry['index']}] 文本框与面板相交",
        )

    # 需求 6：同一字幕独占的帧之间，像素改动区域完全一致。
    for entry in report["subtitles"]:
        if not entry["placed"]:
            continue
        solo = [
            changes[f["index"]]
            for f in entry["frames"]
            if f["index"] in changes and len(active_times.get(f["index"], [])) == 1
        ]
        if len(solo) < 2:
            continue
        check(
            len(set(solo)) == 1,
            f"{name}: 字幕[{entry['index']}] 跨帧文本框不一致 {set(solo)}",
        )
        bbox = solo[0]
        if bbox is not None:
            check(
                entry["box"]["left"] <= bbox[0] and bbox[2] <= entry["box"]["right"]
                and entry["box"]["top"] <= bbox[1] and bbox[3] <= entry["box"]["bottom"],
                f"{name}: 字幕[{entry['index']}] 像素区域不在说明坐标内",
            )


def verify_main() -> None:
    print("主样例 main：")
    report = load_report(OUT / "main")
    subs = report["subtitles"]
    check(len(subs) == 5, "main: 字幕条目数应为 5（空白条目被忽略）")
    check(
        all(subs[i]["start_ms"] <= subs[i + 1]["start_ms"] for i in range(len(subs) - 1)),
        "main: 字幕应按起始毫秒非降序",
    )
    for entry in subs:
        for key in ("start_ms", "end_ms", "frames", "box", "onset_offset_ms", "result"):
            check(key in entry, f"main: 字幕[{entry['index']}] 缺少字段 {key}")
    by_index = {s["index"]: s for s in subs}

    # 需求 2：起音偏移语义（正整数 / null）。
    check(by_index[0]["onset_offset_ms"] == 40, "main: 字幕[0] 起音偏移应为 40ms")
    check(by_index[2]["onset_offset_ms"] == 600, "main: 字幕[2] 起音偏移应为 600ms")
    check(by_index[3]["onset_offset_ms"] is None, "main: 无起音时偏移应为 null")
    check(
        isinstance(by_index[2]["onset_offset_ms"], int) and by_index[2]["onset_offset_ms"] > 0,
        "main: 起音早于字幕起始时偏移应为正整数",
    )
    check(
        all(
            isinstance(s["onset_offset_ms"], int) or s["onset_offset_ms"] is None
            for s in subs
        ),
        "main: 偏移字段应为整数毫秒或 null",
    )

    # 需求 14：偏移 600ms 标记偏移过大；40ms 保持已放置。
    check(by_index[2]["result"] == "偏移过大", "main: 偏移 600ms 应标记为偏移过大")
    check(by_index[2]["placed"] is True, "main: 偏移过大仍应放置（仅提醒复核）")
    check(by_index[0]["result"] == "已放置", "main: 偏移 40ms 应为已放置")

    # 需求 5：三行文本在只能容纳两行时整段未放置。
    check(by_index[4]["placed"] is False, "main: 超行字幕应未放置")
    check(by_index[4]["box"] is None, "main: 未放置字幕不应有文本框")
    check(by_index[4]["result"] == "未放置", "main: 超行字幕处理结果应为未放置")

    # 需求 4/6：放置字幕的文本框约束。
    for entry in subs:
        if entry["placed"]:
            check(
                not rects_intersect(entry["box"], report["panel"]["rect"]),
                f"main: 字幕[{entry['index']}] 文本框与面板相交",
            )
    check(
        [f["time_ms"] for f in by_index[1]["frames"]] == [1100],
        "main: 字幕[1] 只应落在 1100ms 帧",
    )


def verify_time_window() -> None:
    print("时间窗 time_window：")
    report = load_report(OUT / "time_window")
    manifest = load_manifest("time_window")
    entry = report["subtitles"][0]
    check(
        [f["time_ms"] for f in entry["frames"]] == [100],
        "time_window: 仅 100ms 帧落在起止范围内",
    )
    for frame in report["frames"]:
        source = source_frame("time_window", manifest, frame["time_ms"])
        changed = diff_bbox(open_rgb(source), open_rgb(OUT / "time_window" / frame["output"]))
        if frame["time_ms"] == 100:
            check(changed is not None, "time_window: 中间帧应出现字幕")
        else:
            check(changed is None, f"time_window: {frame['time_ms']}ms 帧不应出现字幕")


def verify_geometry() -> None:
    print("几何参照：")
    no_panel = load_report(OUT / "no_panel")
    safe = no_panel["safe_area"]
    box = no_panel["subtitles"][0]["box"]
    check(
        box["left"] == safe["left"] + (safe["width"] - box["width"]) // 2,
        "no_panel: 水平未居中于安全区",
    )
    check(
        box["top"] == safe["top"] + (safe["height"] - box["height"]) // 2,
        "no_panel: 垂直未居中于安全区",
    )

    invalid = load_report(OUT / "panel_invalid")
    check(invalid["panel"]["status"] == "invalid", "panel_invalid: 面板状态应为 invalid")
    check(
        any("面板" in w for w in invalid["warnings"]),
        "panel_invalid: 应给出面板非法提示",
    )
    safe = invalid["safe_area"]
    box = invalid["subtitles"][0]["box"]
    check(
        box["left"] == safe["left"] + (safe["width"] - box["width"]) // 2,
        "panel_invalid: 无面板时应居中于整个安全区",
    )

    main = load_report(OUT / "main")
    region = main["usable_region"]
    box0 = main["subtitles"][0]["box"]
    check(
        box0["left"] == region["left"] + (region["width"] - box0["width"]) // 2,
        "main: 水平未居中于面板左侧可用区域",
    )
    check(
        box0["top"] == region["top"] + (region["height"] - box0["height"]) // 2,
        "main: 垂直未居中于安全区",
    )
    check(region["right"] <= main["panel"]["rect"]["left"], "main: 可用区域应止于面板左边界")


def verify_tight() -> None:
    print("剩余高度不足 tight_height：")
    report = load_report(OUT / "tight_height")
    entry = report["subtitles"][0]
    check(entry["placed"] is False, "tight_height: 应未放置")
    check(entry["result"] == "未放置", "tight_height: 处理结果应为未放置")
    check(
        report["usable_region"]["height"] < report["two_line_capacity_height"],
        "tight_height: 可用高度应小于两行所需高度",
    )
    manifest = load_manifest("tight_height")
    files = sorted((OUT / "tight_height").glob("preview_*.png"))
    check(len(files) == len(manifest["frames"]), "tight_height: 帧数量应与输入一致")
    for path in files:
        time_ms = int(path.name.split("_")[2].removesuffix("ms.png"))
        check(
            diff_bbox(open_rgb(source_frame("tight_height", manifest, time_ms)), open_rgb(path)) is None,
            f"tight_height: {path.name} 不应有字幕像素",
        )


def verify_empty(name: str) -> None:
    print(f"空字幕表 {name}：")
    report = load_report(OUT / name)
    check(report["subtitles"] == [], f"{name}: 字幕数组应为空")
    manifest = load_manifest(name)
    check(
        report["summary"]["subtitle_count"] == 0,
        f"{name}: 摘要中的字幕数量应为 0",
    )
    files = sorted((OUT / name).glob("preview_*.png"))
    check(len(files) == len(manifest["frames"]), f"{name}: 帧数量应与输入一致")
    for index, frame in enumerate(manifest["frames"]):
        path = OUT / name / f"preview_{index:04d}_{frame['time_ms']:09d}ms.png"
        check(path.is_file(), f"{name}: 缺少 {path.name}")
        if path.is_file():
            check(
                diff_bbox(open_rgb(source_frame(name, manifest, frame["time_ms"])), open_rgb(path))
                is None,
                f"{name}: {path.name} 应与原帧一致",
            )
    if name == "blank_subtitles":
        check(
            any("空白" in w or "为空" in w for w in report["warnings"]),
            "blank_subtitles: 应提示已忽略空白字幕",
        )


def verify_broken() -> None:
    print("坏帧 broken_frames：")
    report = load_report(OUT / "broken_frames")
    problems = [f for f in report["frames"] if f["problem"]]
    check(len(problems) == 2, "broken_frames: 应记录 2 个问题帧")
    check(
        any("不存在" in f["problem"] for f in problems),
        "broken_frames: 缺失帧应记录文件不存在",
    )
    check(
        any("解码" in f["problem"] for f in problems),
        "broken_frames: 损坏帧应记录解码失败",
    )
    outputs = [f["output"] for f in report["frames"] if f["output"]]
    check(len(outputs) == 4, "broken_frames: 其余 4 帧应正常输出")
    for name in outputs:
        check((OUT / "broken_frames" / name).is_file(), f"broken_frames: 缺少 {name}")
    check(
        any(f["problem"] for f in report["frames"]),
        "broken_frames: 说明的帧条目应记录问题",
    )


def verify_determinism() -> None:
    print("确定性与编码：")
    first = OUT / "main"
    redo = OUT / "_repeat"
    if redo.exists():
        shutil.rmtree(redo)
    result = run(SAMPLES / "main" / "manifest.json", redo)
    check(result.returncode == 0, "repeat: 第二次运行应成功")
    check(
        digest(first / "render_report.json") == digest(redo / "render_report.json"),
        "repeat: 说明 JSON 应逐字节相同",
    )
    first_files = sorted(p.name for p in first.glob("preview_*.png"))
    redo_files = sorted(p.name for p in redo.glob("preview_*.png"))
    check(first_files == redo_files, "repeat: 帧文件名应一致")
    if first_files == redo_files:
        check(
            all(digest(first / n) == digest(redo / n) for n in first_files),
            "repeat: 所有 PNG 应逐字节相同",
        )
    shutil.rmtree(redo)

    raw = (first / "render_report.json").read_bytes()
    text = raw.decode("utf-8")
    check("风起于青萍之末" in text, "encoding: 中文字段应以原字符存储")
    check("\\u" not in text, "encoding: 说明中不应出现 \\uXXXX 转义")
    check(
        not (first / "render_report.json").read_bytes().startswith(b"\xef\xbb\xbf"),
        "encoding: 说明不应带 BOM",
    )


def verify_output_errors() -> None:
    print("输出目录错误处理：")
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        target = blocker / "out"
        result = run(SAMPLES / "empty_subtitles" / "manifest.json", target)
        check(result.returncode != 0, "output: 不可写路径应返回非零退出码")
        check("输出" in result.stderr, "output: 错误信息应可读")
        check(not target.exists(), "output: 失败时不应创建目标目录")
        check(list(Path(tmp).iterdir()) == [blocker], "output: 失败时目录内不应残留新文件")

    # 嵌套的新目录应自动创建并成功写入。
    nested = OUT / "nested" / "deep" / "run"
    if nested.exists():
        shutil.rmtree(OUT / "nested")
    result = run(SAMPLES / "no_panel" / "manifest.json", nested)
    check(result.returncode == 0, "output: 不存在的输出目录应自动创建")
    check((nested / "render_report.json").is_file(), "output: 自动创建的目录应写入说明")


def main() -> int:
    if not (ASSETS / "frames").is_dir():
        print("缺少样例素材，请先运行 python demo/make_demo.py")
        return 2
    if OUT.exists():
        shutil.rmtree(OUT)
    for sample in SAMPLES_TO_RUN:
        result = run(SAMPLES / sample / "manifest.json", OUT / sample)
        check(
            result.returncode == 0,
            f"{sample}: 运行应以 0 退出，实际 {result.returncode}：{result.stderr.strip()}",
        )

    if failures:
        print(f"运行阶段失败 {len(failures)} 项，终止后续核对")
        return 1

    verify_main()
    verify_time_window()
    verify_geometry()
    verify_tight()
    verify_empty("empty_subtitles")
    verify_empty("blank_subtitles")
    verify_broken()
    verify_determinism()
    verify_output_errors()
    for name in SAMPLES_TO_RUN:
        verify_pixels(name)

    print()
    if failures:
        print(f"验证失败：{len(failures)} 项 / 共 {checks} 项检查")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{checks} 项检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
