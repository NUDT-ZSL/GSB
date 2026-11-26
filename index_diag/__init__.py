"""Deterministic local inverted-index diagnostics engine."""

from .compression import (
    CompressionCache,
    CompressionParams,
    decode_integer_stream,
    encode_integer_stream,
)
from .config import (
    ConfigLoader,
    NormalizedConfig,
    downgrade_v2_to_v1,
    load_config,
    upgrade_v1_to_v2,
)
from .errors import DiagnosticError
from .engine import IndexEngine
from .samples import generate_samples

__all__ = [
    "CompressionCache",
    "CompressionParams",
    "ConfigLoader",
    "DiagnosticError",
    "IndexEngine",
    "NormalizedConfig",
    "decode_integer_stream",
    "downgrade_v2_to_v1",
    "encode_integer_stream",
    "generate_samples",
    "load_config",
    "upgrade_v1_to_v2",
]
__version__ = "1.0.0"
