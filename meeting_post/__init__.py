"""Deterministic local meeting post-production toolkit."""

from .config import MeetingConfig, default_config, parse_config, render_config
from .errors import MeetingError

__all__ = [
    "MeetingConfig",
    "MeetingError",
    "default_config",
    "parse_config",
    "render_config",
]

__version__ = "1.0.0"
