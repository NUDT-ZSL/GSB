"""Deterministic fixed-point interactive narrative engine."""

from .errors import DigestError, NarrativeError, SchemaError
from .schema import Script, load_script, parse_script_object
from .engine import ReplayEngine, run_choice_sequence
from .branches import explore_branches, explore_choice_sequences
from .checkpoint import CheckpointStore

__version__ = "1.0.0"

__all__ = [
    "DigestError",
    "NarrativeError",
    "SchemaError",
    "Script",
    "load_script",
    "parse_script_object",
    "ReplayEngine",
    "run_choice_sequence",
    "explore_branches",
    "explore_choice_sequences",
    "CheckpointStore",
]
