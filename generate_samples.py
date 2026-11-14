"""确定性样例生成器：生成样例 SRT、配置 JSON 与坏样本（无随机、无网络）。

运行：python generate_samples.py
产出（默认到 ./samples 与 ./output）：
  samples/demo.srt            12 条业务样例（含交叠/倒置/超宽/硬换行）
  samples/demo.config.json    1280×720 / 30fps 画幅配置
  samples/bad.srt             混入坏块与倒置的 SRT
  samples/bad.config.json     零宽画幅等非法配置（用于终止路径验证）
  output/                     预置输出目录
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"
OUTPUT = ROOT / "output"

DEMO_CONFIG = {
    "width": 1280,
    "height": 720,
    "fps": 30,
    "safe_ratio": 0.05,
    "font_size": 32,
    "line_height": 42,
}

DEMO_SRT = """1
00:00:01,000 --> 00:00:03,000
游戏即将开始，请做好准备。

2
00:00:02,000 --> 00:00:04,500
系统提示：检测到新的任务线索，与上一句同屏错位显示。

3
00:00:05,000 --> 00:00:08,000
这是一句刻意拉长的中文测试字幕用于验证安全区宽度约束下的自动换行逻辑，换行后应当产生多行且每一行的像素宽度都不能超过安全区可用宽度。

4
00:00:09,000 --> 00:00:11,000
Supercalifragilisticexpialidocious-superlong-english-token-for-overflow-check-1234567890

5
00:00:12,000 --> 00:00:14,000
第一条同时出现的字幕。

6
00:00:12,500 --> 00:00:15,000
第二条同时出现的字幕。

7
00:00:13,000 --> 00:00:16,000
第三条同时出现的字幕，三句同屏也必须纵向错位、互不遮挡。

8
00:00:17,000 --> 00:00:19,000
显式换行第一行
显式换行第二行

9
00:00:20,000 --> 00:00:22,000
正常短句，不与任何字幕交叠。

10
00:00:23,000 --> 00:00:22,000
本句结束时间早于开始时间，用于触发倒置检测。

11
00:00:24,000 --> 00:00:26,000
结尾附近的常规提示，注意与前后字幕保持时间间隔。

12
00:00:27,000 --> 00:00:29,000
全部样例播放完毕，感谢观看。
"""

# 坏块样本：序号 2 缺正文；块 4 时间格式非法；块 5 序号非法；块 6 倒置
BAD_SRT = """1
00:00:01,000 --> 00:00:03,000
坏块样本中的正常字幕，应当照常处理。

2
00:00:04,000 --> 00:00:05,000

ABC
00:00:06,000 --> 00:00:08,000
序号不是数字，应被跳过。

4
00:00:09,000 -> 00:00:10,000
时间轴箭头格式错误，应被跳过。

5
00:00:11,000 --> 00:00:13,000
另一条正常字幕，验证坏块之后流水线仍可继续。

6
00:00:15,000 --> 00:00:14,000
结束早于开始的倒置样本。
"""

BAD_CONFIG = {
    # 零宽画幅：触发配置项校验失败、非零退出
    "width": 0,
    "height": 720,
    "fps": 30,
    "safe_ratio": 0.05,
    "font_size": 32,
    "line_height": 42,
}


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    (SAMPLES / "demo.srt").write_text(DEMO_SRT, encoding="utf-8", newline="\n")
    (SAMPLES / "bad.srt").write_text(BAD_SRT, encoding="utf-8", newline="\n")
    (SAMPLES / "demo.config.json").write_text(
        json.dumps(DEMO_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    (SAMPLES / "bad.config.json").write_text(
        json.dumps(BAD_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"样例已生成到: {SAMPLES}")
    print(f"输出目录已就绪: {OUTPUT}")


if __name__ == "__main__":
    main()
