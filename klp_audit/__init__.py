"""Public API for the KLP key-lifecycle audit engine."""

from .config import AuditConfig
from .engine import AuditEngine
from .errors import (
    E_ALGORITHM,
    E_BUDGET,
    E_FORMAT,
    E_INCOMPLETE,
    E_ITER_REGRESSION,
    E_LENGTH_ESCALATION,
    E_LOCK,
    E_PARAM,
    E_SALT_REUSE,
    E_SEQ_CONFLICT,
    E_STAGE,
    E_TIMEOUT,
    E_TRANSITION,
    KlpError,
)
from .generator import generate_samples

__all__ = [
    "AuditConfig",
    "AuditEngine",
    "E_ALGORITHM",
    "E_BUDGET",
    "E_FORMAT",
    "E_INCOMPLETE",
    "E_ITER_REGRESSION",
    "E_LENGTH_ESCALATION",
    "E_LOCK",
    "E_PARAM",
    "E_SALT_REUSE",
    "E_SEQ_CONFLICT",
    "E_STAGE",
    "E_TIMEOUT",
    "E_TRANSITION",
    "KlpError",
    "generate_samples",
]

__version__ = "1.0.0"

