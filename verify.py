#!/usr/bin/env python3
"""对 sprite_preview.py 做端到端验证，覆盖需求 (1)-(12)。

运行：python -I verify.py
退出码 0 表示全部检查通过。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).parent
TOOL = ROOT / "sprite_preview.py"
DATA = ROOT / "sample_data"
WORK = ROOT / "verify_work"
REPORT = "report.json"

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run_tool(atlas: Path, config: Path, out: Path) -> subprocess.CompletedProcess:
    # -I 会忽略 PYTHONIOENCODING，故按 UTF-8 解码并对历史编码输出容错。
    return subprocess.run(
        [sys.executable, "-I", str(TOOL), str(atlas), str(config), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def expect_fatal(label: str, atlas: Path, config: Path) -> None:
    out = WORK / ("fatal_" + label)
    if out.exists():
        shutil.rmtree(out)
    r = run_tool(atlas, config, out)
    check(f"{label}：退出码非零", r.returncode != 0, f"rc={r.returncode}, stderr={r.stderr.strip()}")
    check(f"{label}：有可读错误提示", bool(r.stderr.strip()), r.stderr.strip())
    check(f"{label}：不生成任何预览与报告", not out.exists() or not any(out.iterdir()))


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    atlas = DATA / "atlas.png"
    config = DATA / "regions.json"
    atlas_hash_before = file_hash(atlas)

    # ---------- (12) --help ----------
    r = subprocess.run(
        [sys.executable, "-I", str(TOOL), "--help"], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    help_text = r.stdout
    check("(12) --help 退出码为 0", r.returncode == 0)
    check("(12) --help 含三个位置参数说明",
          all(k in help_text for k in ("atlas", "config", "output_dir")), help_text)
    check("(12) --help 含示例命令", "python sprite_preview.py" in help_text)

    # ---------- (1) 损坏 PNG / RGB 无 alpha ----------
    expect_fatal("损坏图集", DATA / "broken.png", config)
    expect_fatal("RGB无alpha图集", DATA / "rgb_no_alpha.png", config)

    # ---------- (11) 配置无法解析 / 非数组 ----------
    expect_fatal("损坏JSON配置", atlas, DATA / "broken_config.json")
    expect_fatal("对象型配置", atlas, DATA / "object_config.json")

    # ---------- 主运行：(2)(3)(4)(8)(9) ----------
    out = WORK / "main_out"
    r = run_tool(atlas, config, out)
    check("主运行退出码为 0", r.returncode == 0, r.stderr)
    report = json.loads((out / REPORT).read_text(encoding="utf-8"))

    # (9) 报告结构与原始顺序
    cfg = json.loads(config.read_text(encoding="utf-8"))
    check("(9) 报告含图集尺寸", report["atlas"]["width"] == 64 and report["atlas"]["height"] == 48)
    check("(9) 区域数组长度与配置一致", len(report["regions"]) == len(cfg))
    check("(9) 区域顺序与配置一致",
          [e["name"] for e in report["regions"]] == [e["name"] for e in cfg])
    success_items = [e for e in report["regions"] if e["status"] in ("success", "fully_transparent")]
    check("(9) 成功项含预览文件名", all(e.get("preview_file", "").endswith(".png") for e in success_items))

    status_by_name = {}
    for e in report["regions"]:
        status_by_name.setdefault(e["name"], []).append(e["status"])

    # (2) 配置无效
    check("(2) 缺字段区域标记 config_invalid",
          status_by_name.get("missing_width") == ["config_invalid"])
    check("(2) 字符串坐标区域标记 config_invalid",
          status_by_name.get("string_coord") == ["config_invalid"])
    check("(2) 无效区域无预览文件", not (out / "missing_width.png").exists()
          and not (out / "string_coord.png").exists())

    # (3) 负坐标 / 越界
    check("(3) 负坐标标记 invalid_coordinates",
          status_by_name.get("negative") == ["invalid_coordinates"])
    check("(3) 越界标记 out_of_bounds",
          status_by_name.get("out_of_bounds") == ["out_of_bounds"])
    check("(3) 两类区域均无预览文件",
          not (out / "negative.png").exists() and not (out / "out_of_bounds.png").exists())

    # (4) 同名覆盖
    check("(4) 同名首项标记 superseded", status_by_name["dup"] == ["superseded", "success"])
    check("(4) duplicate_names 含 dup", "dup" in report["duplicate_names"])
    check("(4) 仅生成一份 dup.png", (out / "dup.png").exists())
    dup_img = Image.open(out / "dup.png").convert("RGBA")
    # 后一项是 (16,24) 的黄色不透明块，预览内容中心 (13,13) 应为黄色。
    check("(4) dup.png 内容对应最后一项（黄色块）",
          dup_img.getpixel((13, 13)) == (230, 200, 40, 255), str(dup_img.getpixel((13, 13))))

    # (5) 预览文件集合
    expected_files = {"hero.png", "ghost.png", "dup.png", "edge.png", "pad_test.png", REPORT}
    check("(5) 输出文件集合与有效区域一致",
          {p.name for p in out.iterdir()} == expected_files,
          str({p.name for p in out.iterdir()}))

    # (6) 棋盘格：全透明区域 ghost 的每个像素都应符合 8px 交替公式
    ghost = Image.open(out / "ghost.png").convert("RGBA")
    board_colors = {(0xCC, 0xCC, 0xCC, 255), (0xFF, 0xFF, 0xFF, 255)}
    board_ok = all(
        ghost.getpixel((x, y)) in board_colors
        and ghost.getpixel((x, y)) == (
            (0xCC, 0xCC, 0xCC, 255) if ((x // 8) + (y // 8)) % 2 == 0
            else (0xFF, 0xFF, 0xFF, 255))
        for y in range(ghost.height) for x in range(ghost.width)
    )
    check("(6) 棋盘格按 8px 块在 0xCC/0xFF 间交替", board_ok)

    # (7) 尺寸与内边距
    check("(7) 预览宽高 = 区域宽高 + 16", ghost.size == (28, 28), str(ghost.size))
    hero = Image.open(out / "hero.png").convert("RGBA")
    check("(7) 内容左上角位于 (8,8)（红色不透明像素）",
          hero.getpixel((8, 8)) == (220, 40, 40, 255), str(hero.getpixel((8, 8))))
    check("(7) 内边距为棋盘格",
          hero.getpixel((0, 0)) in ((0xCC, 0xCC, 0xCC, 255), (0xFF, 0xFF, 0xFF, 255)))
    # 半透明边缘被平铺为不透明像素（与棋盘格合成），可直接检视溢色。
    semi = hero.getpixel((8 + 15, 8))
    check("(6/7) 半透明像素与棋盘格合成后 alpha=255", semi[3] == 255, str(semi))

    # (8) 全透明
    check("(8) ghost 状态为 fully_transparent",
          status_by_name.get("ghost") == ["fully_transparent"])
    inner_has_content = any(
        ghost.getpixel((x, y)) not in ((0xCC, 0xCC, 0xCC, 255), (0xFF, 0xFF, 0xFF, 255))
        for y in range(8, 20) for x in range(8, 20)
    )
    check("(8) 全透明预览无非透明内容像素", not inner_has_content)

    # ---------- (5) 重跑不残留旧文件 ----------
    r2 = run_tool(atlas, config, out)
    check("(5) 重跑退出码为 0", r2.returncode == 0)
    check("(5) 重跑后文件集合不变",
          {p.name for p in out.iterdir()} == expected_files)

    subset = WORK / "subset.json"
    subset.write_text(json.dumps([
        {"name": "hero", "x": 2, "y": 2, "width": 16, "height": 16},
        {"name": "ghost", "x": 22, "y": 2, "width": 12, "height": 12},
    ]), encoding="utf-8")
    r3 = run_tool(atlas, subset, out)
    check("(5) 子集运行退出码为 0", r3.returncode == 0)
    check("(5) 旧预览被清理，集合与本次有效区域一致",
          {p.name for p in out.iterdir()} == {"hero.png", "ghost.png", REPORT},
          str({p.name for p in out.iterdir()}))
    # 恢复完整输出，便于人工查看。
    run_tool(atlas, config, out)

    # ---------- (10) 目录自动创建 + 单项写入失败不影响其他区域 ----------
    nested = WORK / "a" / "b" / "new_out"
    r4 = run_tool(atlas, config, nested)
    check("(10) 不存在的嵌套输出目录自动创建", r4.returncode == 0 and nested.is_dir())

    fail_out = WORK / "write_fail"
    fail_out.mkdir(parents=True)
    # 预置同名目录，使 hero.png 的文件写入必然失败（跨平台稳定复现）。
    (fail_out / "hero.png").mkdir()
    r5 = run_tool(atlas, config, fail_out)
    check("(10) 存在写入失败时整体仍退出 0", r5.returncode == 0, r5.stderr)
    rep5 = json.loads((fail_out / REPORT).read_text(encoding="utf-8"))
    hero_entry = next(e for e in rep5["regions"] if e["name"] == "hero")
    check("(10) 写入失败区域标记 write_failed", hero_entry["status"] == "write_failed",
          hero_entry.get("reason", ""))
    check("(10) 其他区域预览照常生成", (fail_out / "edge.png").exists()
          and (fail_out / "ghost.png").exists())
    shutil.rmtree(fail_out / "hero.png")  # 清理便于重跑

    # 图集全程只读、未被改动。
    check("(10) 原图集字节未发生变化", file_hash(atlas) == atlas_hash_before)

    print(f"\n共 {checks} 项检查，失败 {len(failures)} 项。")
    if failures:
        print("失败项：" + "；".join(failures))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
