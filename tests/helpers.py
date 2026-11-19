from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from voice_state.synth import generate_dataset


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def make_dataset(
    root: Path,
    *,
    dialogs: int = 2,
    speakers: int = 2,
    seed: int = 4242,
) -> Path:
    dataset_dir = root / "dataset"
    generate_dataset(
        dataset_dir,
        dialog_count=dialogs,
        speaker_count=speakers,
        seed=seed,
        force=True,
    )
    return dataset_dir


def run_cli(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_ROOT) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", "voice_state.cli", *args],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

