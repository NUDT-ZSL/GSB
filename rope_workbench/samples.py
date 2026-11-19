"""本地确定性样本生成。

样本 = 任务清单(JSONL) + 固定 seed 的 NumPy Q/K/V 数组(.npz 缓存)。
不依赖网络与真实账号；同一 seed 重生成逐字节一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# 清单字段契约（顺序固定，序列化按此顺序写出）
CASE_FIELDS = (
    "case_id", "seq_len", "head_dim", "num_heads", "base_theta",
    "pos_encoding_variant", "pos_scale", "block_size", "causal", "seed",
)
# 可选扩展字段
OPTIONAL_FIELDS = ("mask", "expect_equivalent", "inject")


def default_cases():
    """内置确定性样本清单，覆盖四个变体与四类注入触发。"""
    base = dict(seq_len=64, head_dim=8, num_heads=2, base_theta=10000.0,
                pos_encoding_variant="rope_classic", pos_scale=1.0,
                block_size=16, causal=True, seed=20261120)
    cases = []

    def add(**kw):
        c = dict(base)
        c.update(kw)
        cases.append(c)

    add(case_id="c001_classic")
    add(case_id="c002_interp", pos_encoding_variant="position_interpolation",
        pos_scale=2.0)
    add(case_id="c003_ntk", pos_encoding_variant="ntk_aware", pos_scale=2.0)
    add(case_id="c004_blockwise", pos_encoding_variant="blockwise_freq",
        block_size=16)
    add(case_id="c005_inject_angle", pos_encoding_variant="position_interpolation",
        pos_scale=-1.0)                        # 触发 angle 阶段注入
    add(case_id="c006_inject_rotate", head_dim=7)   # 奇数维 -> rotate 配对越界
    add(case_id="c007_inject_score", block_size=128)  # block_size > seq_len
    add(case_id="c008_inject_norm", causal=False, mask="causal")  # 掩码语义冲突
    add(case_id="c009_equiv_interp_s1", pos_encoding_variant="position_interpolation",
        pos_scale=1.0, expect_equivalent=True)  # 与 c001 声明等价
    return cases


def case_arrays(case):
    """由固定 seed 确定性生成 Q/K/V，形状 (num_heads, seq_len, head_dim)。"""
    rng = np.random.default_rng(int(case["seed"]))
    shape = (int(case["num_heads"]), int(case["seq_len"]), int(case["head_dim"]))
    q = rng.standard_normal(shape, dtype=np.float64) * 0.5
    k = rng.standard_normal(shape, dtype=np.float64) * 0.5
    v = rng.standard_normal(shape, dtype=np.float64)
    return q, k, v


def write_manifest(cases, path):
    """按固定字段顺序写 JSONL（每行一个 case，键序确定）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for c in cases:
            ordered = {k: c[k] for k in CASE_FIELDS if k in c}
            for k in OPTIONAL_FIELDS:
                if k in c:
                    ordered[k] = c[k]
            f.write(json.dumps(ordered, ensure_ascii=False, sort_keys=False) + "\n")


def read_manifest(path):
    cases = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def cache_arrays(case, cache_dir):
    """把 Q/K/V 写入缓存目录；返回文件路径。"""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{case['case_id']}.npz"
    q, k, v = case_arrays(case)
    with path.open("wb") as f:
        np.savez(f, q=q, k=k, v=v)
    return path


def load_arrays(case, cache_dir):
    path = Path(cache_dir) / f"{case['case_id']}.npz"
    if not path.exists():
        return case_arrays(case)
    with np.load(path) as z:
        return z["q"], z["k"], z["v"]


def generate_all(data_dir="data", cache_dir="cache"):
    """生成内置样本清单与数组缓存，返回 (cases, manifest_path)。"""
    cases = default_cases()
    manifest = Path(data_dir) / "cases.jsonl"
    write_manifest(cases, manifest)
    for c in cases:
        cache_arrays(c, cache_dir)
    return cases, str(manifest)
