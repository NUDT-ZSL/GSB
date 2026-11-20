"""Local, deterministic activation + transpose operator-chain component."""

from .config import Config
from .engine import execute_case, run_batch, run_query
from .errors import ErrorCode, OpChainError
from .task import parse_spec

__all__ = [
    "Config",
    "ErrorCode",
    "OpChainError",
    "execute_case",
    "parse_spec",
    "run_batch",
    "run_query",
]
