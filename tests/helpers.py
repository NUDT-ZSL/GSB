from __future__ import annotations

import tempfile
from pathlib import Path

from sampling_compat.engine import SamplingApplication

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def make_app(temp_root: Path) -> SamplingApplication:
    return SamplingApplication(
        samples_dir=SAMPLES,
        snapshot_path=temp_root / "snapshots.json",
    )


def temporary_app() -> tuple[tempfile.TemporaryDirectory[str], SamplingApplication]:
    temporary = tempfile.TemporaryDirectory()
    return temporary, make_app(Path(temporary.name))
