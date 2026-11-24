from __future__ import annotations

import atexit
import copy
import shutil
import tempfile
from pathlib import Path
from typing import Any

from anim_replay.samplegen import generate_samples
from anim_replay.schema import load_edits, load_input

_TEMPDIR: tempfile.TemporaryDirectory[str] | None = None
_ROOT: Path | None = None


def sample_root() -> Path:
    global _TEMPDIR, _ROOT
    if _ROOT is None:
        _TEMPDIR = tempfile.TemporaryDirectory(prefix="anim-replay-tests-")
        _ROOT = Path(_TEMPDIR.name) / "samples"
        generate_samples(_ROOT, seed=20261125)
        atexit.register(_cleanup)
    return _ROOT


def _cleanup() -> None:
    if _TEMPDIR is not None:
        _TEMPDIR.cleanup()


def sample_loaded():
    return load_input(sample_root() / "input.json")


def sample_edits(name: str = "edits.json") -> dict[str, Any]:
    return load_edits(sample_root() / name)


def sample_input_data() -> dict[str, Any]:
    import json

    return json.loads((sample_root() / "input.json").read_text(encoding="utf-8"))


def clone_input(data: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(data)

