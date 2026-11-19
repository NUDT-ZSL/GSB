"""Public API for the deterministic multi-label experiment lab."""

from __future__ import annotations

from typing import Any, Iterable

from .config import create_projection, default_config, feature_dimension
from .hashing import canonical_json_bytes, sha256_json
from .injection import Injection, load_injections, normalize_injections
from .metrics import compute_metrics, recompute_metrics_from_details
from .pipeline import apply_resource_limits, classify_experiment
from .samples import build_default_payloads, generate_annotations, generate_samples
from .snapshot import SnapshotStore
from .verifier import recompute_report_metrics, verify_experiment, verify_report


def classify(
    snapshot: dict[str, Any],
    injections: Iterable[Injection | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classification entry point (normal runs use injections=None)."""

    return classify_experiment(snapshot, injections)


def metrics(
    sample_records: Iterable[dict[str, Any]],
    labels: list[str],
    *,
    uncertain_strategy: str,
    zero_division: str | None,
) -> dict[str, Any]:
    """Metric entry point."""

    return compute_metrics(
        sample_records,
        labels,
        uncertain_strategy=uncertain_strategy,
        zero_division=zero_division,
    )


def inject(
    snapshot: dict[str, Any],
    injections: Iterable[Injection | dict[str, Any]],
) -> dict[str, Any]:
    """Injection entry point; incompatible only in requiring a plan."""

    return classify_experiment(snapshot, injections)


def verify(
    snapshot: dict[str, Any],
    injections: Iterable[Injection | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verification entry point."""

    return verify_experiment(snapshot, injections)


__all__ = [
    "Injection",
    "SnapshotStore",
    "apply_resource_limits",
    "build_default_payloads",
    "canonical_json_bytes",
    "classify",
    "classify_experiment",
    "compute_metrics",
    "create_projection",
    "default_config",
    "feature_dimension",
    "generate_annotations",
    "generate_samples",
    "inject",
    "load_injections",
    "metrics",
    "normalize_injections",
    "recompute_metrics_from_details",
    "recompute_report_metrics",
    "sha256_json",
    "verify",
    "verify_experiment",
    "verify_report",
]
