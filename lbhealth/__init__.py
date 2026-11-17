"""Deterministic loopback load-balancer health-check component."""

from .config import Config, load_config
from .engine import HealthEngine
from .errors import ConfigError
from .models import HealthState, ProbeResult, RoundResult, RuntimeState, Transition

__all__ = [
    "Config",
    "ConfigError",
    "HealthEngine",
    "HealthState",
    "ProbeResult",
    "RoundResult",
    "RuntimeState",
    "Transition",
    "load_config",
]

__version__ = "1.0.0"
