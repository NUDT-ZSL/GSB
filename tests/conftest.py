from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sessionfix.samplegen import generate_sample  # noqa: E402


@pytest.fixture
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "sample"
    generate_sample(path)
    return path


@pytest.fixture
def run_cli() -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        merged_env["PYTHONPATH"] = str(SRC)
        if env:
            merged_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "sessionfix", *args],
            cwd=str(cwd or ROOT),
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return run


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def json_helpers() -> tuple[Callable[[Path], Any], Callable[[Path, Any], None]]:
    return load_json, save_json
