from __future__ import annotations

from pathlib import Path

from quantlab.config import WorkbenchConfig


def make_config(root: Path, **overrides: object) -> WorkbenchConfig:
    values = {
        "state_dir": root / "state",
        "cache_dir": root / "cache",
        "temp_dir": root / "tmp",
        "artifacts_dir": root / "artifacts",
    }
    values.update(overrides)
    return WorkbenchConfig(**values)
