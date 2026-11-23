"""Deterministic, metric-driven autoscaling control plane.

The package is deliberately dependency-light.  Its scheduling and numerics use
logical time and exact decimal arithmetic so results do not depend on wall
clock speed or thread scheduling order.
"""

from .errors import ControlError
from .models import PolicyBundle

__all__ = ["ControlError", "PolicyBundle"]
__version__ = "1.0.0"
