"""Offline, deterministic HTTP request-smuggling decision engine."""

from .constants import (
    BUSINESS_CL_TE,
    BUSINESS_CHUNK_HEX,
    BUSINESS_DUP_HEADER,
    BUSINESS_TRAILING_BYTES,
)
from .checkpoint import CheckpointManager
from .closure import RuleCycleError, compute_invalid_closure
from .engine import Engine, EngineConfig, run_engine
from .generator import generate_sample
from .models import ConnectionReport, EngineReport
from .parsing import parse_messages
from .rules import RuleView, load_rule_view

__all__ = [
    "BUSINESS_CL_TE",
    "BUSINESS_CHUNK_HEX",
    "BUSINESS_DUP_HEADER",
    "BUSINESS_TRAILING_BYTES",
    "ConnectionReport",
    "CheckpointManager",
    "Engine",
    "EngineConfig",
    "EngineReport",
    "RuleView",
    "RuleCycleError",
    "compute_invalid_closure",
    "generate_sample",
    "load_rule_view",
    "parse_messages",
    "run_engine",
]

