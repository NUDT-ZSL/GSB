from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from llm_sampling_diag.samplegen import generate_suite


def make_suite() -> tuple[Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="sampling-diag-tests-"))
    suite = temp_dir / "samples"
    generate_suite(suite, seed=20261126)
    return temp_dir, suite


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)

