"""Multilingual rhythm challenge judging kernel.

The package is standard-library only and is organised as small, individually
reviewable modules so that every number used in a judgement can be traced back
to a deterministic rule (RTT correction, beat window, segment attribution,
trigger clamping, statistics and cross-language consistency).
"""

from .engine import RhythmEngine, load_project
from .persistence import load_snapshot, save_snapshot

ALGORITHM_VERSION = "1.0.0"
SCHEMA_VERSION = 1
SNAPSHOT_VERSION = 1

__all__ = [
    "ALGORITHM_VERSION",
    "SCHEMA_VERSION",
    "SNAPSHOT_VERSION",
    "RhythmEngine",
    "load_project",
    "load_snapshot",
    "save_snapshot",
]
