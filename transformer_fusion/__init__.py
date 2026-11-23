"""Deterministic activation + transpose fusion for local Transformer chains."""

from .cache import CacheStore, cache_diagnose
from .engine import execute
from .injection import inject_diagnose
from .model import ExecutionReport, ResourceLimits, TaskCase
from .selfcheck import equivalence_report
from .snapshot import SnapshotStore

__all__ = [
    "CacheStore",
    "ExecutionReport",
    "ResourceLimits",
    "SnapshotStore",
    "TaskCase",
    "cache_diagnose",
    "equivalence_report",
    "execute",
    "inject_diagnose",
]

__version__ = "1.0.0"
