# -*- coding: utf-8 -*-
"""
run_acceptance.py —— 9 项验收的本地自动化验证（仅标准库）。

做法：
  * 以子进程方式调用 vector_render.py，检查退出码与 stderr/stdout 文本
  * 内置最小 PNG 解码器（8-bit RGB，过滤器 0~4），读取真实 PNG 文件做像素断言
  * 检查 SVG 元素顺序与属性、报告字段、输出目录创建、无半成品文件等
全部通过时退出码 0；任一失败退出码 1。
"""

import json
import os
import struct
import subprocess
import sys
import zlib

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(ROOT, "samples")
WORK = os.path.join(ROOT, "acceptance_out")
RENDERER = os.path.join(ROOT, "vector_render.py")

PY = sys.executable
results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print("[%s] %s%s" % (flag, name, (" —— " + detail) if detail else ""))


def run_cli(input_path, output_path):
    proc = subprocess.run(
        [PY, "-I", RENDERER, input_path, output_path],
        capture_output=True,  # 按字节捕获，下面做容错解码
    )
    def decode(raw):
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    return proc.returncode, decode(proc.stdout), decode(proc.stderr)


# --------------------------------------------------------------------------- #
# 最小 PNG 解码器（只支持本工具产出的 8-bit RGB、过滤器 0~4）
# --------------------------------------------------------------------------- #
def read_png(path):
    with open(path, "rb") as fh:
        blob = fh.read()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "不是合法 PNG 签名"
    pos = 8
    width = height = bit_depth = color_type = None
    idata = b""
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        payload = blob[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type, _, _, _ = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            idata += payload
        elif tag == b"IEND":
            break
    assert bit_depth == 8 and color_type == 2, "解码器仅支持 8-bit RGB"
    channels = 3
    raw = zlib.decompress(idata)
    stride = width * channels
    pixels = bytearray()
    prev = bytearray(stride)
    offset = 0

    def paeth(a, b, c):
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        if pa <= pb and pa <= pc:
            return a
        if pb <= pc:
            return b
        return c

    for _ in range(height):
        ftype = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) // 2)) & 0xFF
            elif ftype == 4:
                line[i] = (line[i] + paeth(a, b, c)) & 0xFF
            elif ftype != 0:
                raise AssertionError("不支持的 PNG 过滤器类型：%d" % ftype)
        pixels.extend(line)
        prev = line
    return width, height, pixels


def pixel_at(pixels, width, x, y):
    off = (y * width + x) * 3
    return tuple(pixels[off:off + 3])


def sample_path(name):
    return os.path.join(SAMPLES, name)


def output_path(name):
    return os.path.join(WORK, name)


