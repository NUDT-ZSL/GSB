from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from relief_tiles.config import parse_config
from relief_tiles.samples import sample_config_dict


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def config_dict(
    *,
    seed: int = 20260101,
    width: int = 48,
    height: int = 32,
    rows: int = 2,
    cols: int = 3,
    concurrency: int = 2,
    budget_blocks: int = 3,
) -> dict[str, Any]:
    result = copy.deepcopy(sample_config_dict(seed))
    result["canvas"] = {"width": width, "height": height}
    result["grid"] = {"rows": rows, "cols": cols}
    block_width = width // cols
    block_height = height // rows
    result["resources"] = {
        "concurrency_limit": concurrency,
        "memory_budget_pixels": (block_width + 2)
        * (block_height + 2)
        * budget_blocks,
    }
    result["slice"] = {"preview_row": min(1, rows - 1), "preview_col": min(2, cols - 1)}
    parse_config(result)
    return result


def config(
    **kwargs: Any,
):
    return parse_config(config_dict(**kwargs))


def write_config(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def cli_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(PROJECT_ROOT)
        if not existing
        else str(PROJECT_ROOT) + os.pathsep + existing
    )
    return environment


def run_cli(
    *args: str,
    timeout: float = 30.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "relief_tiles.cli", *args],
        cwd=PROJECT_ROOT,
        env=cli_environment(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def start_cli(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "relief_tiles.cli", *args],
        cwd=PROJECT_ROOT,
        env=cli_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
