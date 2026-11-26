"""确定性任务摄入与初始状态校验节点。"""

import json

from .errors import EValidation
from .model import Task
from .transforms import array_checksum, block_bounds, generate_state


def parse_jsonl(path):
    """按 task_id 与 logical_clock_ms 升序解析 JSONL 任务清单。"""
    tasks = []
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                tasks.append(Task.from_dict(json.loads(line)))
            except (KeyError, ValueError, TypeError) as exc:
                raise EValidation(
                    "JSONL 行解析失败",
                    details={"lineno": lineno, "error": str(exc)},
                )
    if not tasks:
        raise EValidation("任务清单为空")
    # 主键 task_id 升序，其次 logical_clock_ms 升序，确定且稳定。
    tasks.sort(key=lambda t: (t.task_id, t.logical_clock_ms))
    return tasks


def validate_task(task, state_dim):
    """对单个任务执行四类初始校验。"""
    if task.state_seed < 0:
        raise EValidation("state_seed 非法", details={"task_id": task.task_id})
    # 校验 state_seed 与状态维度一致：由种子生成的数组维度必须等于声明维度。
    generated_dim = generate_state(task.state_seed, state_dim).shape[0]
    if generated_dim != state_dim:
        raise EValidation(
            "state_seed 与状态维度不一致",
            details={"task_id": task.task_id, "dim": state_dim},
        )
    block_bounds(state_dim, task.block_size)
    if task.quant_scale <= 0:
        raise EValidation(
            "quant_scale 必须为正数", details={"task_id": task.task_id}
        )
    if task.num_steps <= 0:
        raise EValidation(
            "num_steps 必须为正数", details={"task_id": task.task_id}
        )


def check_revision_monotonic(tasks):
    """revision 非回退：在摄入排序后的序列上单调不减。"""
    last = None
    for task in tasks:
        if last is not None and task.revision < last:
            raise EValidation(
                "revision 发生回退",
                details={
                    "task_id": task.task_id,
                    "revision": task.revision,
                    "previous": last,
                },
            )
        last = task.revision


def ingest(jsonl_path, state_dim):
    """摄入并初始校验，产出确定性的分块边界与初始校验和。"""
    tasks = parse_jsonl(jsonl_path)
    check_revision_monotonic(tasks)
    records = []
    for task in tasks:
        validate_task(task, state_dim)
        state = generate_state(task.state_seed, state_dim)
        records.append(
            {
                "task": task,
                "initial_state": state,
                "initial_checksum": array_checksum(state),
                "block_bounds": block_bounds(state_dim, task.block_size),
            }
        )
    return records
