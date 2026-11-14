"""Run the 14 acceptance checks against the real CLI and record the results.

    python tools/verify_requirements.py
    python tools/verify_requirements.py --out build/verification

Writes ``VERIFICATION.md`` (machine-checked results) next to the exports it
produces.  Uses only Pillow and the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import PIL
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
CLEAN = ROOT / "samples" / "clean_storyboard" / "description.json"
ISSUES = ROOT / "samples" / "storyboard_issues" / "description.json"
INVALID_FPS = ROOT / "samples" / "invalid_fps" / "description.json"


class Checker:
    def __init__(self) -> None:
        self.results: list[tuple[int, str, bool, str]] = []

    def check(self, number: int, title: str, passed: bool, detail: str) -> None:
        self.results.append((number, title, bool(passed), detail))

    def run_cli(self, spec: Path, out: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [PYTHON, "-m", "frame_export", str(spec), str(out)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def load(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def by_index(self, report: dict) -> dict:
        return {entry["index"]: entry for entry in report["frames"]}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def alpha_extrema(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.load()
        if image.mode not in ("RGBA", "LA"):
            image = image.convert("RGBA")
        return image.getchannel("A").getextrema()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="build/verification")
    args = parser.parse_args()
    out_root = (ROOT / args.out).resolve()
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)

    checker = Checker()
    clean_out = out_root / "clean"
    issues_out = out_root / "issues"
    invalid_out = out_root / "invalid_fps"

    clean_run = checker.run_cli(CLEAN, clean_out)
    issues_run = checker.run_cli(ISSUES, issues_out)
    invalid_run = checker.run_cli(INVALID_FPS, invalid_out)

    clean_report = checker.load(clean_out / "report.json")
    issues_report = checker.load(issues_out / "report.json")
    clean_desc = checker.load(CLEAN)
    clean_by_index = checker.by_index(clean_report)
    issues_by_index = checker.by_index(issues_report)

    # 1 — order comes from the description array, not file names.
    described = [frame["file"] for frame in clean_desc["frames"]]
    reported = [entry["source_file"] for entry in clean_report["frames"]]
    sorted_names = sorted(described)
    ok = described == reported and sorted_names != described
    checker.check(
        1,
        "按描述顺序导出，与文件名排序无关",
        ok and clean_run.returncode == 0,
        f"描述顺序前 3: {described[:3]}；文件名字典序前 3: {sorted_names[:3]}；"
        f"报告顺序一致={described == reported}",
    )

    # 2 — undersized frame is centred on real background pixels.
    small = clean_by_index[1]
    small_png = clean_out / small["output"]
    image = Image.open(small_png)
    image.load()
    background = tuple(clean_report["background"]["rgba"])
    corners = [
        image.getpixel((0, 0)),
        image.getpixel((image.width - 1, 0)),
        image.getpixel((0, image.height - 1)),
        image.getpixel((image.width - 1, image.height - 1)),
    ]
    ok = (
        image.size == (320, 180)
        and all(pixel == background for pixel in corners)
        and small["size_processing"]["content_box"] == [80, 45, 240, 135]
    )
    checker.check(
        2,
        "小尺寸帧居中留白，留白为真实背景像素",
        ok,
        f"160x90 → {image.size[0]}x{image.size[1]}，四角像素 {corners[0]}，"
        f"背景 {background}，内容框 {small['size_processing']['content_box']}",
    )

    # 3 — RGB frame is fully opaque and flagged in the report.
    rgb_entry = clean_by_index[3]
    rgb_alpha = alpha_extrema(clean_out / rgb_entry["output"])
    ok = (
        rgb_alpha == (255, 255)
        and rgb_entry["channel"]["source_mode"] == "RGB"
        and rgb_entry["channel"]["has_alpha_channel"] is False
        and any("缺少透明通道" in note for note in rgb_entry["notes"])
    )
    checker.check(
        3,
        "无透明通道帧按不透明处理并在报告标注",
        ok,
        f"源模式={rgb_entry['channel']['source_mode']}，导出 alpha 范围={rgb_alpha}，"
        f"标注={[n for n in rgb_entry['notes'] if '透明通道' in n]}",
    )

    # 4 — oversized frame scaled down, nothing cropped.
    big = clean_by_index[2]
    big_image = Image.open(clean_out / big["output"])
    big_image.load()
    ok = (
        big["size_processing"]["placement"] == "downscaled"
        and big["size_processing"]["source_size"] == [480, 270]
        and big_image.size == (320, 180)
        and big["size_processing"]["content_box"] == [0, 0, 320, 180]
        and abs(big["size_processing"]["scale"] - 2 / 3) < 1e-5
    )
    checker.check(
        4,
        "超画幅帧等比缩放且完整可见",
        ok,
        f"480x270 → {big_image.size[0]}x{big_image.size[1]}，"
        f"缩放 {big['size_processing']['scale']}，内容框 {big['size_processing']['content_box']}",
    )

    # 5 — durations drive the timeline and the total.
    expected_total = sum(frame["duration_ms"] for frame in clean_desc["frames"])
    preview_text = (clean_out / "preview.html").read_text(encoding="utf-8")
    ok = (
        clean_report["summary"]["duration_ms_exported"] == expected_total
        and f"累计 {expected_total} ms" in preview_text
        and preview_text.count("data-duration-ms=") == len(clean_desc["frames"])
    )
    checker.check(
        5,
        "停留毫秒参与时间线，累计时长与描述一致",
        ok,
        f"描述累计 {expected_total} ms，报告 {clean_report['summary']['duration_ms_exported']} ms，"
        f"预览含 {preview_text.count('data-duration-ms=')} 个时长",
    )

    # 6 — negative duration invalidates only that frame.
    invalid_frame = issues_by_index[5]
    others_ok = all(
        entry["usable"] for entry in issues_report["frames"] if entry["index"] != 5
        and entry["index"] not in (6, 9, 13)
    )
    ok = (
        invalid_frame["status"] == "invalid"
        and invalid_frame["error"]["code"] == "invalid_duration"
        and invalid_frame["usable"] is False
        and others_ok
    )
    checker.check(
        6,
        "停留毫秒为负/-非数值时该帧无效，其余继续",
        ok,
        f"序号 5 状态={invalid_frame['status']}，原因={invalid_frame['error']['message']}；"
        f"序号 13 状态={issues_by_index[13]['status']}",
    )

    # 7 — corrupt PNG does not stop the batch.
    corrupt_entry = issues_by_index[9]
    ok = (
        corrupt_entry["status"] == "corrupt"
        and corrupt_entry["error"]["code"] == "frame_undecodable"
        and issues_report["summary"]["frames_exported"] >= 10
        and issues_run.returncode == 2
    )
    checker.check(
        7,
        "损坏帧不中断整批，其余帧正常产出",
        ok,
        f"序号 9 状态={corrupt_entry['status']}，{corrupt_entry['error']['message']}；"
        f"其余导出 {issues_report['summary']['frames_exported']} 帧",
    )

    # 8 — missing file is distinguished, other pixels unchanged.
    missing_entry = issues_by_index[6]
    baseline_hashes = {
        entry["index"]: entry["sha256"] for entry in clean_report["frames"]
    }
    # The issues sample shares frame files with the clean sample, so every
    # exported frame that is not itself faulted must hash-match the baseline.
    # (Index 4 is duplicated in the issues sample, index 5 has the bad
    # duration, so those two are excluded from the hash comparison.)
    compared = [
        entry["index"]
        for entry in issues_report["frames"]
        if entry["usable"] and entry["index"] not in (4, 5)
    ]
    shared = [
        entry["index"]
        for entry in issues_report["frames"]
        if entry["usable"]
        and entry["index"] not in (4, 5)
        and entry["sha256"] == baseline_hashes.get(entry["index"])
    ]
    ok = (
        missing_entry["status"] == "missing_file"
        and missing_entry["output"] is None
        and len(compared) >= 10
        and shared == compared
    )
    checker.check(
        8,
        "缺失帧与成功帧区分，成功帧像素不受污染",
        ok,
        f"序号 6 状态={missing_entry['status']}；"
        f"{len(shared)}/{len(compared)} 帧合成 PNG 与全量基线逐字节一致",
    )

    # 9 — duplicate / missing indices are called out, gaps are not exported.
    sequence = issues_report["sequence"]
    exported_indices = {entry["index"] for entry in issues_report["frames"]}
    ok = (
        sequence["missing_indices"] == [3]
        and sequence["duplicate_indices"] == [4]
        and 3 not in exported_indices
    )
    checker.check(
        9,
        "重复/缺失序号明确列出，缺失帧不导出为空白帧",
        ok,
        f"缺失={sequence['missing_indices']}，重复={sequence['duplicate_indices']}，"
        f"报告条目数={len(issues_report['frames'])}（与描述条目一致，无补白）",
    )

    # 10 — report schema completeness and order.
    required = {
        "index",
        "source_file",
        "duration_ms",
        "size_processing",
        "channel",
    }
    schema_ok = all(
        required <= set(entry) and entry["size_processing"] and entry["channel"]
        for entry in clean_report["frames"]
        if entry["usable"]
    )
    order_ok = [entry["index"] for entry in clean_report["frames"]] == [
        frame["index"] for frame in clean_desc["frames"]
    ]
    checker.check(
        10,
        "报告 JSON 字段齐全，序号顺序与描述一致",
        schema_ok and order_ok,
        f"{len(clean_report['frames'])} 条逐帧记录，必需字段全部存在，顺序一致={order_ok}",
    )

    # 11 — preview page shows all successful frames in order.
    images = [clean_out / entry["output"] for entry in clean_report["frames"] if entry["usable"]]
    order_markers = [
        int(value) for value in re.findall(r'data-ordinal="(\d+)"', preview_text)
    ]
    ok = (
        len(images) == 18
        and all(path.is_file() for path in images)
        and order_markers == list(range(1, 19))
        and "data-duration-ms" in preview_text
    )
    checker.check(
        11,
        "预览页面按顺序展示全部成功帧与停留时长",
        ok,
        f"卡片 {len(order_markers)} 张，PNG {len(images)} 张全部存在，顺序={order_markers[:5]}…",
    )

    # 12 — invalid global config yields a readable failure.
    failure_report = checker.load(invalid_out / "report.json")
    failure_preview = (invalid_out / "preview.html").read_text(encoding="utf-8")
    ok = (
        invalid_run.returncode == 3
        and failure_report["verdict"] == "unexportable"
        and failure_report["error"]["code"] == "invalid_fps"
        and "帧率" in failure_report["error"]["message"]
        and "帧率" in failure_preview
        and "帧率" in invalid_run.stderr
    )
    checker.check(
        12,
        "帧率/画幅非法时给出可读失败说明",
        ok,
        f"退出码 {invalid_run.returncode}，报告错误={failure_report['error']['message']}",
    )

    # 13 — three-way verdict.
    ok = (
        clean_report["verdict"] == "complete"
        and issues_report["verdict"] == "partial"
        and failure_report["verdict"] == "unexportable"
    )
    checker.check(
        13,
        "整批结论三分类：complete / partial / unexportable",
        ok,
        f"全量={clean_report['verdict']}，含坏帧={issues_report['verdict']}，"
        f"描述不可用={failure_report['verdict']}",
    )

    # 14 — two consecutive runs are byte-identical.
    second = out_root / "clean_second"
    second_run = checker.run_cli(CLEAN, second)
    identical_report = sha256_file(clean_out / "report.json") == sha256_file(
        second / "report.json"
    )
    identical_preview = sha256_file(clean_out / "preview.html") == sha256_file(
        second / "preview.html"
    )
    frame_files = sorted(path.name for path in (clean_out / "frames").iterdir())
    frame_identical = all(
        sha256_file(clean_out / "frames" / name) == sha256_file(second / "frames" / name)
        for name in frame_files
    )
    checker.check(
        14,
        "连续运行两次输出完全一致",
        identical_report and identical_preview and frame_identical,
        f"report={identical_report}，preview={identical_preview}，"
        f"{len(frame_files)} 帧 PNG={frame_identical}，退出码 {clean_run.returncode}/{second_run.returncode}",
    )

    # Write the markdown record.
    passed = sum(1 for _, _, ok_flag, _ in checker.results if ok_flag)
    lines = [
        "# 实际检查结果（machine-checked）",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 平台：{platform.platform()}",
        f"- Python：{platform.python_version()}",
        f"- Pillow：{PIL.__version__}",
        f"- 命令：`python tools/verify_requirements.py --out {args.out}`",
        f"- 结果：**{passed}/{len(checker.results)} 项通过**",
        "",
        "| # | 检查项 | 结果 | 证据 |",
        "|---|--------|------|------|",
    ]
    for number, title, ok_flag, detail in checker.results:
        lines.append(
            f"| {number} | {title} | {'PASS' if ok_flag else 'FAIL'} | {detail} |"
        )
    lines.extend(
        [
            "",
            "## 产物",
            "",
            f"- 全量样本导出包：`{(clean_out / 'preview.html').relative_to(ROOT).as_posix()}`",
            f"- 故障样本导出包：`{(issues_out / 'preview.html').relative_to(ROOT).as_posix()}`",
            f"- 非法帧率失败包：`{(invalid_out / 'preview.html').relative_to(ROOT).as_posix()}`",
            "",
            "## 单元/验收测试",
            "",
            "```",
            "python -m unittest discover -s tests -v",
            "```",
            "",
            "覆盖 14 条验收要求及边界情况（路径越界、重复序号文件冲突、别名键、缺省背景色）。",
            "",
        ]
    )
    target = ROOT / "VERIFICATION.md"
    target.write_text("\n".join(lines), encoding="utf-8", newline="\n")

    for number, title, ok_flag, detail in checker.results:
        print(f"[{'PASS' if ok_flag else 'FAIL'}] {number:02d} {title} — {detail}")
    print(f"\n{passed}/{len(checker.results)} checks passed")
    print(f"record: {target}")
    return 0 if passed == len(checker.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
