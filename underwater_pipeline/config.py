"""Configuration shared by generation, analysis and reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PipelineConfig:
    # Frame boundary measure.
    hist_bins: int = 16
    edge_threshold: float = 0.075
    hist_weight: float = 0.72
    edge_weight: float = 0.28
    edge_change_scale: float = 0.14
    adaptive_mad_k: float = 3.25
    adaptive_window: int = 18
    adaptive_floor: float = 0.026
    local_peak_radius: int = 2

    # Shot stabilization.
    min_shot_frames: int = 12
    min_shot_seconds: float = 1.0

    # Visibility scoring.
    contrast_block: int = 8
    dark_channel_radius: int = 2
    contrast_scale: float = 0.075
    dark_reference: float = 0.43
    dark_scale: float = 0.38
    contrast_weight: float = 0.55
    dark_weight: float = 0.45
    clear_score: float = 65.0
    light_score: float = 48.0
    moderate_score: float = 30.0

    # Visibility segmentation and events.
    segment_penalty: float = 70.0
    visibility_change_points: float = 10.0
    frame_smooth_width: int = 5
    turbidity_reference_percentile: float = 75.0
    turbidity_drop_points: float = 13.0
    min_turbidity_frames: int = 6
    turbidity_gap_fill: int = 2
    visibility_jump_threshold: float = 8.0
    turbidity_event_radius: int = 12
    turbidity_change_merge_radius: int = 20
    visibility_evidence_scale: float = 50.0
    mixed_event_visibility_bias: float = 1.05

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_CONFIG = PipelineConfig()
