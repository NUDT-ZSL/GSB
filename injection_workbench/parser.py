"""清单解析与记录级校验。

任何单条非法记录都生成结构化 Rejection（含记录序号与业务错误码），
合法记录照常返回，绝不因单条问题中断整份清单。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import DIALECTS, CONTEXT_REQUIREMENTS
from .transforms import known_transform, KNOWN_FAMILIES


# 业务错误码（记录级）
E_BAD_DIALECT = "E_BAD_DIALECT"
E_BAD_LINE = "E_BAD_LINE"
E_UNKNOWN_TRANSFORM = "E_UNKNOWN_TRANSFORM"
E_DUP_SINK_ID = "E_DUP_SINK_ID"
E_DUP_PATH_ID = "E_DUP_PATH_ID"
E_UNKNOWN_SINK_REF = "E_UNKNOWN_SINK_REF"
E_UNKNOWN_CONTEXT = "E_UNKNOWN_CONTEXT"
E_BAD_FAMILY = "E_BAD_FAMILY"
E_BAD_SHAPE = "E_BAD_SHAPE"
E_BAD_JSON = "E_BAD_JSON"
E_MISSING_FIELD = "E_MISSING_FIELD"


@dataclass
class Sink:
    sink_id: str
    line: int
    context_type: str
    required_families: tuple[str, ...]


@dataclass
class PathRecord:
    path_id: str
    source_id: str
    sink_id: str
    transforms: list[str]


@dataclass
class Rejection:
    record_index: int
    record_kind: str
    record_id: str | None
    error_code: str
    message: str

    def to_dict(self) -> dict:
        return {
            "record_index": self.record_index,
            "record_kind": self.record_kind,
            "record_id": self.record_id,
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass
class ParsedManifest:
    file_id: str | None
    dialect: str | None
    sinks: list[Sink]
    paths: list[PathRecord]
    rejections: list[Rejection]


def _is_positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_sink(raw: dict, index: int, seen_ids: set, rejections: list[Rejection]) -> Sink | None:
    sink_id = raw.get("sink_id")
    if not isinstance(sink_id, str) or not sink_id:
        rejections.append(
            Rejection(index, "sink", None, E_MISSING_FIELD, "sink 缺少合法 sink_id")
        )
        return None
    if sink_id in seen_ids:
        rejections.append(
            Rejection(index, "sink", sink_id, E_DUP_SINK_ID, f"汇点标识重复: {sink_id}")
        )
        return None

    line = raw.get("line")
    if not _is_positive_int(line):
        rejections.append(
            Rejection(index, "sink", sink_id, E_BAD_LINE, f"汇点行号须为正整数: {line!r}")
        )
        return None

    context_type = raw.get("context_type")
    if not isinstance(context_type, str) or not context_type:
        rejections.append(
            Rejection(index, "sink", sink_id, E_MISSING_FIELD, "缺少合法 context_type")
        )
        return None

    required = raw.get("required_families")
    if required is None:
        if context_type not in CONTEXT_REQUIREMENTS:
            rejections.append(
                Rejection(
                    index,
                    "sink",
                    sink_id,
                    E_UNKNOWN_CONTEXT,
                    f"未知上下文类型且未显式声明 required_families: {context_type}",
                )
            )
            return None
        required = list(CONTEXT_REQUIREMENTS[context_type])

    if not isinstance(required, list) or not required or not all(
        isinstance(item, str) for item in required
    ):
        rejections.append(
            Rejection(index, "sink", sink_id, E_BAD_SHAPE, "required_families 须为非空字符串数组")
        )
        return None
    bad = [item for item in required if item not in KNOWN_FAMILIES]
    if bad:
        rejections.append(
            Rejection(
                index, "sink", sink_id, E_BAD_FAMILY, f"未知编码族: {bad}"
            )
        )
        return None

    seen_ids.add(sink_id)
    return Sink(
        sink_id=sink_id,
        line=line,
        context_type=context_type,
        required_families=tuple(required),
    )


def _parse_path(raw: dict, index: int, sink_ids: set, seen_ids: set,
                rejections: list[Rejection]) -> PathRecord | None:
    path_id = raw.get("path_id")
    if not isinstance(path_id, str) or not path_id:
        rejections.append(
            Rejection(index, "path", None, E_MISSING_FIELD, "path 缺少合法 path_id")
        )
        return None
    if path_id in seen_ids:
        # 重复路径记录拒绝码但保留记录，交由分析层按“首次出现为真值”去重
        rejections.append(
            Rejection(index, "path", path_id, E_DUP_PATH_ID, f"路径标识重复: {path_id}")
        )

    sink_id = raw.get("sink_id")
    if not isinstance(sink_id, str) or not sink_id:
        rejections.append(
            Rejection(index, "path", path_id, E_MISSING_FIELD, "path 缺少合法 sink_id")
        )
        return None
    if sink_id not in sink_ids:
        rejections.append(
            Rejection(
                index, "path", path_id, E_UNKNOWN_SINK_REF, f"引用了不存在的汇点: {sink_id}"
            )
        )
        return None

    source_id = raw.get("source_id")
    if not isinstance(source_id, str) or not source_id:
        rejections.append(
            Rejection(index, "path", path_id, E_MISSING_FIELD, "path 缺少合法 source_id")
        )
        return None

    transforms = raw.get("transforms")
    if not isinstance(transforms, list) or not all(isinstance(item, str) for item in transforms):
        rejections.append(
            Rejection(index, "path", path_id, E_BAD_SHAPE, "transforms 须为字符串数组")
        )
        return None

    bad = [idx for idx, name in enumerate(transforms, start=1) if not known_transform(name)]
    if bad:
        bad_name = transforms[bad[0] - 1]
        rejections.append(
            Rejection(
                index,
                "path",
                path_id,
                E_UNKNOWN_TRANSFORM,
                f"第 {bad[0]} 个变换名未知: {bad_name!r}",
            )
        )
        return None

    seen_ids.add(path_id)
    return PathRecord(
        path_id=path_id, source_id=source_id, sink_id=sink_id, transforms=list(transforms)
    )


def parse_manifest(raw: object) -> ParsedManifest:
    """解析已反序列化的清单对象，逐条校验并收集拒绝记录。"""
    rejections: list[Rejection] = []
    if not isinstance(raw, dict):
        return ParsedManifest(None, None, [], [], [
            Rejection(0, "manifest", None, E_BAD_SHAPE, "清单顶层须为 JSON 对象")
        ])

    file_id = raw.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        rejections.append(Rejection(0, "manifest", None, E_MISSING_FIELD, "缺少合法 file_id"))
        file_id = None

    dialect = raw.get("dialect")
    if dialect not in DIALECTS:
        rejections.append(
            Rejection(0, "manifest", file_id, E_BAD_DIALECT, f"方言不在白名单: {dialect!r}")
        )
        dialect = None

    sinks_raw = raw.get("sinks", [])
    paths_raw = raw.get("paths", [])
    if not isinstance(sinks_raw, list):
        rejections.append(Rejection(0, "manifest", file_id, E_BAD_SHAPE, "sinks 须为数组"))
        sinks_raw = []
    if not isinstance(paths_raw, list):
        rejections.append(Rejection(0, "manifest", file_id, E_BAD_SHAPE, "paths 须为数组"))
        paths_raw = []

    sinks: list[Sink] = []
    seen_sink_ids: set[str] = set()
    for idx, item in enumerate(sinks_raw, start=1):
        if not isinstance(item, dict):
            rejections.append(Rejection(idx, "sink", None, E_BAD_SHAPE, "汇点记录须为对象"))
            continue
        sink = _parse_sink(item, idx, seen_sink_ids, rejections)
        if sink is not None:
            sinks.append(sink)

    paths: list[PathRecord] = []
    seen_path_ids: set[str] = set()
    valid_sink_ids = {sink.sink_id for sink in sinks}
    for idx, item in enumerate(paths_raw, start=1):
        if not isinstance(item, dict):
            rejections.append(Rejection(idx, "path", None, E_BAD_SHAPE, "路径记录须为对象"))
            continue
        path = _parse_path(item, idx, valid_sink_ids, seen_path_ids, rejections)
        if path is not None:
            paths.append(path)

    return ParsedManifest(
        file_id=file_id, dialect=dialect, sinks=sinks, paths=paths, rejections=rejections
    )


def load_manifest(path: str | Path) -> tuple[ParsedManifest | None, Rejection | None]:
    """从磁盘读取并解析清单；JSON 本身损坏时返回致命拒绝记录。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, Rejection(0, "manifest", None, E_BAD_JSON, f"清单读取/解析失败: {exc}")
    return parse_manifest(raw), None