def main():
    os.makedirs(WORK, exist_ok=True)

    # ---- 验收 1：删除 width 后报含 width 的错误并非零退出 ------------------ #
    no_width = output_path("no_width.json")
    with open(sample_path("basic.json"), "r", encoding="utf-8") as fh:
        basic = json.load(fh)
    del basic["width"]
    with open(no_width, "w", encoding="utf-8") as fh:
        json.dump(basic, fh)
    code, _, err = run_cli(no_width, output_path("no_width.png"))
    record("验收1 缺少 width 字段报错并非零退出",
           code != 0 and "width" in err, "exit=%d, stderr=%s" % (code, err.strip()))

    # ---- 验收 2：不支持的输出格式，无图像文件 ------------------------------ #
    jpg_path = output_path("result.jpg")
    if os.path.exists(jpg_path):
        os.remove(jpg_path)
    code, _, err = run_cli(sample_path("basic.json"), jpg_path)
    record("验收2 output.jpg 提示不支持的输出格式、非零退出且无文件",
           code != 0 and "不支持的输出格式" in err and not os.path.exists(jpg_path),
           "exit=%d, stderr=%s" % (code, err.strip()))

    # ---- 验收 3：未知 triangle 被跳过，圆形正常渲染，报告记录索引与原因 ---- #
    skip_png = output_path("skip.png")
    code, _, _ = run_cli(sample_path("skip.json"), skip_png)
    w, h, pixels = read_png(skip_png)
    center = pixel_at(pixels, w, 80, 80)   # 合法蓝色圆形中心应是 #3366cc
    with open(output_path("skip.txt"), "r", encoding="utf-8") as fh:
        report = fh.read()
    record("验收3 非法图元跳过、圆形正常渲染、报告记录索引与原因",
           code == 0 and center == (0x33, 0x66, 0xcc)
           and "跳过图元数：4" in report
           and "索引 1" in report and "triangle" in report
           and "索引 2" in report and "radius" in report,
           "圆心像素=%s" % (center,))

    # ---- 验收 4：50%% 蓝叠加红，合成像素约 (127, 0, 128) --------------------- #
    alpha_png = output_path("alpha.png")
    code, _, _ = run_cli(sample_path("alpha.json"), alpha_png)
    w, h, pixels = read_png(alpha_png)
    blended = pixel_at(pixels, w, 50, 50)
    red_only = pixel_at(pixels, w, 15, 50)
    record("验收4 alpha 合成像素约为 (127,0,128)",
           code == 0 and blended == (127, 0, 128) and red_only == (255, 0, 0),
           "混合区=%s，红区=%s" % (blended, red_only))

    # ---- 验收 5：SVG 依序输出 rect/circle，属性正确 ------------------------ #
    svg_path = output_path("basic.svg")
    code, _, _ = run_cli(sample_path("basic.json"), svg_path)
    with open(svg_path, "r", encoding="utf-8") as fh:
        svg = fh.read()
    rect_pos = svg.find("<rect")
    circle_pos = svg.find("<circle")
    line_pos = svg.find("<line")
    record("验收5 SVG 依序生成 rect/circle/line 且属性正确",
           code == 0 and 0 <= rect_pos < circle_pos < line_pos
           and 'fill="#ff0000"' in svg and 'fill="#0000ff"' in svg
           and 'stroke="#00aa44"' in svg and 'stroke-width="4"' in svg
           and 'fill-opacity="0.5"' not in svg,  # 不透明度 1 也显式输出 fill-opacity="1"
           "rect@%d circle@%d line@%d" % (rect_pos, circle_pos, line_pos))

    # ---- 验收 6：同名 .txt 报告含尺寸/成功数/跳过数/逐条原因 --------------- #
    result_png = output_path("result.png")
    code, _, _ = run_cli(sample_path("skip.json"), result_png)
    with open(output_path("result.txt"), "r", encoding="utf-8") as fh:
        result_report = fh.read()
    record("验收6 result.png 同名 result.txt 含成功数/跳过数/逐条原因",
           code == 0 and os.path.isfile(result_png)
           and "画布宽度：160" in result_report and "画布高度：160" in result_report
           and "成功渲染图元数：1" in result_report and "跳过图元数：4" in result_report
           and result_report.count("索引 ") == 4)

    # ---- 验收 7：输出到不存在的多级目录，自动创建并生成 -------------------- #
    deep_png = output_path(os.path.join("sub", "dir", "out.png"))
    if os.path.exists(output_path("sub")):
        import shutil
        shutil.rmtree(output_path("sub"))
    code, _, _ = run_cli(sample_path("basic.json"), deep_png)
    record("验收7 多级目录自动创建且图像与报告生成",
           code == 0 and os.path.isfile(deep_png)
           and os.path.isfile(output_path(os.path.join("sub", "dir", "out.txt"))))

    # 验收7 反向：父路径被普通文件占用时目录创建失败，非零退出且无半成品
    blocker = output_path("blocker")
    with open(blocker, "w", encoding="utf-8") as fh:
        fh.write("i am a file, not a directory")
    blocked_png = output_path(os.path.join("blocker", "nested", "out.png"))
    code, _, err = run_cli(sample_path("basic.json"), blocked_png)
    record("验收7补充 目录创建失败时报错、非零退出且无半成品文件",
           code != 0 and "无法创建输出目录" in err
           and not os.path.exists(blocked_png)
           and not os.path.exists(output_path(os.path.join("blocker", "nested", "out.txt"))),
           "exit=%d" % code)

    # ---- 验收 8：width=5000 报宽度越界，非零退出且无任何输出 --------------- #
    wide_png = output_path("wide.png")
    wide_txt = output_path("wide.txt")
    for p in (wide_png, wide_txt):
        if os.path.exists(p):
            os.remove(p)
    code, _, err = run_cli(sample_path("too_wide.json"), wide_png)
    record("验收8 width=5000 提示宽度越界、非零退出且无输出文件",
           code != 0 and "width" in err and "越界" in err
           and not os.path.exists(wide_png) and not os.path.exists(wide_txt),
           "exit=%d, stderr=%s" % (code, err.strip()))

    # 补充：height=0 同样拦截
    code, _, err = run_cli(sample_path("bad_height.json"), output_path("bh.png"))
    record("验收8补充 height=0 提示高度越界且无文件",
           code != 0 and "height" in err and "越界" in err
           and not os.path.exists(output_path("bh.png"))
           and not os.path.exists(output_path("bh.txt")))

    # ---- 验收 9：文件不存在 / 非法 JSON，均非零退出且无输出 ---------------- #
    missing = output_path("missing.json")
    if os.path.exists(missing):
        os.remove(missing)
    code, _, err = run_cli(missing, output_path("missing.png"))
    record("验收9a missing.json 错误信息含文件名、非零退出",
           code != 0 and "missing.json" in err and not os.path.exists(output_path("missing.png")),
           "exit=%d, stderr=%s" % (code, err.strip()))

    code, _, err = run_cli(sample_path("broken.json"), output_path("broken.png"))
    record("验收9b 非法 JSON 报错、非零退出且无输出",
           code != 0 and "合法 JSON" in err
           and not os.path.exists(output_path("broken.png"))
           and not os.path.exists(output_path("broken.txt")),
           "exit=%d, stderr=%s" % (code, err.strip()))

    # ---- 附加：背景色字段错误、顺序覆盖语义、SVG 透明度属性 ---------------- #
    code, _, err = run_cli(sample_path("bad_background.json"), output_path("bb.png"))
    record("附加1 background 颜色格式错误被场景级拦截",
           code != 0 and "background" in err and not os.path.exists(output_path("bb.png")))

    with open(output_path("alpha.svg"), "w", encoding="utf-8") as fh:  # 确保路径可用
        fh.write("")
    code, _, _ = run_cli(sample_path("alpha.json"), output_path("alpha.svg"))
    with open(output_path("alpha.svg"), "r", encoding="utf-8") as fh:
        alpha_svg = fh.read()
    record("附加2 SVG 透明度以 fill-opacity 标准属性表达",
           code == 0 and 'fill-opacity="0.5"' in alpha_svg
           and alpha_svg.find("<rect") < alpha_svg.find("<rect", alpha_svg.find("<rect") + 1))

    # ---- 汇总 ------------------------------------------------------------- #
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n================ 验收汇总 ================")
    print("通过 %d / %d" % (passed, total))
    if passed != total:
        print("存在失败项，请检查上方 FAIL 记录")
        return 1
    print("全部验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
