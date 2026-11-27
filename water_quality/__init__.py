"""SQLite predicate-pushdown kernel for water-quality monitoring."""

from .engine import (
    EngineError,
    ExecutionInvariantError,
    PartitionVersionMismatchError,
    PushdownEngine,
)
from .generator import GeneratorConfig, generate_database, verify_reproducible
from .predicates import (
    Predicate,
    PredicateError,
    PredicateGroup,
    PredicateSet,
    QuerySpec,
    kleene_and,
    kleene_or,
    parse_reading_groups,
)
from .rules import PartitionRules
from .snapshots import create_snapshot, resume_snapshot

__all__ = [
    "EngineError",
    "ExecutionInvariantError",
    "GeneratorConfig",
    "PartitionRules",
    "PartitionVersionMismatchError",
    "Predicate",
    "PredicateError",
    "PredicateGroup",
    "PredicateSet",
    "PushdownEngine",
    "QuerySpec",
    "create_snapshot",
    "generate_database",
    "kleene_and",
    "kleene_or",
    "parse_reading_groups",
    "resume_snapshot",
    "verify_reproducible",
]
