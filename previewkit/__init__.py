"""previewkit — 镜头清单 + 字幕表 → 可逐帧核对的字幕预览片。

模块划分：
    loader     读取并校验 JSON 输入
    timeline   规范化镜头时间轴（排序、重叠、空档）
    subtitles  判定每条字幕能否处理并给出原因
    render     Pillow 逐帧绘制 + ffmpeg 无损编码
    report     生成 JSON 报告
    cli        命令行入口
"""

__version__ = "1.0.0"
