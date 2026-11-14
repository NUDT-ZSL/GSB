#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地验收：逐条验证 8 项要求，并打印实际结果。

Run:  python tools/verify_acceptance.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "samples" / "cases"
SCRIPT = ROOT / "paper_preview.py"
OUT_ROOT = ROOT / "out"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str) -> None:
    RESULTS.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}: {detail}")


def run_case(case: str, extra_args: list[str] | None = None) -> tuple[int, Path]:
    out_dir = OUT_ROOT / "acceptance" / Path(case).stem
    if out_dir.exists():
        shutil.rmtree(out_dir)
    command = [
        sys.executable,
        str(SCRIPT),
        "--scene",
        str(CASES / case),
        "--out",
        str(out_dir),
    ]
    command.extend(extra_args or [])
    completed = subprocess.run(command, capture_output=True, text=True)
    print(f"--- {case} (exit {completed.returncode}) ---")
    print(completed.stdout.strip())
    if completed.stderr.strip():
        print("stderr:", completed.stderr.strip())
    return completed.returncode, out_dir


def load_report(out_dir: Path) -> dict:
    return json.loads((out_dir / "report.json").read_text(encoding="utf-8"))


def image_of(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        return opened.convert("RGB")


def pixel(image: Image.Image, x: int, y: int) -> tuple[int, int, int]:
    return image.getpixel((x, y))


def texel(name: str, x: int, y: int) -> tuple[int, int, int]:
    with Image.open(ROOT / "samples" / "textures" / name) as opened:
        return opened.convert("RGB").getpixel((x, y))


# ---------------------------------------------------------------------------


def main() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    code, base_dir = run_case("base.json", ["--mesh-previews", str(OUT_ROOT / "acceptance" / "base_meshes")])
    base = image_of(base_dir / "preview.png")
    base_report = load_report(base_dir)

    # 需求 2：尺寸一致 + 前层覆盖后层。
    check("R2 尺寸一致", base.size == (240, 160), f"preview={base.size} canvas=240x160")
    front_px = pixel(base, 130, 30)  # front 与 middle 重叠区，front 为最前层
    expected_front = texel("front.png", 10, 14)
    check(
        "R2 重叠区域为前层颜色",
        front_px == expected_front,
        f"preview{front_px} == front.png texel {expected_front}",
    )
    middle_px = pixel(base, 84, 44)  # middle 与 back 重叠，front 不覆盖
    expected_middle = texel("middle.png", 20, 20)
    check(
        "R2 后绘制网格覆盖先绘制区域",
        middle_px == expected_middle,
        f"preview{middle_px} == middle.png texel {expected_middle}",
    )

    # 需求 3：逐个网格阴影预览中，被完全遮挡区域明显更暗；最前层不受影响。
    back_preview = image_of(OUT_ROOT / "acceptance" / "base_meshes" / "000_000_back.png")
    covered = pixel(back_preview, 84, 60)      # back 与 middle 重叠，被 middle 完全覆盖
    uncovered = pixel(back_preview, 20, 100)   # back 自身未被覆盖
    brightness = lambda px: round(0.299 * px[0] + 0.587 * px[1] + 0.114 * px[2], 1)
    check(
        "R3 被遮挡区域明显更暗",
        brightness(covered) < brightness(uncovered),
        f"covered {covered} lum={brightness(covered)} < uncovered {uncovered} lum={brightness(uncovered)}",
    )
    front_preview = image_of(OUT_ROOT / "acceptance" / "base_meshes" / "002_002_front.png")
    front_recolor = pixel(front_preview, 130, 30)
    check(
        "R3 前层自身不被遮挡",
        front_recolor == texel("front.png", 10, 14),
        f"front preview texel {front_recolor} == 原贴图 {texel('front.png', 10, 14)}",
    )
    front_entry = next(e for e in base_report["meshes"] if e["name"] == "front")
    check(
        "R3 报告 occlusion 统计",
        front_entry["occluded_pixels"] == 0 and front_entry["mean_occlusion"] == 0.0,
        f"front occluded_pixels={front_entry['occluded_pixels']} mean={front_entry['mean_occlusion']}",
    )

    # 需求 3：半透明前层，遮挡程度逐像素递增。
    code, dir3 = run_case("partial_occlusion.json", ["--mesh-previews", str(OUT_ROOT / "acceptance" / "partial_meshes")])
    report3 = load_report(dir3)
    base_entry = next(e for e in report3["meshes"] if e["name"] == "base")
    veil_entry = next(e for e in report3["meshes"] if e["name"] == "veil")
    base_shadow = image_of(OUT_ROOT / "acceptance" / "partial_meshes" / "000_000_base.png")
    base_texture = image_of(ROOT / "samples" / "textures" / "back.png")

    def darkening(x_offset: int) -> float:
        """阴影亮度 / 原始贴图亮度；越小表示遮挡越强。"""
        shadow_px = pixel(base_shadow, 10 + x_offset, 30)
        origin_px = base_texture.getpixel((x_offset, 20))
        return round(brightness(shadow_px) / brightness(origin_px), 4)

    ratios = [darkening(6), darkening(47), darkening(87)]
    check(
        "R3 半透明遮挡逐像素递增变暗",
        ratios[0] > ratios[1] > ratios[2],
        f"左 {ratios[0]} > 中 {ratios[1]} > 右 {ratios[2]}（阴影亮度/原贴图亮度）",
    )
    check(
        "R3 半透明前层自身不参与遮挡",
        veil_entry["occluded_pixels"] == 0,
        f"veil occluded_pixels={veil_entry['occluded_pixels']} mean={veil_entry['mean_occlusion']}",
    )
    check(
        "R3 未遮挡像素保持原色（左缘 alpha=0 不压暗）",
        pixel(base_shadow, 10, 40) == base_texture.getpixel((0, 30)),
        f"base shadow texel {pixel(base_shadow, 10, 40)} == 原贴图 {base_texture.getpixel((0, 30))}",
    )
    check(
        "R3 报告统计随遮挡程度变化",
        base_entry["occluded_pixels"] > 0 and 0.0 < base_entry["mean_occlusion"] < 1.0,
        f"base occluded_pixels={base_entry['occluded_pixels']} ratio={base_entry['occluded_pixel_ratio']} mean={base_entry['mean_occlusion']}",
    )

    # 需求 1：缺 layer 的网格不参与合成，报告给出原因。
    code, dir1 = run_case("missing_layer.json")
    report1 = load_report(dir1)
    preview1 = image_of(dir1 / "preview.png")
    entry1 = report1["meshes"][-1]
    check(
        "R1 缺层级网格被跳过",
        entry1["status"] == "skipped" and "缺少必需字段" in entry1["unprocessed_reason"],
        f"status={entry1['status']} reason={entry1['unprocessed_reason']}",
    )
    check(
        "R1 缺层级网格不出现在预览",
        pixel(preview1, 10, 10) == (0x20, 0x28, 0x30) and preview1 == base,
        f"(10,10)={pixel(preview1, 10, 10)} 与基准图一致={preview1 == base}",
    )

    # 需求 4：缺失贴图跳过，其余照常。
    code, dir4 = run_case("missing_texture.json")
    report4 = load_report(dir4)
    preview4 = image_of(dir4 / "preview.png")
    skipped4 = report4["meshes"][1]
    check(
        "R4 缺贴图条目",
        skipped4["status"] == "skipped"
        and "textures/missing_middle.png" in skipped4["unprocessed_reason"],
        f"name={skipped4['name']} reason={skipped4['unprocessed_reason']}",
    )
    check(
        "R4 其余网格正常出现",
        pixel(preview4, 130, 30) == expected_front
        and pixel(preview4, 20, 100) == texel("back.png", 12, 52),
        f"front={pixel(preview4, 130, 30)} back={pixel(preview4, 20, 100)}",
    )
    check("R4 报告 top-level status=partial", report4["status"] == "partial", report4["status"])

    # 需求 5：越界裁剪。
    code, dir5 = run_case("clipped.json")
    report5 = load_report(dir5)
    preview5 = image_of(dir5 / "preview.png")
    entry5 = next(e for e in report5["meshes"] if e["name"] == "front")
    check(
        "R5 标注部分裁剪",
        entry5["status"] == "composited" and entry5["partially_clipped"] is True,
        f"status={entry5['status']} partially_clipped={entry5['partially_clipped']}",
    )
    check(
        "R5 越界像素被裁掉且保留部分正常",
        entry5["placed_rect"] == [220, 16, 240, 112]
        and preview5.size == (240, 160)
        and pixel(preview5, 230, 30) == texel("front.png", 10, 14),
        f"placed_rect={entry5['placed_rect']} size={preview5.size} pixel(230,30)={pixel(preview5, 230, 30)}",
    )
    check(
        "R5 未越界网格不标注裁剪",
        all(not e["partially_clipped"] for e in report5["meshes"] if e["name"] != "front"),
        "其它网格 partially_clipped 均为 false",
    )

    # 需求 6：画布为 0 拒绝整个场景且不产生预览。
    code, dir6 = run_case("canvas_zero.json")
    report6 = load_report(dir6)
    check(
        "R6 画布非法拒绝场景",
        report6["status"] == "rejected"
        and "宽度" in report6.get("reason", "")
        and not (dir6 / "preview.png").exists(),
        f"status={report6['status']} reason={report6.get('reason')} preview存在={(dir6 / 'preview.png').exists()}",
    )
    code, dir6b = run_case("bad_mesh.json")
    report6b = load_report(dir6b)
    preview6b = image_of(dir6b / "preview.png")
    bad6b = next(e for e in report6b["meshes"] if e["name"] == "bad_coord")
    ok6b = [e for e in report6b["meshes"] if e["status"] == "composited"]
    check(
        "R6 个别坐标非法仅跳过该网格",
        bad6b["status"] == "skipped"
        and "坐标" in bad6b["unprocessed_reason"]
        and len(ok6b) == 3
        and pixel(preview6b, 130, 30) == expected_front,
        f"bad_status={bad6b['status']} reason={bad6b['unprocessed_reason']} composited={len(ok6b)}",
    )

    # 需求 7：按层级顺序列出状态，成功条目无未处理原因。
    code, dir7 = run_case("mixed.json")
    report7 = load_report(dir7)
    layers = [e["layer"] for e in report7["meshes"]]
    statuses = [(e["name"], e["status"], e.get("unprocessed_reason")) for e in report7["meshes"]]
    check(
        "R7 按层级顺序与状态",
        layers == sorted(layers)
        and statuses[0] == ("ok_mesh", "composited", None)
        and statuses[1][0] == "missing_mesh" and statuses[1][1] == "skipped"
        and statuses[2][0] == "bad_coord" and statuses[2][1] == "skipped",
        f"layers={layers} statuses={statuses}",
    )
    check(
        "R7 成功条目无未处理原因",
        "unprocessed_reason" not in report7["meshes"][0],
        f"success keys={list(report7['meshes'][0].keys())}",
    )

    # 需求 8：重复运行逐像素一致、报告完全一致。
    code_a, dir_a = run_case("base.json")
    code_b, dir_b = run_case("base.json")
    png_a = (dir_a / "preview.png").read_bytes()
    png_b = (dir_b / "preview.png").read_bytes()
    report_a = (dir_a / "report.json").read_text(encoding="utf-8")
    report_b = (dir_b / "report.json").read_text(encoding="utf-8")
    check(
        "R8 两次运行 PNG 逐像素一致",
        png_a == png_b,
        f"SHA 字节一致={png_a == png_b} size={len(png_a)}B",
    )
    check("R8 两次运行报告完全一致", report_a == report_b, f"报告长度={len(report_a)} 字符")

    # 汇总
    failures = [item for item in RESULTS if not item[1]]
    print("\n===== 汇总 =====")
    print(f"通过 {len(RESULTS) - len(failures)} / {len(RESULTS)}")
    if failures:
        for name, _, detail in failures:
            print(f"失败: {name} -> {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
