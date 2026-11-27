"""Deterministic incident-query reuse kernel.

The package intentionally depends only on the Python standard library.  SQLite
is used for both durable state and the normalized event store.
"""

from .config import MATERIALIZE_THRESHOLD, MAX_MATERIALIZED, SCHEMA_VERSION
from .generator import generate_problem
from .kernel import IncidentReuseKernel

__all__ = [
    "IncidentReuseKernel",
    "MATERIALIZE_THRESHOLD",
    "MAX_MATERIALIZED",
    "SCHEMA_VERSION",
    "generate_problem",
]
