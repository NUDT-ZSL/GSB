"""Budgeted, deterministic loopback traffic aggregation."""

from .engine import Engine
from .errors import FlowAggError
from .plan import AggregationPlan, load_plan

__all__ = ["AggregationPlan", "Engine", "FlowAggError", "load_plan"]
__version__ = "1.0.0"

