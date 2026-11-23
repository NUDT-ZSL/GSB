"""测试共用的确定性任务与临时目录工具。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from rope_attn.config import RunConfig, Task


def make_task(**overrides) -> Task:
    payload = {
        "case_id": "case_test",
        "seq_len": 12,
        "head_dim": 8,
        "num_heads": 2,
        "base_theta": 10000.0,
        "pos_encoding_variant": "standard",
        "pos_scale": 1.0,
        "kv_revision": 1,
        "block_size": 4,
        "layers": 2,
        "causal": True,
        "seed": 42,
        "dtype": "float32",
    }
    payload.update(overrides)
    return Task(**payload)


class TempWorkspace:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rope_attn_test_"))
        self.cache = self.root / "cache"
        self.state = self.root / "state"

    def config(self, **overrides) -> RunConfig:
        payload = {
            "cache_dir": str(self.cache),
            "state_dir": str(self.state),
            "workers": 1,
        }
        payload.update(overrides)
        return RunConfig(**payload)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
