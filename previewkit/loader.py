"""JSON 输入读取与结构校验。

结构非法（JSON 解析失败、字段缺失、类型错误）一律抛 InputFormatError，
调用方据此在生成任何产物之前中止。语义上的问题（引用了不存在的镜头、
区间倒序等）不在此拦截，留给 subtitles 模块按条登记。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import InputFormatError, Shot, Subtitle


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise InputFormatError(f"找不到输入文件：{path.as_posix()}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise InputFormatError(f"无法读取输入文件 {path.as_posix()}：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise InputFormatError(f"输入文件 {path.as_posix()} 不是 UTF-8 编码：{exc}") from exc
    if not text.strip():
        raise InputFormatError(f"输入文件为空：{path.as_posix()}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputFormatError(
            f"{path.as_posix()} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列附近 "
            f"解析失败（{exc.msg}）"
        ) from exc


def _expect_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputFormatError(f"{what} 必须是 JSON 对象，实际是 {type(value).__name__}")
    return value


def _expect_list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise InputFormatError(f"{what} 必须是 JSON 数组，实际是 {type(value).__name__}")
    return value


def _expect_int(value: Any, what: str) -> int:
    # bool 在 Python 中属于 int，必须单独排除
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputFormatError(f"{what} 必须是整数毫秒，实际是 {value!r}")
    return value


def _expect_text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise InputFormatError(f"{what} 必须是字符串，实际是 {value!r}")
    return value


def _normalize_shot_id(value: Any, where: str) -> str:
    """编号允许是整数或字符串，统一成字符串以便比对。"""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InputFormatError(f"{where}.shot_id 必须是字符串或整数，实际是 {value!r}")
    text = str(value)
    if not text:
        raise InputFormatError(f"{where}.shot_id 不能为空")
    return text


def _load_shots(raw: Any, source: str) -> list[Shot]:
    items = _expect_list(raw, f"{source} 的 shots")
    if not items:
        raise InputFormatError(f"{source} 的 shots 为空，没有可渲染的镜头")
    shots: list[Shot] = []
    seen: dict[str, int] = {}
    for pos, item in enumerate(items):
        where = f"shots[{pos}]"
        obj = _expect_object(item, where)
        for key in ("shot_id", "start_ms", "end_ms"):
            if key not in obj:
                raise InputFormatError(f"{where} 缺少字段 {key}")
        shot_id = _normalize_shot_id(obj["shot_id"], where)
        start_ms = _expect_int(obj["start_ms"], f"{where}.start_ms")
        end_ms = _expect_int(obj["end_ms"], f"{where}.end_ms")
        if start_ms < 0:
            raise InputFormatError(f"{where}.start_ms 不能为负（{start_ms}）")
        if end_ms <= start_ms:
            raise InputFormatError(
                f"{where} 区间非法：end_ms({end_ms}) 必须大于 start_ms({start_ms})"
            )
        if shot_id in seen:
            raise InputFormatError(
                f"镜头编号 {shot_id!r} 重复出现（shots[{seen[shot_id]}] 与 {where}）"
            )
        seen[shot_id] = pos
        shots.append(Shot(shot_id=shot_id, start_ms=start_ms, end_ms=end_ms, input_index=pos))
    return shots


def _load_subtitles(raw: Any, source: str) -> list[Subtitle]:
    items = _expect_list(raw, f"{source} 的 subtitles")
    subtitles: list[Subtitle] = []
    for pos, item in enumerate(items):
        where = f"subtitles[{pos}]"
        obj = _expect_object(item, where)
        for key in ("text", "start_ms", "end_ms"):
            if key not in obj:
                raise InputFormatError(f"{where} 缺少字段 {key}")
        text = _expect_text(obj["text"], f"{where}.text")
        start_ms = _expect_int(obj["start_ms"], f"{where}.start_ms")
        end_ms = _expect_int(obj["end_ms"], f"{where}.end_ms")
        shot_id = obj.get("shot_id")
        if shot_id is not None:
            shot_id = _normalize_shot_id(shot_id, where)
        subtitles.append(
            Subtitle(index=pos, text=text, start_ms=start_ms, end_ms=end_ms, shot_id=shot_id)
        )
    return subtitles


def load_inputs(shots_path: Path, subtitles_path: Path) -> tuple[list[Shot], list[Subtitle]]:
    """读取镜头清单与字幕表，返回通过结构校验的原始记录。"""
    shots_doc = _expect_object(_read_json(shots_path), f"{shots_path.as_posix()} 根节点")
    subtitles_doc = _expect_object(
        _read_json(subtitles_path), f"{subtitles_path.as_posix()} 根节点"
    )
    if "shots" not in shots_doc:
        raise InputFormatError(f"{shots_path.as_posix()} 缺少顶层字段 shots")
    if "subtitles" not in subtitles_doc:
        raise InputFormatError(f"{subtitles_path.as_posix()} 缺少顶层字段 subtitles")
    shots = _load_shots(shots_doc["shots"], shots_path.as_posix())
    subtitles = _load_subtitles(subtitles_doc["subtitles"], subtitles_path.as_posix())
    return shots, subtitles
