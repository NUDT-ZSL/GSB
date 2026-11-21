"""SlotLine: deterministic multi-turn slot state diagnostics."""

from .config import DEFAULT_CONFIG, normalize_config
from .engine import Engine
from .errors import BusinessError
from .generator import generate_append_turn, generate_session
from .models import Candidate, ClarificationRequest, Session, Turn

__all__ = [
    "BusinessError",
    "Candidate",
    "ClarificationRequest",
    "DEFAULT_CONFIG",
    "Engine",
    "Session",
    "Turn",
    "generate_append_turn",
    "generate_session",
    "normalize_config",
]

__version__ = "1.0.0"
