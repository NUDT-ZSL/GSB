"""Straightforward serial reference path without cache or process scheduling."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from .binning import compute_boundaries
from .config import normalize_config
from .encoding import build_encoding_context, build_feature_spec, encode_rows
from .validation import validate_samples


def reference_encode(
    rows: Sequence[Mapping[str, Any]],
    raw_config: Mapping[str, Any],
    *,
    sample_content_hash: Optional[str] = None,
) -> Dict[str, Any]:
    config = normalize_config(raw_config)
    validation = validate_samples(rows, config)
    ordered_rows = validation["rows"]
    boundaries = compute_boundaries(ordered_rows, config)
    feature_spec = build_feature_spec(config, boundaries)
    context = build_encoding_context(ordered_rows, config, boundaries)
    batch = encode_rows(
        ordered_rows,
        config,
        boundaries,
        feature_spec,
        context,
        batch_id="reference",
        isolate_failures=False,
    )
    return {
        "sample_content_hash": sample_content_hash
        or validation["sample_content_hash"],
        "boundaries": boundaries,
        "feature_spec": feature_spec,
        "context": context,
        "rows": batch["row_results"],
        "comparison_count": batch["comparison_count"] + context["comparison_count"],
        "error_interval": batch["error_interval"],
    }
