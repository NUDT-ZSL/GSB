"""配置项与计算任务定义的加载及强校验。

任一不合法均以 E_CONFIG 整体拒绝加载, 并在 details.position 中给出具体位置。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import BizError, E_CONFIG

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
SUPPORTED_TYPES = frozenset({"int", "float"})
SUPPORTED_AGGREGATIONS = frozenset({"sum", "avg", "min", "max", "weighted_sum"})


@dataclass(frozen=True)
class ConfigItem:
    key: str
    type: str
    value: int | float
    weight: int | float

    def check_value(self, value) -> bool:
        if isinstance(value, bool):
            return False
        if self.type == "int":
            return isinstance(value, int)
        return isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True)
class TaskSpec:
    id: str
    keys: tuple
    aggregation: str
    expected_version: int | None = None


def _err(position: str, message: str) -> BizError:
    return BizError(E_CONFIG, message, {"position": position})


def _load_json(path: str | Path):
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise _err("$", f"cannot read file {p}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _err("$", f"invalid JSON in {p}: line {exc.lineno} col {exc.colno}: {exc.msg}") from exc


def _check_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_config_items(path: str | Path) -> dict[str, ConfigItem]:
    doc = _load_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise _err("$", "config document must be an object with an items array")
    items: dict[str, ConfigItem] = {}
    for idx, raw in enumerate(doc["items"]):
        pos = f"items[{idx}]"
        if not isinstance(raw, dict):
            raise _err(pos, "config item must be an object")
        key = raw.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise _err(f"{pos}.key", f"illegal config key: {key!r}")
        if key in items:
            raise _err(f"{pos}.key", f"duplicate config key: {key}")
        ctype = raw.get("type")
        if ctype not in SUPPORTED_TYPES:
            raise _err(f"{pos}.type", f"unsupported type: {ctype!r}")
        value = raw.get("value")
        probe = ConfigItem(key=key, type=ctype, value=0, weight=1.0)
        if not probe.check_value(value):
            raise _err(f"{pos}.value", f"value {value!r} does not match type {ctype}")
        weight = raw.get("weight")
        if not _check_number(weight):
            raise _err(f"{pos}.weight", f"unsupported weight: {weight!r}")
        items[key] = ConfigItem(key=key, type=ctype, value=value, weight=weight)
    if not items:
        raise _err("items", "at least one config item is required")
    return items


def load_task_specs(path: str | Path, items: dict[str, ConfigItem]) -> list[TaskSpec]:
    doc = _load_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("tasks"), list):
        raise _err("$", "task document must be an object with a tasks array")
    specs: list[TaskSpec] = []
    seen: set[str] = set()
    for idx, raw in enumerate(doc["tasks"]):
        pos = f"tasks[{idx}]"
        if not isinstance(raw, dict):
            raise _err(pos, "task must be an object")
        tid = raw.get("id")
        if not isinstance(tid, str) or not tid:
            raise _err(f"{pos}.id", f"illegal task id: {tid!r}")
        if tid in seen:
            raise _err(f"{pos}.id", f"duplicate task id: {tid}")
        seen.add(tid)
        keys = raw.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise _err(f"{pos}.keys", "keys must be a non-empty string array")
        if len(set(keys)) != len(keys):
            raise _err(f"{pos}.keys", "keys contain duplicates")
        for k in keys:
            if k not in items:
                raise _err(f"{pos}.keys", f"referenced config key does not exist: {k}")
        agg = raw.get("aggregation")
        if agg not in SUPPORTED_AGGREGATIONS:
            raise _err(f"{pos}.aggregation", f"unsupported aggregation: {agg!r}")
        expected = raw.get("expected_version")
        if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool) or expected < 1):
            raise _err(f"{pos}.expected_version", f"illegal expected_version: {expected!r}")
        specs.append(TaskSpec(id=tid, keys=tuple(keys), aggregation=agg, expected_version=expected))
    if not specs:
        raise _err("tasks", "at least one task is required")
    return specs


def load_sample_series(path: str | Path, items: dict[str, ConfigItem]) -> dict[str, list[float]]:
    doc = _load_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("series"), dict):
        raise _err("$", "data document must be an object with a series object")
    series: dict[str, list[float]] = {}
    for key, arr in doc["series"].items():
        pos = f"series.{key}"
        if key not in items:
            raise _err(pos, f"series has no matching config key: {key}")
        if not isinstance(arr, list) or not arr or not all(_check_number(v) for v in arr):
            raise _err(pos, "series must be a non-empty numeric array")
        series[key] = [float(v) for v in arr]
    return series
