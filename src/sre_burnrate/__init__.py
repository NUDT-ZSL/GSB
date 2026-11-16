"""Public API for the deterministic SRE error-budget burn-rate engine."""

from .aggregation import (
    AggregationResult,
    FullWindowAggregator,
    StreamingWindowAggregator,
    aggregate_events,
)
from .alerts import AlertEvaluation, AlertStateMachine
from .budget import BudgetMetrics, compute_budget_metrics, compute_burn_rate
from .config import (
    AdaptivePolicy,
    AlertPolicy,
    EvaluationConfig,
    PrecisionPolicy,
    SLISpec,
    SLOSpec,
    WindowSpec,
    load_config,
)
from .engine import evaluate_file
from .errors import (
    ConfigError,
    DomainError,
    InputError,
    ReplayError,
    SLIValidationError,
    SnapshotError,
)
from .fixed import FixedPoint, format_scaled_int, quantize
from .loader import LoadedSLI, SLIEventStream, load_sli
from .models import (
    AlertRecord,
    AlertTransition,
    EvaluationSession,
    EvaluationStep,
    Event,
    ResourceReport,
    SourceMetadata,
    WindowResult,
)
from .replay import ReplayReport, replay_snapshot
from .snapshot import (
    RestoreReport,
    RestoreResult,
    Snapshot,
    load_snapshot,
    restore_snapshot,
    save_snapshot,
)

__all__ = [
    "AdaptivePolicy",
    "AggregationResult",
    "AlertEvaluation",
    "AlertPolicy",
    "AlertRecord",
    "AlertStateMachine",
    "AlertTransition",
    "BudgetMetrics",
    "ConfigError",
    "DomainError",
    "EvaluationConfig",
    "EvaluationSession",
    "EvaluationStep",
    "Event",
    "FixedPoint",
    "FullWindowAggregator",
    "InputError",
    "LoadedSLI",
    "PrecisionPolicy",
    "ReplayError",
    "ReplayReport",
    "ResourceReport",
    "RestoreReport",
    "RestoreResult",
    "SLIEventStream",
    "SLISpec",
    "SLOSpec",
    "SLIValidationError",
    "Snapshot",
    "SnapshotError",
    "SourceMetadata",
    "StreamingWindowAggregator",
    "WindowResult",
    "WindowSpec",
    "aggregate_events",
    "compute_budget_metrics",
    "compute_burn_rate",
    "evaluate_file",
    "format_scaled_int",
    "load_config",
    "load_sli",
    "load_snapshot",
    "quantize",
    "replay_snapshot",
    "restore_snapshot",
    "save_snapshot",
]

__version__ = "0.1.0"
