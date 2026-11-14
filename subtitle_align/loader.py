"""字幕表读取与结构校验。

字幕表 JSON 结构：

{
  "config": {
    "canvas_width": 1280,        // 预览图画布宽度（像素，正整数）
    "font_size": 28,             // 渲染字号（像素，正整数）
    "video_duration_ms": 10000,  // 视频时长（毫秒，正整数）
    "max_lines": 3               // 单语种允许的最大行数（正整数）
  },
  "subtitles": [
    {
      "id": "S01",
      "start_ms": 0,             // 句子起始毫秒（必填）
      "end_ms": 3000,            // 句子结束毫秒（必填）
      "zh_text": "中文文本",       // 必填，允许空串（按文本缺失处理）
      "en_text": "English text", // 必填，允许空白串（按文本缺失处理）
      "zh_timing": {"start_ms": 0, "end_ms": 2500},   // 可选：中文独立时间区间
      "en_timing": {"start_ms": 500, "end_ms": 3000}  // 可选：英文独立时间区间
    }
  ]
}

仅当某句显式提供 zh_timing / en_timing 时，才检测该句内中英时间区间
是否重叠；未提供时两语共用句子级 start_ms/end_ms，不构成句内重叠。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import SubtitleInputError

_REQUIRED_CONFIG_FIELDS = {
    "canvas_width": "画布宽度 canvas_width",
    "font_size": "字号 font_size",
    "video_duration_ms": "视频时长 video_duration_ms",
    "max_lines": "最大行数 max_lines",
}
_REQUIRED_SENTENCE_FIELDS = {
    "start_ms": "开始毫秒 start_ms",
    "end_ms": "结束毫秒 end_ms",
    "zh_text": "中文文本 zh_text",
    "en_text": "英文文本 en_text",
}


def _is_int(value: Any) -> bool:
    """要求严格的整数（bool 不算整数）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_timing_pair(timing: Any, where: str) -> None:
    if not isinstance(timing, dict):
        raise SubtitleInputError(f"{where}必须是对象，包含 start_ms 与 end_ms")
    for key in ("start_ms", "end_ms"):
        if key not in timing:
            raise SubtitleInputError(f"{where}缺少必要字段：{key}")
        if not _is_int(timing[key]):
            raise SubtitleInputError(f"{where}的 {key} 必须是整数毫秒值")


def load_subtitle_table(path: str | Path) -> dict[str, Any]:
    """读取并完整校验字幕表，返回规范化后的 dict。

    任何结构/类型问题都抛出 SubtitleInputError，由 CLI 负责输出错误并停止。
    """
    path = Path(path)
    if not path.exists():
        raise SubtitleInputError(f"字幕表文件不存在：{path}")
    try:
        # -I 运行时无第三方 json 劫持风险；encoding 显式指定避免跟随系统 ANSI 代码页
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SubtitleInputError(f"字幕表不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc
    except OSError as exc:
        raise SubtitleInputError(f"字幕表读取失败：{exc}") from exc

    if not isinstance(data, dict):
        raise SubtitleInputError("字幕表顶层必须是对象，包含 config 与 subtitles")

    config = data.get("config")
    if config is None:
        raise SubtitleInputError("缺少全局配置对象 config")
    if not isinstance(config, dict):
        raise SubtitleInputError("全局配置 config 必须是对象")

    for key, label in _REQUIRED_CONFIG_FIELDS.items():
        if key not in config:
            raise SubtitleInputError(f"全局配置缺少必要字段：{label}")
        if not _is_int(config[key]):
            raise SubtitleInputError(f"全局配置 {label} 必须是整数")

    if config["canvas_width"] <= 0:
        raise SubtitleInputError(
            f"全局画布宽度非法：canvas_width={config['canvas_width']}，必须为正整数"
        )
    if config["font_size"] <= 0:
        raise SubtitleInputError(
            f"全局字号非法：font_size={config['font_size']}，必须为正整数"
        )
    if config["video_duration_ms"] <= 0:
        raise SubtitleInputError(
            f"全局视频时长非法：video_duration_ms={config['video_duration_ms']}，必须为正整数"
        )
    if config["max_lines"] <= 0:
        raise SubtitleInputError(
            f"全局最大行数非法：max_lines={config['max_lines']}，必须为正整数"
        )

    if "subtitles" not in data:
        raise SubtitleInputError("缺少字幕句数组 subtitles")
    subtitles = data["subtitles"]
    if not isinstance(subtitles, list):
        raise SubtitleInputError("字幕句数组 subtitles 必须是数组")

    seen_ids: set[str] = set()
    for index, sentence in enumerate(subtitles):
        where = f"第 {index + 1} 句（下标 {index}）"
        if not isinstance(sentence, dict):
            raise SubtitleInputError(f"{where}必须是对象")

        sid = sentence.get("id")
        if sid is not None and not isinstance(sid, str):
            raise SubtitleInputError(f"{where}的 id 必须是字符串")
        if sid is not None:
            if sid in seen_ids:
                raise SubtitleInputError(f"句子 id 重复：{sid}")
            seen_ids.add(sid)

        for key, label in _REQUIRED_SENTENCE_FIELDS.items():
            if key not in sentence:
                raise SubtitleInputError(f"{where}缺少必要字段：{label}")
        if not _is_int(sentence["start_ms"]):
            raise SubtitleInputError(f"{where}的 start_ms 必须是整数毫秒值")
        if not _is_int(sentence["end_ms"]):
            raise SubtitleInputError(f"{where}的 end_ms 必须是整数毫秒值")
        if not isinstance(sentence["zh_text"], str) or not isinstance(sentence["en_text"], str):
            raise SubtitleInputError(f"{where}的 zh_text / en_text 必须是字符串")

        if "zh_timing" in sentence:
            _check_timing_pair(sentence["zh_timing"], f"{where}的 zh_timing")
        if "en_timing" in sentence:
            _check_timing_pair(sentence["en_timing"], f"{where}的 en_timing")

    return data
