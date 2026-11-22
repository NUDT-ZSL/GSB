from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from log_retention import sample_data
from log_retention.config import Config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sample_dir(tmp_path: Path) -> Path:
    target = tmp_path / "sample"
    sample_data.generate(target)
    return target


@pytest.fixture
def config(sample_dir: Path) -> Config:
    return Config.load(sample_dir / "config.json")


def cli(
    config: Config,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "log_retention.cli",
        "--config",
        str(config.path),
        *args,
    ]
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    result = subprocess.run(
        command,
        check=False,
        cwd=ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
