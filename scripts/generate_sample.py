"""确定性生成业务样例清单（无随机数，重复运行结果逐字节一致）。

用法：python scripts/generate_sample.py [输出清单路径]
"""
from __future__ import annotations

import json
import os
import sys

# 允许直接以脚本方式运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LONG_TEXT = "字幕" * 100 + "尾"      # 201 字符，触发超长跳过
assert len(LONG_TEXT) == 201
WHITESPACE_TEXT = "   \n\t  "        # 仅空白，触发跳过


def build_manifest() -> dict:
    return {
        "manifest_id": "meeting-subtitle-demo-2026",
        "sheets": [
            # ---------- 画布 1：有效画布，覆盖全部冲突类型 ----------
            {
                "id": "meeting_main",
                "width": 1280,
                "height": 720,
                "padding": 20,
                "faces": [
                    {"id": "face-1", "x": 300, "y": 120, "width": 180, "height": 200},
                    {"id": "face-2", "x": 800, "y": 140, "width": 160, "height": 190},
                ],
                "texts": [
                    # 1) 正常安全区文字（默认前景/描边色）
                    {"id": "t1_title", "text": "会议议程 · 2026 季度复盘",
                     "x": 40, "y": 40, "font_size": 32, "font_weight": "bold",
                     "font_family": "microsoft_yahei"},
                    # 2) 左贴边越界：x=8 < padding 20 → 边界冲突（红框）
                    {"id": "t2_edge", "text": "左下角字幕（贴边测试）",
                     "x": 8, "y": 660, "font_size": 28, "font_weight": "regular",
                     "font_family": "microsoft_yahei"},
                    # 3) 压人脸 face-1（黄框）
                    {"id": "t3_face", "text": "发言人：张伟",
                     "x": 330, "y": 260, "font_size": 26, "font_weight": "bold",
                     "font_family": "microsoft_yahei",
                     "foreground": "#7CFFB2", "stroke_color": "#101010"},
                    # 4/5) 两段文字互相重叠（紫框，同一对只记一次）
                    {"id": "t4_overlap_a", "text": "讨论要点（一）",
                     "x": 620, "y": 480, "font_size": 24,
                     "font_family": "microsoft_yahei",
                     "foreground": "#8AD8FF", "stroke_color": "#002030"},
                    {"id": "t5_overlap_b", "text": "讨论要点（二）",
                     "x": 660, "y": 500, "font_size": 24,
                     "font_family": "microsoft_yahei",
                     "foreground": "#FFD58A", "stroke_color": "#302000"},
                    # 6) 同时越界且压人脸 → 红框优先，报告含 边界+人脸
                    {"id": "t6_both", "text": "本场会议由张伟主持并记录会议纪要内容",
                     "x": 8, "y": 200, "font_size": 26,
                     "font_family": "microsoft_yahei"},
                    # 7) 空文本 → 跳过
                    {"id": "t7_empty", "text": "", "x": 100, "y": 100,
                     "font_size": 24, "font_family": "microsoft_yahei"},
                    # 8) 仅空白 → 跳过
                    {"id": "t8_blank", "text": WHITESPACE_TEXT, "x": 100, "y": 150,
                     "font_size": 24, "font_family": "microsoft_yahei"},
                    # 9) 超长 201 字符 → 跳过
                    {"id": "t9_long", "text": LONG_TEXT, "x": 40, "y": 600,
                     "font_size": 20, "font_family": "microsoft_yahei"},
                    # 10) 字号 5 < 6 → 跳过
                    {"id": "t10_tiny", "text": "字号过小", "x": 40, "y": 560,
                     "font_size": 5, "font_family": "microsoft_yahei"},
                    # 11) 未知字体族 → 加载失败跳过
                    {"id": "t11_badfont", "text": "字体不存在", "x": 900, "y": 620,
                     "font_size": 22, "font_family": "not_a_real_font"},
                    # 12/13) 真实度量演示：英文同文本不同字号/字重
                    {"id": "t12_metric_reg", "text": "Subtitle 2026",
                     "x": 900, "y": 560, "font_size": 20, "font_weight": "regular",
                     "font_family": "arial"},
                    {"id": "t13_metric_bold", "text": "Subtitle 2026",
                     "x": 900, "y": 610, "font_size": 20, "font_weight": "bold",
                     "font_family": "arial"},
                ],
            },
            # ---------- 画布 2：非法画布（负宽度）→ 整条跳过 ----------
            {
                "id": "bad_canvas",
                "width": -1280,
                "height": 720,
                "padding": 20,
                "faces": [],
                "texts": [
                    {"id": "x1", "text": "不应被渲染", "x": 10, "y": 10,
                     "font_size": 20, "font_family": "microsoft_yahei"},
                ],
            },
            # ---------- 画布 3：局部非法人脸与文字位置，其余正常 ----------
            {
                "id": "mixed_invalid",
                "width": 800,
                "height": 600,
                "padding": 16,
                "faces": [
                    {"id": "face-a", "x": 100, "y": 100, "width": 120, "height": 140},
                    {"id": "face-bad", "x": 900, "y": 100, "width": 120, "height": 140},
                ],
                "texts": [
                    # x 非数值 → 跳过
                    {"id": "m1_badpos", "text": "位置非法", "x": "一百", "y": 100,
                     "font_size": 22, "font_family": "microsoft_yahei"},
                    # 正常文字
                    {"id": "m2_ok", "text": "右上角标签", "x": 560, "y": 30,
                     "font_size": 20, "font_family": "microsoft_yahei",
                     "foreground": "#A6E1FF"},
                ],
            },
        ],
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = argv[0] if argv else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "samples", "manifest.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(build_manifest(), f, ensure_ascii=False, indent=2)
    print(f"清单已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
