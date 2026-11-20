from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def copy_samples(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "samples", workspace / "samples")
    return workspace


def invoke(
    workspace: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "pipeline.py"),
            *args,
            "--workspace",
            str(workspace),
        ],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
        timeout=60,
    )


def run_sample(
    workspace: Path,
    *,
    state: str = "var/state.sqlite3",
    cache: str = "var/cache",
    extra: tuple[str, ...] = (),
    manifest: str = "samples/manifest.json",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return invoke(
        workspace,
        "run",
        "--manifest",
        manifest,
        "--state",
        state,
        "--cache",
        cache,
        *extra,
        env=env,
    )


def payload(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    text = process.stdout.strip() or process.stderr.strip()
    return json.loads(text)


def task_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["task"]: task for task in result["tasks"]}


def read_manifest(workspace: Path, name: str = "samples/manifest.json") -> dict[str, Any]:
    return json.loads((workspace / name).read_text(encoding="utf-8"))


def write_manifest(
    workspace: Path, data: dict[str, Any], name: str = "samples/manifest.json"
) -> None:
    (workspace / name).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.fixture
def sample_workspace(tmp_path: Path) -> Path:
    return copy_samples(tmp_path)

