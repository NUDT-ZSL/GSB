"""Offline skeletal animation keyframe editing and incremental recomputation."""

from .engine import AnimEngine, EngineReport
from .errors import AnimRecalcError
from .edits import load_edit_script
from .pipeline import load_project_files, normalize_project
from .sample_gen import generate_sample_tree

__version__ = "1.0.0"

__all__ = [
    "AnimEngine",
    "AnimRecalcError",
    "EngineReport",
    "generate_sample_tree",
    "load_edit_script",
    "load_project_files",
    "normalize_project",
]
