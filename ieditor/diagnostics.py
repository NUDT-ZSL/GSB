"""Cross-version render consistency diagnostics."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .config import CHANNELS, RenderConfig, VersionMap
from .errors import E_OK
from .fingerprint import structure_fingerprint
from .models import UnifiedProject
from .pipeline import RenderArtifacts


def _weights(project: UnifiedProject,
             version_map: Optional[VersionMap]) -> Dict[str, float]:
    if project.style.channel_weights:
        return dict(project.style.channel_weights)
    if version_map is None:
        return {c: 0.0 for c in CHANNELS}
    return version_map.weights_for(project.style.intensity)


def compare_renders(project_a: UnifiedProject,
                    project_b: UnifiedProject,
                    artifacts_a: RenderArtifacts,
                    artifacts_b: RenderArtifacts,
                    config: RenderConfig,
                    version_map: Optional[VersionMap] = None) -> Dict[str, Any]:
    styled_a = artifacts_a.styled.astype(np.int16)
    styled_b = artifacts_b.styled.astype(np.int16)
    if styled_a.shape != styled_b.shape:
        diff = np.abs(styled_a.astype(np.int32) -
                      np.pad(styled_b, ((0, max(0, styled_a.shape[0] -
                                                 styled_b.shape[0])),
                                        (0, 0), (0, 0)))[:styled_a.shape[0]])
    else:
        diff = np.abs(styled_a.astype(np.int32) - styled_b.astype(np.int32))
    per_channel_max = {c: int(diff[..., i].max())
                       for i, c in enumerate(CHANNELS)}
    pixel_max = diff.max(axis=2)
    differing_pixels = int(np.count_nonzero(pixel_max > 0))

    mask_a = project_a.mask_bitmap()
    mask_b = project_b.mask_bitmap()
    mask_xor = int(np.count_nonzero(mask_a != mask_b))
    mask_coverage = {
        "a_damaged_pixels": int(mask_a.sum()),
        "b_damaged_pixels": int(mask_b.sum()),
        "xor_pixels": mask_xor,
    }

    weights_a = _weights(project_a, version_map)
    weights_b = _weights(project_b, version_map)
    per_channel_dev = {c: float(abs(weights_a[c] - weights_b[c]))
                       for c in CHANNELS}
    # When project_a is itself a re-upgraded project, its folded weights mask
    # the real downgrade deviation. Recover the original pre-fold weights from
    # the recorded intensity_fold loss when available.
    original_weights = None
    for entry in project_a.loss_log:
        if entry.kind == "intensity_fold":
            original_weights = entry.values.get("original_weights")
            break
    effective_weights_a = (original_weights if original_weights
                           else weights_a)
    if original_weights:
        per_channel_dev = {
            c: float(abs(float(effective_weights_a[c]) - weights_b[c]))
            for c in CHANNELS}
    max_deviation = max(per_channel_dev.values())
    threshold = float(config.deviation_threshold)
    warning = max_deviation > threshold

    return {
        "business_code": E_OK,
        "project_id": project_a.project_id,
        "max_channel_diff": per_channel_max,
        "max_abs_diff": int(diff.max()),
        "differing_pixel_count": differing_pixels,
        "mask_coverage": mask_coverage,
        "style_weight_deviation": {
            "per_channel": per_channel_dev,
            "max": max_deviation,
            "threshold": threshold,
            "weights_a": weights_a,
            "weights_b": weights_b,
            "original_pre_fold_weights": effective_weights_a,
        },
        "version_compat_warning": bool(warning),
        "loss_log": [entry.to_dict() for entry in project_b.loss_log],
        "fingerprints": {
            "project_a": structure_fingerprint(project_a),
            "project_b": structure_fingerprint(project_b),
        },
        "source_versions": {
            "project_a": project_a.source_version,
            "project_b": project_b.source_version,
        },
        "render_stats": {
            "a_inpaint": artifacts_a.inpaint_stats,
            "b_inpaint": artifacts_b.inpaint_stats,
        },
    }
