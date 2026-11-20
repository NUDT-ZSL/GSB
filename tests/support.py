from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from qxworkbench.samplegen import generate_samples


def prepare_samples(root: Path) -> Path:
    samples = root / "samples"
    generate_samples(samples)
    return samples


def run_cli(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "qxworkbench", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=Path(__file__).resolve().parents[1],
    )
