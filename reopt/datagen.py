"""确定性流式批次数据生成。

依据表描述、全局种子与批次序号逐批产出列式数组；倾斜键按声明比例集中分布。
相同种子与描述在任意运行次数下产出逐位一致的每批摘要与行数。
每批摘要是后续漂移观测与真值比对的唯一事实来源。
"""

from __future__ import annotations

import hashlib

import numpy as np

from .schema import TableDesc


def batch_seed(global_seed: int, table_name: str, seq: int) -> int:
    material = f"{global_seed}|{table_name}|{seq}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "little")


def generate_batch(table: TableDesc, global_seed: int, seq: int, nrows: int) -> dict[str, np.ndarray]:
    """按表描述与种子确定性生成一个批次的列式数组。"""
    rng = np.random.default_rng(batch_seed(global_seed, table.name, seq))
    columns: dict[str, np.ndarray] = {}
    for col in table.columns:
        if nrows == 0:
            columns[col.name] = np.empty(0, dtype=np.int64)
            continue
        values = rng.integers(0, col.ndv, size=nrows, dtype=np.int64)
        if col.skew_ratio > 0.0 and col.skew_hot_keys > 0:
            hot = min(col.skew_hot_keys, col.ndv)
            mask = rng.random(nrows) < col.skew_ratio
            hot_pick = rng.integers(0, hot, size=int(mask.sum()), dtype=np.int64)
            values[mask] = hot_pick
        if col.type == "float64":
            columns[col.name] = values.astype(np.float64)
        else:
            columns[col.name] = values
    return columns


def batch_digest(table_name: str, seq: int, columns: dict[str, np.ndarray],
                 declared_order: tuple[str, ...]) -> str:
    """每批摘要：表名、批次序号、行数与逐列字节内容的 SHA-256。"""
    hasher = hashlib.sha256()
    hasher.update(table_name.encode("utf-8"))
    hasher.update(b"|")
    hasher.update(str(seq).encode("ascii"))
    first = columns[declared_order[0]] if declared_order else None
    nrows = 0 if first is None else int(first.shape[0])
    hasher.update(b"|")
    hasher.update(str(nrows).encode("ascii"))
    for name in declared_order:
        arr = np.ascontiguousarray(columns[name])
        hasher.update(b"|")
        hasher.update(name.encode("utf-8"))
        hasher.update(b":")
        hasher.update(arr.dtype.str.encode("ascii"))
        hasher.update(b":")
        hasher.update(arr.tobytes())
    return hasher.hexdigest()


def combined_digest(digests: dict[str, str]) -> str:
    """一批次内各表摘要的组合摘要，作为该批次的唯一事实标识。"""
    hasher = hashlib.sha256()
    for table_name in sorted(digests):
        hasher.update(table_name.encode("utf-8"))
        hasher.update(b"=")
        hasher.update(digests[table_name].encode("ascii"))
        hasher.update(b";")
    return hasher.hexdigest()
