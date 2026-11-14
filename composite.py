#!/usr/bin/env python3
"""图层合成工具：按 JSON 清单把各图层 PNG 叠加到画布上，输出预览图与 JSON 报告。

用法:
    python composite.py <清单.json> [-o 预览图.png] [-r 报告.json]

清单格式:
    {
      "width": 800, "height": 600,
      "layers": [
        {"file": "bg.png", "mode": "normal", "opacity": 1.0,
         "offsetX": 0, "offsetY": 0, "width": 800, "height": 600},
        ...
      ]
    }

规则:
  - 支持 normal / multiply / screen，其他模式的图层不参与合成；
  - opacity 须在 [0, 1]，越界图层不参与合成；
  - offsetX/offsetY 为整数（可为负），只合成与画布相交的区域；
  - 图片实际尺寸与清单声明宽高不一致时，该图层记为未完成；
  - 文件缺失或无法解码时跳过该图层，其余图层照常处理；
  - 全透明图层视为成功；
  - 清单 width/height 缺失或非正整数时，不生成预览图，报告标记配置无效；
  - 输出文件已存在时以本次结果覆盖。
"""
import argparse
import json
import os
import sys

from PIL import Image, ImageChops

SUPPORTED_MODES = ("normal", "multiply", "screen")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _blend_rgb(base_rgb, top_rgb, mode):
    if mode == "multiply":
        return ImageChops.multiply(base_rgb, top_rgb)
    if mode == "screen":
        return ImageChops.screen(base_rgb, top_rgb)
    return top_rgb  # normal


def _apply_layer(canvas, layer_img, mode, opacity, offset_x, offset_y):
    """把单个图层按相交区域合成到画布上（原地修改 canvas）。"""
    cw, ch = canvas.size
    lw, lh = layer_img.size
    # 画布坐标系中的相交区域
    x0 = max(0, offset_x)
    y0 = max(0, offset_y)
    x1 = min(cw, offset_x + lw)
    y1 = min(ch, offset_y + lh)
    if x1 <= x0 or y1 <= y0:
        return  # 完全在画布外，无相交区域，但仍视为已处理
    # 图层中对应的裁剪区域
    crop = layer_img.crop((x0 - offset_x, y0 - offset_y,
                           x1 - offset_x, y1 - offset_y)).convert("RGBA")
    # 不透明度：缩放图层 alpha
    if opacity < 1.0:
        alpha = crop.getchannel("A").point(lambda a: int(round(a * opacity)))
        crop.putalpha(alpha)
    base = canvas.crop((x0, y0, x1, y1))
    blended_rgb = _blend_rgb(base.convert("RGB"), crop.convert("RGB"), mode)
    src = blended_rgb.convert("RGBA")
    src.putalpha(crop.getchannel("A"))
    base.alpha_composite(src)
    canvas.paste(base, (x0, y0))


def composite(manifest_path, output_png, report_path):
    errors = []
    success_count = 0
    total = 0
    canvas = None
    config_valid = False
    width = height = None

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as exc:
        manifest = None
        manifest_error = f"清单文件无法读取或解析: {exc}"
    else:
        manifest_error = None

    if isinstance(manifest, dict):
        w, h = manifest.get("width"), manifest.get("height")
        if _is_int(w) and _is_int(h) and w > 0 and h > 0:
            config_valid = True
            width, height = w, h

    layers = []
    if config_valid:
        raw_layers = manifest.get("layers", [])
        if isinstance(raw_layers, list):
            layers = raw_layers
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        total = len(layers)
        base_dir = os.path.dirname(os.path.abspath(manifest_path))

        for idx, entry in enumerate(layers):
            if not isinstance(entry, dict):
                errors.append({"index": idx, "file": None,
                               "reason": "图层条目不是对象"})
                continue
            fname = entry.get("file")
            mode = entry.get("mode", "normal")
            opacity = entry.get("opacity", 1.0)
            off_x = entry.get("offsetX", 0)
            off_y = entry.get("offsetY", 0)
            decl_w = entry.get("width")
            decl_h = entry.get("height")

            if mode not in SUPPORTED_MODES:
                errors.append({"index": idx, "file": fname,
                               "reason": f"不支持的混合模式: {mode}"})
                continue
            if not _is_num(opacity) or not (0.0 <= opacity <= 1.0):
                errors.append({"index": idx, "file": fname,
                               "reason": f"不透明度越界(须为0~1): {opacity}"})
                continue
            if not _is_int(off_x) or not _is_int(off_y):
                errors.append({"index": idx, "file": fname,
                               "reason": f"偏移量须为整数: offsetX={off_x}, offsetY={off_y}"})
                continue
            if not (_is_int(decl_w) and decl_w > 0 and _is_int(decl_h) and decl_h > 0):
                errors.append({"index": idx, "file": fname,
                               "reason": f"清单声明的图层宽高无效: {decl_w}x{decl_h}"})
                continue

            path = os.path.join(base_dir, str(fname))
            try:
                with Image.open(path) as im:
                    im.load()
                    layer_img = im.convert("RGBA")
            except Exception as exc:
                errors.append({"index": idx, "file": fname,
                               "reason": f"图层文件缺失或无法解码: {exc}"})
                continue

            if layer_img.size != (decl_w, decl_h):
                errors.append({"index": idx, "file": fname,
                               "reason": f"尺寸不符: 清单声明{decl_w}x{decl_h}, "
                                         f"实际{layer_img.size[0]}x{layer_img.size[1]}"})
                continue

            _apply_layer(canvas, layer_img, mode, float(opacity), off_x, off_y)
            success_count += 1

    if config_valid:
        # 仅保存最终合成结果，不写入任何元信息
        canvas.save(output_png, "PNG")
    else:
        # 配置无效：不生成预览图；若存在旧输出则移除，避免误用
        if os.path.exists(output_png):
            os.remove(output_png)

    report = {
        "manifest": os.path.abspath(manifest_path),
        "configValid": config_valid,
        "canvas": {"width": width, "height": height} if config_valid else None,
        "totalLayers": total,
        "successCount": success_count,
        "failedCount": total - success_count,
        "errors": errors,
        "output": os.path.abspath(output_png) if config_valid else None,
    }
    if not config_valid:
        report["configError"] = (manifest_error or
                                 "清单 width/height 缺失或非正整数，整体配置无效")
    # 覆盖写，不追加旧内容
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="按图层清单合成预览图并生成报告")
    parser.add_argument("manifest", help="图层清单 JSON 路径")
    parser.add_argument("-o", "--output", default="preview.png", help="预览图输出路径")
    parser.add_argument("-r", "--report", default="report.json", help="报告输出路径")
    args = parser.parse_args(argv)

    report = composite(args.manifest, args.output, args.report)
    print(f"配置有效: {report['configValid']}")
    print(f"图层总数: {report['totalLayers']}, 成功: {report['successCount']}, "
          f"未完成: {report['failedCount']}")
    for e in report["errors"]:
        print(f"  [未完成] #{e['index']} {e['file']}: {e['reason']}")
    if report["configValid"]:
        print(f"预览图: {report['output']}")
    print(f"报告: {os.path.abspath(args.report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
