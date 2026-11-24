"""并发快照分片加载与内存预算约束.

按配置并发上限使用有界线程池并行加载与校验快照分片;
在途数据总量受内存预算约束, 超出预算时按确定顺序(分片规范键)等待,
任一分片失败即取消其余任务并抛出首个错误。
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .canon import sha256_file
from .errors import DiagError, E_BUDGET, E_DIGEST, E_SCHEMA


class MemoryBudget:
    """确定性等待的内存预算闸门(条件变量, 等待顺序由提交顺序保证)."""

    def __init__(self, total_bytes: int):
        if total_bytes <= 0:
            raise DiagError(E_BUDGET, "内存预算必须为正数", {"budget": total_bytes})
        self.total = total_bytes
        self._avail = total_bytes
        self._cond = threading.Condition()

    def acquire(self, nbytes: int) -> None:
        if nbytes > self.total:
            raise DiagError(E_BUDGET, "单个分片超出内存预算上限",
                            {"shard_bytes": nbytes, "budget": self.total})
        with self._cond:
            while nbytes > self._avail:
                self._cond.wait()
            self._avail -= nbytes

    def release(self, nbytes: int) -> None:
        with self._cond:
            self._avail += nbytes
            self._cond.notify_all()


def load_shards(shard_paths, digests: dict, max_workers: int, budget: MemoryBudget) -> dict:
    """并行加载与校验分片, 返回 {分片名: 解析内容}.

    shard_paths 按规范键(接收屏标识确定顺序)排序后提交;
    digests: {相对路径: 记录摘要}, 用于逐分片比对。
    """
    ordered = sorted(shard_paths)
    cancel = threading.Event()
    first_error: list[BaseException] = []
    results: dict[str, dict] = {}

    def _load(path: str):
        if cancel.is_set():
            return None
        size = os.path.getsize(path)
        budget.acquire(size)
        try:
            if cancel.is_set():
                return None
            name = os.path.basename(path)
            recorded = digests.get(name) or digests.get(path)
            if recorded is None:
                raise DiagError(E_DIGEST, "分片摘要记录缺失", {"file": name})
            actual = sha256_file(path)
            if actual != recorded:
                raise DiagError(E_DIGEST, "分片摘要不匹配",
                                {"file": name, "recorded": recorded, "actual": actual})
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise DiagError(E_SCHEMA, "分片不可解析", {"file": name, "cause": str(exc)})
            return name, payload
        finally:
            budget.release(size)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_load, p) for p in ordered]
        for fut in futures:
            try:
                outcome = fut.result()
            except BaseException as exc:  # 首个错误: 取消其余任务
                if not first_error:
                    first_error.append(exc)
                    cancel.set()
                    for other in futures:
                        other.cancel()
                continue
            if outcome is not None:
                results[outcome[0]] = outcome[1]

    if first_error:
        raise first_error[0]
    return results
