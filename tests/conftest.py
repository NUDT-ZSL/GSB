from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from health_machine.cli import main
from health_machine.seed import generate_sample
from health_machine.store import Store


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace(tmp_path: Path) -> SimpleNamespace:
    samples = tmp_path / "samples"
    generate_sample(samples)
    db = tmp_path / "state" / "hsm.sqlite"
    code_load = main(["load", "--samples", str(samples), "--db", str(db)])
    code_merge = main(["merge", "--samples", str(samples), "--db", str(db),
                       "--output", str(tmp_path / "unified.json")])
    assert code_load == 0
    assert code_merge == 0
    return SimpleNamespace(root=tmp_path, samples=samples, db=db)


@pytest.fixture
def completed_workspace(workspace: SimpleNamespace) -> SimpleNamespace:
    code = main(["probe", "--db", str(workspace.db)])
    assert code == 0
    return workspace


def copy_db(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(source) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(target) + suffix))
    return target


def run_module(*args: str) -> int:
    return main(list(args))


def subprocess_env() -> dict[str, str]:
    env = dict(**__import__("os").environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


PYTHON = sys.executable
