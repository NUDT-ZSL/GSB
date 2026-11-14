#!/usr/bin/env python3
"""验证脚本：针对需求 1~12 逐条构造确定性样例并核对，输出验证记录。"""
import json
import os
import shutil
import struct
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from composite import composite  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "checks_workspace")

RESULTS = []


def check(no, name, ok, detail=""):
    RESULTS.append((no, name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] 核对{no}: {name}" + (f" — {detail}" if detail else ""))


def reset():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)


def save_img(name, size, color):
    Image.new("RGBA", size, color).save(os.path.join(WORK, name))


def run(manifest, tag=""):
    mpath = os.path.join(WORK, f"manifest{tag}.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    out = os.path.join(WORK, f"preview{tag}.png")
    rep = os.path.join(WORK, f"report{tag}.json")
    report = composite(mpath, out, rep)
    return out, rep, report


def png_chunk_types(path):
    types = []
    with open(path, "rb") as f:
        data = f.read()
    pos = 8  # 跳过 PNG 签名
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8].decode("ascii")
        types.append(ctype)
        pos += 12 + length
    return types


def main():
    reset()

    # ---- 核对1: 画布 800x600 -> 输出 800x600 ----
    save_img("bg.png", (800, 600), (200, 220, 240, 255))
    out, _, rep = run({"width": 800, "height": 600, "layers": [
        {"file": "bg.png", "mode": "normal", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 800, "height": 600}]}, "_1")
    with Image.open(out) as im:
        check(1, "清单800x600时输出为800x600", im.size == (800, 600), f"实际{im.size}")

    # ---- 核对2: overlay 模式不参与合成并在报告中体现 ----
    save_img("ov.png", (100, 100), (255, 0, 0, 255))
    out, _, rep = run({"width": 800, "height": 600, "layers": [
        {"file": "bg.png", "mode": "normal", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 800, "height": 600},
        {"file": "ov.png", "mode": "overlay", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 100, "height": 100}]}, "_2")
    ok = (rep["successCount"] == 1 and rep["failedCount"] == 1
          and any("overlay" in e["reason"] for e in rep["errors"]))
    check(2, "overlay模式图层未合成且报告列明", ok, json.dumps(rep["errors"], ensure_ascii=False))

    # ---- 核对3: opacity=1.5 的图层被跳过，输出与无该图层相同 ----
    save_img("badop.png", (100, 100), (0, 0, 255, 255))
    base_layers = [{"file": "bg.png", "mode": "normal", "opacity": 1.0,
                    "offsetX": 0, "offsetY": 0, "width": 800, "height": 600}]
    out_with, _, rep3 = run({"width": 800, "height": 600, "layers": base_layers + [
        {"file": "badop.png", "mode": "normal", "opacity": 1.5,
         "offsetX": 0, "offsetY": 0, "width": 100, "height": 100}]}, "_3a")
    out_without, _, _ = run({"width": 800, "height": 600, "layers": base_layers}, "_3b")
    with Image.open(out_with) as a, Image.open(out_without) as b:
        same = a.tobytes() == b.tobytes()
    check(3, "opacity=1.5时输出与跳过该图层相同", same and rep3["failedCount"] == 1)

    # ---- 核对4: 宽200、offsetX=-50 -> 仅右侧150列可见 ----
    # 图层左半(0..49)红色、右半(50..199)绿色，便于区分
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    for x in range(200):
        c = (255, 0, 0, 255) if x < 50 else (0, 255, 0, 255)
        for y in range(200):
            img.putpixel((x, y), c)
    img.save(os.path.join(WORK, "off.png"))
    out, _, _ = run({"width": 800, "height": 600, "layers": [
        {"file": "off.png", "mode": "normal", "opacity": 1.0,
         "offsetX": -50, "offsetY": 0, "width": 200, "height": 200}]}, "_4")
    with Image.open(out) as im:
        im = im.convert("RGBA")
        p0 = im.getpixel((0, 100))      # 画布x=0 对应图层x=50（绿色）
        p149 = im.getpixel((149, 100))  # 画布x=149 对应图层x=199（绿色）
        p150 = im.getpixel((150, 100))  # 画布x=150 在图层外（透明）
        red_visible = any(im.getpixel((x, 100))[:3] == (255, 0, 0) for x in range(800))
    ok = (p0 == (0, 255, 0, 255) and p149 == (0, 255, 0, 255)
          and p150[3] == 0 and not red_visible)
    check(4, "宽200偏移X=-50时仅右侧150像素列可见", ok,
          f"x0={p0}, x149={p149}, x150={p150}")

    # ---- 核对5: 声明100x100实际50x50 -> 报告尺寸不符 ----
    save_img("mm.png", (50, 50), (0, 255, 0, 255))
    _, _, rep = run({"width": 800, "height": 600, "layers": [
        {"file": "mm.png", "mode": "normal", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 100, "height": 100}]}, "_5")
    ok = rep["failedCount"] == 1 and "尺寸不符" in rep["errors"][0]["reason"]
    check(5, "声明100x100实际50x50时报告尺寸不符", ok, rep["errors"][0]["reason"])

    # ---- 核对6: 删除一个图层文件 -> 预览仍生成且报告记录读取失败 ----
    save_img("keep.png", (100, 100), (255, 255, 0, 255))
    # gone.png 不生成
    out, _, rep = run({"width": 800, "height": 600, "layers": [
        {"file": "gone.png", "mode": "normal", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 100, "height": 100},
        {"file": "keep.png", "mode": "normal", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 100, "height": 100}]}, "_6")
    ok = (os.path.exists(out) and rep["successCount"] == 1
          and rep["failedCount"] == 1
          and any("缺失或无法解码" in e["reason"] for e in rep["errors"]))
    check(6, "文件缺失时预览仍生成且报告记录读取失败", ok)

    # ---- 核对7: 全透明图层计为成功且画面不变 ----
    save_img("trans.png", (400, 300), (0, 0, 0, 0))
    out_t, _, rep7 = run({"width": 800, "height": 600, "layers": base_layers + [
        {"file": "trans.png", "mode": "multiply", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 400, "height": 300}]}, "_7a")
    with Image.open(out_t) as a, Image.open(out_without) as b:
        same = a.tobytes() == b.tobytes()
    ok = rep7["successCount"] == 2 and rep7["failedCount"] == 0 and same
    check(7, "全透明图层计为成功且预览内容不变", ok)

    # ---- 核对8: 输出PNG无EXIF或文本段 ----
    chunks = png_chunk_types(out_without)
    bad = [c for c in chunks if c in ("eXIf", "tEXt", "zTXt", "iTXt")]
    check(8, "输出PNG无EXIF或文本段", not bad, f"块序列: {chunks}")

    # ---- 核对9: 两个图层出错 -> successCount=总数-2, errors两条 ----
    _, _, rep = run({"width": 800, "height": 600, "layers": base_layers + [
        {"file": "ov.png", "mode": "overlay", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 100, "height": 100},
        {"file": "badop.png", "mode": "normal", "opacity": 1.5,
         "offsetX": 0, "offsetY": 0, "width": 100, "height": 100}]}, "_9")
    ok = rep["totalLayers"] == 3 and rep["successCount"] == 1 and len(rep["errors"]) == 2
    check(9, "两图层出错时successCount=总数-2且errors两条", ok,
          f"success={rep['successCount']}, errors={len(rep['errors'])}")

    # ---- 核对10: 缺失width -> 无PNG输出且报告标记配置无效 ----
    out10 = os.path.join(WORK, "preview_10.png")
    open(out10, "wb").write(b"stale")  # 预置旧文件，应被移除
    mpath = os.path.join(WORK, "manifest_10.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump({"height": 600, "layers": []}, f)
    rep = composite(mpath, out10, os.path.join(WORK, "report_10.json"))
    ok = not os.path.exists(out10) and rep["configValid"] is False
    check(10, "缺失width时无PNG输出且报告配置无效", ok)

    # ---- 核对11: 两个条目均指向不存在的a.png -> errors两条 ----
    _, _, rep = run({"width": 800, "height": 600, "layers": [
        {"file": "a.png", "mode": "normal", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 10, "height": 10},
        {"file": "a.png", "mode": "normal", "opacity": 1.0,
         "offsetX": 20, "offsetY": 20, "width": 10, "height": 10}]}, "_11")
    ok = len(rep["errors"]) == 2 and all(e["file"] == "a.png" for e in rep["errors"])
    check(11, "同名文件两个条目各自记录错误", ok, f"errors={len(rep['errors'])}")

    # ---- 核对12: 已存在的报告被覆盖，无旧内容残留 ----
    rep_path = os.path.join(WORK, "report_12.json")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write('{"OLD_MARKER": true, "junk": "x"' * 50)
    mpath = os.path.join(WORK, "manifest_12.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump({"width": 800, "height": 600, "layers": base_layers}, f)
    composite(mpath, os.path.join(WORK, "preview_12.png"), rep_path)
    with open(rep_path, "r", encoding="utf-8") as f:
        content = f.read()
    ok = "OLD_MARKER" not in content and json.loads(content)["successCount"] == 1
    check(12, "旧报告被本次结果覆盖无残留", ok)

    print()
    passed = sum(1 for r in RESULTS if r[2])
    print(f"验证结果: {passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
