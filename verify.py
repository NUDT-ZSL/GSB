"""验收脚本: 逐条验证需求 1-10, 全部本地离线运行。

用法:
    python verify.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
PACKER = os.path.join(ROOT, "atlas_packer.py")
GEN = os.path.join(ROOT, "generate_samples.py")
SAMPLE = os.path.join(ROOT, "sample_data")
OUT_BASE = os.path.join(ROOT, "verify_out")

RESULTS: list[tuple[int, bool, str]] = []


def check(req: int, ok: bool, detail: str) -> None:
    RESULTS.append((req, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {detail}")


def run_packer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, PACKER, *args],
        capture_output=True, text=True, encoding="utf-8",
    )


def fresh_out(name: str) -> str:
    path = os.path.join(OUT_BASE, name)
    shutil.rmtree(path, ignore_errors=True)
    return path


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def atlas_colors(img: Image.Image) -> set:
    """图集中出现过的 RGBA 颜色集合。"""
    return {color for _count, color in img.getcolors(img.width * img.height)}


def load_manifest(out_dir: str) -> dict:
    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def sprite_entry(manifest: dict, name: str) -> dict:
    return next(s for s in manifest["sprites"] if s["name"] == name)


def main() -> int:
    print("== 生成确定性样例 ==")
    subprocess.run([sys.executable, GEN, SAMPLE], check=True, capture_output=True)
    shutil.rmtree(OUT_BASE, ignore_errors=True)
    os.makedirs(OUT_BASE)

    cfg = os.path.join(SAMPLE, "config.json")
    cfg_small = os.path.join(SAMPLE, "config_small.json")
    cfg_missing = os.path.join(SAMPLE, "config_missing.json")
    cfg_invalid = os.path.join(SAMPLE, "config_invalid.json")

    # ---------- 需求 1: 溢出不写入, 清单矩形为 null ----------
    print("需求 1: 小画布下溢出小图不写入图集, 清单落位矩形为 null")
    out1 = fresh_out("req1")
    r = run_packer("--config", cfg_small, "--out", out1)
    m = load_manifest(out1)
    big = sprite_entry(m, "big")
    red = sprite_entry(m, "red")
    atlas = Image.open(os.path.join(out1, "atlas.png")).convert("RGBA")
    purple_present = (128, 0, 128, 255) in atlas_colors(atlas)
    check(1, r.returncode == 0, "打包进程退出码为 0")
    check(1, big["rect"] is None and big["status"] == "overflow",
          f"big 落位矩形为 null 且状态 overflow (实际: rect={big['rect']}, status={big['status']})")
    check(1, red["status"] == "placed" and not purple_present,
          "red 正常落位, 图集中不存在 big 的紫色像素")

    # ---------- 需求 2/3/5/6: 主配置一次运行, 多项检查 ----------
    print("需求 2/3/5/6: 主配置打包 (bleed=4, 含旋转与半透明小图)")
    out2 = fresh_out("req2356")
    r = run_packer("--config", cfg, "--out", out2)
    m = load_manifest(out2)
    atlas = Image.open(os.path.join(out2, "atlas.png")).convert("RGBA")
    check(2, r.returncode == 0, "打包进程退出码为 0")

    # 需求 2: 出血扩展颜色与边缘一致, 且不侵占相邻落位矩形
    bleed_ok = True
    neighbor_ok = True
    for s in m["sprites"]:
        if s["status"] != "placed":
            continue
        rect = s["rect"]
        x, y, w, h = rect["x"], rect["y"], rect["width"], rect["height"]
        # 上侧出血区每个像素 == 小图顶行对应列像素
        for dx in range(w):
            edge = atlas.getpixel((x + dx, y))
            for b in range(1, 5):
                if atlas.getpixel((x + dx, y - b)) != edge:
                    bleed_ok = False
        for dy in range(h):
            edge = atlas.getpixel((x, y + dy))
            for b in range(1, 5):
                if atlas.getpixel((x - b, y + dy)) != edge:
                    bleed_ok = False
    # 相邻落位矩形内部像素未被其他小图出血覆盖: 与源图逐像素一致
    src_cache = {}
    for s in m["sprites"]:
        if s["status"] != "placed":
            continue
        src = Image.open(os.path.join(SAMPLE, s["src"])).convert("RGBA")
        if s["rotated"]:
            src = src.transpose(Image.Transpose.ROTATE_270)
        src_cache[s["name"]] = src
        rect = s["rect"]
        region = atlas.crop((rect["x"], rect["y"],
                             rect["x"] + rect["width"], rect["y"] + rect["height"]))
        if region.tobytes() != src.tobytes():
            neighbor_ok = False
    check(2, bleed_ok, "各落位矩形外扩 4 像素出血区颜色与小图边缘一致")
    check(2, neighbor_ok, "各落位矩形内部像素与源图一致, 未被相邻出血覆盖")

    # 需求 3: 旋转小图宽高互换且方向正确
    green = sprite_entry(m, "green")
    grect = green["rect"]
    swapped = (grect["width"], grect["height"]) == (40, 24)  # 源 24x40 顺时针转 90°
    # 源图顶部 8 行为亮绿, 顺时针 90° 后亮绿应在右侧
    right_px = atlas.getpixel((grect["x"] + grect["width"] - 1, grect["y"] + 12))
    left_px = atlas.getpixel((grect["x"], grect["y"] + 12))
    oriented = right_px == (0, 255, 0, 255) and left_px == (0, 128, 0, 255)
    check(3, green["rotated"] and swapped,
          f"green 清单宽高互换为 40x24 (实际: {grect['width']}x{grect['height']})")
    check(3, oriented, "图集中 green 亮绿边位于右侧, 顺时针旋转方向正确")

    # 需求 5: 透明度保留
    fade = sprite_entry(m, "fade")
    frect = fade["rect"]
    fx, fy = frect["x"], frect["y"]
    corner_a = atlas.getpixel((fx, fy))[3]            # 源角像素 alpha=0
    center_a = atlas.getpixel((fx + 10, fy + 10))[3]  # 源中心 alpha=255
    bleed_a = atlas.getpixel((fx - 4, fy - 4))[3]     # 出血区复制角像素, alpha 应为 0
    check(5, fade["has_alpha"] is True and corner_a == 0 and center_a == 255,
          f"fade 区域角 alpha={corner_a}, 中心 alpha={center_a}, 清单 has_alpha=true")
    check(5, bleed_a == 0, f"fade 出血扩展区保持透明 (alpha={bleed_a}), 未被填为不透明")
    red_e = sprite_entry(m, "red")
    check(5, red_e["has_alpha"] is False, "不透明小图 red 清单 has_alpha=false")

    # 需求 6: 清单字段齐全, 画布尺寸与配置一致
    fields_ok = (
        m["canvas"] == {"width": 128, "height": 128}
        and all(
            set(s) >= {"name", "src", "rect", "rotated", "status", "has_alpha"}
            for s in m["sprites"]
        )
    )
    check(6, fields_ok, "清单含画布尺寸及每项 name/src/rect/rotated/status/has_alpha")

    # ---------- 需求 4: 缺失源不影响其余小图 ----------
    print("需求 4: 缺失源小图标记未处理, 其余继续打包")
    out4 = fresh_out("req4")
    r = run_packer("--config", cfg_missing, "--out", out4)
    m4 = load_manifest(out4)
    ghost = sprite_entry(m4, "ghost")
    others_ok = all(
        sprite_entry(m4, n)["status"] == "placed" and sprite_entry(m4, n)["rect"]
        for n in ("red", "blue")
    )
    atlas4 = Image.open(os.path.join(out4, "atlas.png")).convert("RGBA")
    colors4 = atlas_colors(atlas4)
    red_present = (255, 0, 0, 255) in colors4
    blue_present = (0, 0, 255, 255) in colors4
    check(4, r.returncode == 0 and ghost["status"] == "missing_source" and ghost["rect"] is None,
          f"ghost 状态 missing_source, 矩形为 null (退出码 {r.returncode})")
    check(4, others_ok and red_present and blue_present,
          "red/blue 均落位并出现在图集 PNG 中")

    # 需求 6 补充: 两类未处理状态可区分
    statuses = {ghost["status"], sprite_entry(load_manifest(out1), "big")["status"]}
    check(6, statuses == {"missing_source", "overflow"},
          "missing_source 与 overflow 两种未处理状态可区分")

    # ---------- 需求 7: 非法配置明确退出, 不产生残缺图集 ----------
    print("需求 7: 非法配置明确报错且输出目录无图集 PNG")
    out7 = fresh_out("req7")
    r = run_packer("--config", cfg_invalid, "--out", out7)
    atlas_exists = os.path.exists(os.path.join(out7, "atlas.png"))
    check(7, r.returncode != 0 and "canvas" in r.stderr,
          f"退出码 {r.returncode}, 错误信息: {r.stderr.strip()}")
    check(7, not atlas_exists, "输出目录下不存在图集 PNG")

    # ---------- 需求 8: 重复运行逐字节一致 ----------
    print("需求 8: 重复运行结果逐字节一致")
    out8a, out8b = fresh_out("req8a"), fresh_out("req8b")
    run_packer("--config", cfg, "--out", out8a)
    run_packer("--config", cfg, "--out", out8b)
    h1 = sha256(os.path.join(out8a, "atlas.png"))
    h2 = sha256(os.path.join(out8b, "atlas.png"))
    with open(os.path.join(out8a, "manifest.json"), encoding="utf-8") as f:
        t1 = f.read()
    with open(os.path.join(out8b, "manifest.json"), encoding="utf-8") as f:
        t2 = f.read()
    check(8, h1 == h2, f"两次图集 PNG 哈希相同 ({h1[:16]}...)")
    check(8, t1 == t2, "两次清单 JSON 文本相同")

    # ---------- 需求 9: 输出目录不可写 ----------
    print("需求 9: 输出目录只读时非零退出且源文件不被修改")
    out9 = fresh_out("req9")
    os.makedirs(out9)
    srcs = [os.path.join(SAMPLE, "sprites", f) for f in os.listdir(os.path.join(SAMPLE, "sprites"))]
    srcs.append(cfg)
    before = {p: sha256(p) for p in srcs}
    user = os.environ.get("USERNAME", "")
    subprocess.run(["icacls", out9, "/deny", f"{user}:(OI)(CI)(W)"],
                   capture_output=True, check=True)
    try:
        r = run_packer("--config", cfg, "--out", out9)
    finally:
        subprocess.run(["icacls", out9, "/remove:d", user],
                       capture_output=True, check=True)
    after = {p: sha256(p) for p in srcs}
    check(9, r.returncode != 0 and r.stderr.strip() != "",
          f"退出码 {r.returncode}, 提示: {r.stderr.strip()}")
    check(9, not os.path.exists(os.path.join(out9, "atlas.png")),
          "只读目录下未生成图集 PNG")
    check(9, before == after, "源小图与配置文件哈希不变")

    # ---------- 需求 10: 命令行参数 ----------
    print("需求 10: 参数缺失给出用法说明, 合法参数正常产出")
    r_noargs = run_packer()
    check(10, r_noargs.returncode != 0 and "usage" in (r_noargs.stderr + r_noargs.stdout).lower(),
          f"无参数退出码 {r_noargs.returncode}, 输出含用法说明")
    out10 = fresh_out("req10")
    r_ok = run_packer("--config", cfg, "--out", out10)
    check(10, r_ok.returncode == 0
          and os.path.exists(os.path.join(out10, "atlas.png"))
          and os.path.exists(os.path.join(out10, "manifest.json")),
          "合法参数运行生成图集 PNG 与清单 JSON")

    # ---------- 汇总 ----------
    failed = [x for x in RESULTS if not x[1]]
    print(f"\n== 汇总: {len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过 ==")
    if failed:
        for req, _ok, detail in failed:
            print(f"  未通过[需求{req}]: {detail}")
        return 1
    print("全部验收项通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
