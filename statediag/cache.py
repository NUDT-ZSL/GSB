"""内容寻址缓存键与已提交产物的复用存储。"""

import hashlib
import json

from .model import Task, TransformConfig


def content_key(task, config):
    """按任务内容与代数配置计算确定性内容寻址缓存键。

    键覆盖所有影响计算结果的输入；任一输入变化即得到不同键，
    从而可安全复用未受影响任务的已提交状态摘要与误差账本。
    """
    if not isinstance(task, Task):
        raise TypeError("content_key 需要 Task 实例")
    if not isinstance(config, TransformConfig):
        raise TypeError("content_key 需要 TransformConfig 实例")
    material = {
        "task_id": task.task_id,
        "state_seed": task.state_seed,
        "num_steps": task.num_steps,
        "block_size": task.block_size,
        "quant_scale": task.quant_scale,
        "revision": task.revision,
        "logical_clock_ms": task.logical_clock_ms,
        "config": config.to_dict(),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class CommitCache:
    """内存态内容寻址表，记录已提交的状态摘要与误差账本。"""

    def __init__(self):
        self._entries = {}

    def put(self, key, final_checksum, ledger, reduction):
        self._entries[key] = {
            "key": key,
            "final_checksum": final_checksum,
            "ledger": ledger,
            "reduction": reduction,
        }

    def get(self, key):
        return self._entries.get(key)

    def has(self, key):
        return key in self._entries

    def __len__(self):
        return len(self._entries)
