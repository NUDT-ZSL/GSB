from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from ddp_ckpt_lab.config import TrainingConfig


def make_config(**overrides: Any) -> TrainingConfig:
    values = {
        "seed": 73,
        "data_seed": 91,
        "samples": 64,
        "features": 4,
        "hidden_dim": 6,
        "workers": 4,
        "batch_size": 8,
        "train_steps": 8,
        "learning_rate": 50,
        "momentum": 900,
        "sharding": "modulo",
        "checkpoint_every": 2,
    }
    values.update(overrides)
    return TrainingConfig(**values)


class TempWorkspace:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="ddp-ckpt-lab-test-"))
        return self.path

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
