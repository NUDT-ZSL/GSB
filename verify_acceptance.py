# -*- coding: utf-8 -*-
"""验收脚本：逐条核对需求 1~11。全部通过时退出码 0，否则 1。

用法:
    python verify_acceptance.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

import sprite_compositor as sc  # noqa: E402

RESULTS = []


def check(title, ok, detail=""):
    RESULTS.append((title, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {title}" + (f"  -- {detail}" if detail else ""))


def write_manifest(directory: Path, manifest: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def make_sprite(path: Path, size, color, shape="rect"):
    """生成确定性测试精灵：矩形实心或居中圆形（带透明角）。"""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    if shape == "circle":
        w, h = size
        draw.ellipse((w // 4, h // 4, w * 3 // 4, h * 3 // 4), fill=color)
    else:
        draw.rectangle((0, 0, size[0] - 1, size[1] - 1), fill=color)
    img.save(path)
    return path


def base_manifest(canvas, out_dir, sprites):
    return {"canvas": canvas, "output_dir": str(out_dir), "sprites": sprites}


def sprite_cfg(name, source, rect, shadow=None):
    return {
        "name": name,
        "source": str(source),
        "rect": rect,
        "shadow": shadow or {"offset_x": 0, "offset_y": 0, "blur_radius": 0},
    }


def main():
    workspace = Path(tempfile.mkdtemp(prefix="sprite_verify_"))
    print(f"验收工作区: {workspace}\n")
    try:
        assets = workspace / "assets"
        assets.mkdir()
        red = make_sprite(assets / "red.png", (100, 80), (200, 30, 30, 255))
        green = make_sprite(assets / "green.png", (120, 90), (30, 180, 60, 255))
        blue = make_sprite(assets / "blue.png", (90, 110), (40, 60, 220, 255))
        circle = make_sprite(assets / "circle.png", (100, 100), (200, 30, 30, 255), "circle")
        tiny = make_sprite(assets / "tiny.png", (40, 40), (200, 30, 30, 255))

        # ---- 1. 基本流程：3 个精灵，预览尺寸 == 画布，报告顺序 == 清单顺序 ----
        case = workspace / "case01"
        mpath = write_manifest(case, base_manifest(
            {"width": 400, "height": 300}, case / "out",
            [
                sprite_cfg("s1", red, {"x": 10, "y": 10, "width": 100, "height": 80}),
                sprite_cfg("s2", green, {"x": 120, "y": 20, "width": 120, "height": 90}),
                sprite_cfg("s3", blue, {"x": 250, "y": 30, "width": 90, "height": 110}),
            ],
        ))
        report = sc.run(mpath)
        preview = Image.open(case / "out" / "preview.png")
        order_ok = [e["name"] for e in report["sprites"]] == ["s1", "s2", "s3"]
        check("1 基本流程：预览尺寸与画布一致、报告顺序与清单相同",
              preview.size == (400, 300) and order_ok and report["completed_count"] == 3)

        # ---- 2. 部分越界矩形：裁掉溢出、保留画布内内容，状态已完成 ----
        case = workspace / "case02"
        mpath = write_manifest(case, base_manifest(
            {"width": 200, "height": 150}, case / "out",
            [sprite_cfg("clipped", red, {"x": 150, "y": 20, "width": 100, "height": 80})],
        ))
        report = sc.run(mpath)
        px = Image.open(case / "out" / "preview.png").load()
        inside_visible = px[199, 60][3] == 255 and px[199, 60][:3] == (200, 30, 30)
        check("2 越界矩形：仅显示画布内部分且报告已完成",
              report["sprites"][0]["status"] == "completed" and inside_visible)

        # ---- 3. 阴影贴合 alpha 轮廓，透明角落无矩形暗块 ----
        case = workspace / "case03"
        mpath = write_manifest(case, base_manifest(
            {"width": 300, "height": 300}, case / "out",
            [sprite_cfg("ball", circle, {"x": 50, "y": 50, "width": 100, "height": 100},
                        {"offset_x": 20, "offset_y": 20, "blur_radius": 5})],
        ))
        report = sc.run(mpath)
        px = Image.open(case / "out" / "preview.png").load()
        corner_clear = px[52, 52][3] == 0 and px[68, 68][3] == 0  # 矩形角+偏移处仍全透明
        shadow_present = px[98, 132][3] > 0  # 圆下缘外侧偏下处出现柔和阴影
        sprite_on_top = px[100, 100][:3] == (200, 30, 30)  # 精灵本体压在阴影上
        check("3 阴影贴合轮廓、透明角落无暗块",
              corner_clear and shadow_present and sprite_on_top,
              f"corner_alpha={px[52, 52][3]},{px[68, 68][3]} shadow_alpha={px[98, 132][3]}")

        # ---- 4. 源图缺失：记未完成并写明原因，其余继续 ----
        case = workspace / "case04"
        mpath = write_manifest(case, base_manifest(
            {"width": 300, "height": 200}, case / "out",
            [
                sprite_cfg("ok1", red, {"x": 0, "y": 0, "width": 100, "height": 80}),
                sprite_cfg("missing", case / "no_such.png", {"x": 0, "y": 0, "width": 50, "height": 50}),
                sprite_cfg("ok2", green, {"x": 110, "y": 0, "width": 120, "height": 90}),
            ],
        ))
        report = sc.run(mpath)
        e = report["sprites"]
        check("4 源图缺失：该项未完成有原因，其余完成，预览正常生成",
              (case / "out" / "preview.png").is_file()
              and e[0]["status"] == "completed" and e[2]["status"] == "completed"
              and e[1]["status"] == "failed" and "缺失" in (e[1]["reason"] or ""))

        # ---- 5. 源图小于摆放矩形：记未完成、不放大合成 ----
        case = workspace / "case05"
        mpath = write_manifest(case, base_manifest(
            {"width": 300, "height": 200}, case / "out",
            [
                sprite_cfg("too_small", tiny, {"x": 20, "y": 20, "width": 100, "height": 100}),
                sprite_cfg("ok", green, {"x": 150, "y": 20, "width": 120, "height": 90}),
            ],
        ))
        report = sc.run(mpath)
        px = Image.open(case / "out" / "preview.png").load()
        region_empty = all(px[xx, yy][3] == 0 for xx, yy in [(30, 30), (60, 60), (110, 110)])
        e = report["sprites"]
        check("5 源图小于矩形：该项未完成且预览不含该精灵，其他不受影响",
              e[0]["status"] == "failed" and "小于摆放矩形" in (e[0]["reason"] or "")
              and e[1]["status"] == "completed" and region_empty)

        # ---- 6. 非法参数：宽为 0 / 偏移为负，记未完成并说明 ----
        case = workspace / "case06"
        mpath = write_manifest(case, base_manifest(
            {"width": 300, "height": 200}, case / "out",
            [
                sprite_cfg("bad_w", red, {"x": 0, "y": 0, "width": 0, "height": 80}),
                sprite_cfg("bad_off", red, {"x": 0, "y": 0, "width": 100, "height": 80},
                           {"offset_x": -3, "offset_y": 0, "blur_radius": 0}),
                sprite_cfg("ok", green, {"x": 150, "y": 20, "width": 120, "height": 90}),
            ],
        ))
        report = sc.run(mpath)
        e = report["sprites"]
        check("6 非法参数：宽为0/偏移为负均记未完成且原因为参数非法",
              e[0]["status"] == "failed" and "参数非法" in (e[0]["reason"] or "")
              and e[1]["status"] == "failed" and "参数非法" in (e[1]["reason"] or "")
              and e[2]["status"] == "completed")

        # ---- 7. 画布短边超 4096：整体拒绝，不生成任何输出 ----
        case = workspace / "case07"
        out_dir = case / "out"
        mpath = write_manifest(case, base_manifest(
            {"width": 5000, "height": 5000}, out_dir,
            [sprite_cfg("s", red, {"x": 0, "y": 0, "width": 100, "height": 80})],
        ))
        try:
            sc.run(mpath)
            refused, msg = False, "未拒绝"
        except sc.ManifestError as exc:
            refused, msg = True, str(exc)
        no_output = not out_dir.exists() or not any(out_dir.iterdir())
        check("7 画布超限：整体拒绝且输出目录无新增文件",
              refused and "超过上限" in msg and no_output, msg)

        # ---- 8a. 输出目录不存在时自动创建 ----
        case = workspace / "case08"
        new_dir = case / "fresh" / "nested"
        mpath = write_manifest(case, base_manifest(
            {"width": 200, "height": 150}, new_dir,
            [sprite_cfg("s", red, {"x": 0, "y": 0, "width": 100, "height": 80})],
        ))
        report = sc.run(mpath)
        check("8a 新目录自动创建并写出结果",
              (new_dir / "preview.png").is_file() and (new_dir / "report.json").is_file())

        # ---- 8b. 输出目录不可写（路径被同名文件占用）：整体报错且无预览 ----
        case = workspace / "case08b"
        case.mkdir()
        blocker = case / "blocked"
        blocker.write_text("i am a file", encoding="utf-8")  # 同名文件使目录无法创建
        mpath = write_manifest(case, base_manifest(
            {"width": 200, "height": 150}, blocker,
            [sprite_cfg("s", red, {"x": 0, "y": 0, "width": 100, "height": 80})],
        ))
        try:
            sc.run(mpath)
            failed, msg = False, "未报错"
        except sc.ManifestError as exc:
            failed, msg = True, str(exc)
        check("8b 目录不可写：整体报错且不产生预览",
              failed and not (blocker / "preview.png").exists(), msg)

        # ---- 9. 单精灵合成异常：仅该项未完成，其余正常 ----
        case = workspace / "case09"
        mpath = write_manifest(case, base_manifest(
            {"width": 300, "height": 200}, case / "out",
            [
                sprite_cfg("ok1", red, {"x": 0, "y": 0, "width": 100, "height": 80}),
                sprite_cfg("glitch", green, {"x": 110, "y": 0, "width": 120, "height": 90}),
                sprite_cfg("ok2", blue, {"x": 0, "y": 90, "width": 90, "height": 110}),
            ],
        ))
        original = sc.process_sprite

        def spy(canvas, cfg, manifest_dir, canvas_size):
            if cfg.get("name") == "glitch":
                raise RuntimeError("模拟合成阶段崩溃")
            return original(canvas, cfg, manifest_dir, canvas_size)

        sc.process_sprite = spy
        try:
            report = sc.run(mpath)
        finally:
            sc.process_sprite = original
        e = report["sprites"]
        check("9 单精灵合成异常：仅该项未完成，其余完成",
              e[0]["status"] == "completed" and e[2]["status"] == "completed"
              and e[1]["status"] == "failed" and "合成过程异常" in (e[1]["reason"] or "")
              and report["completed_count"] == 2)

        # ---- 10. 报告字段齐全、顺序一致、未完成路径为 null、完成数正确 ----
        case = workspace / "case10"
        mpath = write_manifest(case, base_manifest(
            {"width": 300, "height": 200}, case / "out",
            [
                sprite_cfg("a", red, {"x": 0, "y": 0, "width": 100, "height": 80}),
                sprite_cfg("b", case / "missing.png", {"x": 0, "y": 0, "width": 50, "height": 50}),
                sprite_cfg("c", green, {"x": 110, "y": 0, "width": 120, "height": 90}),
            ],
        ))
        sc.run(mpath)
        report = json.loads((case / "out" / "report.json").read_text(encoding="utf-8"))
        entries = report["sprites"]
        fields_ok = all(set(e) >= {"name", "status", "preview", "reason"} for e in entries)
        order_ok = [e["name"] for e in entries] == ["a", "b", "c"]
        paths_ok = (entries[0]["preview"] == "preview.png" and entries[2]["preview"] == "preview.png"
                    and entries[1]["preview"] is None and entries[1]["reason"])
        count_ok = report["completed_count"] == 2 and report["total_count"] == 3
        check("10 报告字段齐全、顺序一致、未完成路径为null、完成数正确",
              fields_ok and order_ok and paths_ok and count_ok)

        # ---- 11. 重复运行：报告条目与状态一致，预览像素一致，无多余条目 ----
        case = workspace / "case11"
        mpath = write_manifest(case, base_manifest(
            {"width": 300, "height": 200}, case / "out",
            [
                sprite_cfg("r1", red, {"x": 0, "y": 0, "width": 100, "height": 80},
                           {"offset_x": 6, "offset_y": 6, "blur_radius": 4}),
                sprite_cfg("r2", case / "missing.png", {"x": 0, "y": 0, "width": 50, "height": 50}),
                sprite_cfg("r3", circle, {"x": 120, "y": 40, "width": 100, "height": 100},
                           {"offset_x": 8, "offset_y": 8, "blur_radius": 6}),
            ],
        ))
        sc.run(mpath)
        report1 = (case / "out" / "report.json").read_bytes()
        preview1 = (case / "out" / "preview.png").read_bytes()
        files1 = sorted(p.name for p in (case / "out").iterdir())
        sc.run(mpath)
        report2 = (case / "out" / "report.json").read_bytes()
        preview2 = (case / "out" / "preview.png").read_bytes()
        files2 = sorted(p.name for p in (case / "out").iterdir())
        check("11 重复运行：报告与预览逐字节一致、无多余文件",
              report1 == report2 and preview1 == preview2 and files1 == files2 == ["preview.png", "report.json"])

    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    failed = [t for t, ok, _ in RESULTS if not ok]
    print(f"\n结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("未通过项:")
        for t in failed:
            print(f"  - {t}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
