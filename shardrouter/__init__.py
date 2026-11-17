"""Public API for the deterministic sharding-routing teaching component."""

from .algorithms import build_virtual_ring, route_table
from .cache import CacheLookup, RouteCacheKey, RoutingCache
from .config import config_summary, load_config, parse_config, validate_config_file
from .dependency import DependencyGraph
from .errors import (
    E_CACHE,
    E_CONFIG,
    E_DEPEND,
    E_RING,
    E_SNAPSHOT,
    E_SQL,
    E_VERSION,
    RouterError,
)
from .models import (
    ColumnConstraint,
    ColumnDef,
    ConsistentHashAlgorithm,
    ModAlgorithm,
    PredicateInfo,
    RangeAlgorithm,
    RingNode,
    RouteUnit,
    RouterConfig,
    TableConfig,
)
from .predicates import normalize_predicate, render_value
from .session import (
    RouterSession,
    restore_snapshot,
    route_predicate,
    save_snapshot,
)
from .ui import build_ui_model, launch_ui
from .util import canonical_json, pretty_json
from .verification import canonical_text, compare_canonical, verify_repeated

__all__ = [
    "CacheLookup",
    "ColumnConstraint",
    "ColumnDef",
    "ConsistentHashAlgorithm",
    "DependencyGraph",
    "E_CACHE",
    "E_CONFIG",
    "E_DEPEND",
    "E_RING",
    "E_SNAPSHOT",
    "E_SQL",
    "E_VERSION",
    "ModAlgorithm",
    "PredicateInfo",
    "RangeAlgorithm",
    "RingNode",
    "RouteCacheKey",
    "RouteUnit",
    "RouterConfig",
    "RouterError",
    "RouterSession",
    "RoutingCache",
    "TableConfig",
    "build_ui_model",
    "build_virtual_ring",
    "canonical_json",
    "canonical_text",
    "compare_canonical",
    "config_summary",
    "launch_ui",
    "load_config",
    "normalize_predicate",
    "parse_config",
    "pretty_json",
    "render_value",
    "restore_snapshot",
    "route_predicate",
    "route_table",
    "save_snapshot",
    "validate_config_file",
    "verify_repeated",
]
