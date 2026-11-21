"""由输入或参数触发的错误注入与异常链路归因。

注入不通过修改源码制造：异常参数文件（JSON）按 task_id 给出字段覆盖，
运行器在任务执行前把覆盖应用到任务参数上，流水各阶段的既有校验
自然抛出带阶段名、业务码、task_id 的 StageError。
未被任何阶段归因的异常（非 StageError）汇总为 E_UNATTRIBUTED 记录。
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import E_UNATTRIBUTED
from .tasks import TASK_FIELDS


def load_anomalies(path):
    """加载异常参数文件；不存在或为空则返回空表。"""
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        entries = json.load(fh)
    table = {}
    for entry in entries:
        table[entry["task_id"]] = dict(entry.get("overrides", {}))
    return table


def apply_anomalies(tasks, anomaly_table):
    """把异常覆盖应用到匹配的任务上，返回注入留痕记录。"""
    applied = []
    for task in tasks:
        overrides = anomaly_table.get(task.task_id)
        if not overrides:
            continue
        for key, value in sorted(overrides.items()):
            if key in TASK_FIELDS:
                setattr(task, key, value)
        applied.append({
            "task_id": task.task_id,
            "overrides": {k: overrides[k] for k in sorted(overrides)},
        })
    return applied


def unattributed_record(task_id, exc):
    """未被任何阶段归因的异常，转为结构化汇总记录。"""
    return {
        "task_id": task_id,
        "stage": "unattributed",
        "code": E_UNATTRIBUTED,
        "message": f"{type(exc).__name__}: {exc}",
        "detail": {},
    }
