"""Default deterministic demonstration configuration."""

from __future__ import annotations

from .models import (
    CATEGORY,
    MEAN_SHIFT,
    NUMERIC,
    DriftEvent,
    FeatureSpec,
    MonitorConfig,
    ResourceLimits,
)


def default_features() -> tuple[FeatureSpec, ...]:
    return (
        FeatureSpec(
            name="channel",
            kind=CATEGORY,
            levels=("app", "partner", "web"),
            probabilities=(0.55, 0.28, 0.17),
            description="访问渠道",
        ),
        FeatureSpec(
            name="device_score",
            kind=NUMERIC,
            mean=0.55,
            std=0.08,
            description="设备可信评分",
        ),
        FeatureSpec(
            name="region",
            kind=CATEGORY,
            levels=("east", "north", "south", "west"),
            probabilities=(0.38, 0.27, 0.22, 0.13),
            description="客户区域",
        ),
        FeatureSpec(
            name="session_minutes",
            kind=NUMERIC,
            mean=14.0,
            std=3.0,
            description="会话时长",
        ),
        FeatureSpec(
            name="transaction_amount",
            kind=NUMERIC,
            mean=120.0,
            std=18.0,
            description="交易金额",
        ),
    )


def default_drift_events() -> tuple[DriftEvent, ...]:
    return (
        DriftEvent(batch=3, feature="region", kind="category_shift", probabilities=(0.12, 0.18, 0.30, 0.40)),
        DriftEvent(batch=3, feature="transaction_amount", kind="mean_shift", magnitude=2.7),
        DriftEvent(batch=4, feature="session_minutes", kind="variance_scale", magnitude=4.0),
        DriftEvent(batch=5, feature="channel", kind="missing_rate", missing_rate=0.35),
        DriftEvent(batch=5, feature="device_score", kind="mean_shift", magnitude=-2.0),
    )


def default_config() -> MonitorConfig:
    return MonitorConfig(
        seed=20261121,
        n_batches=8,
        batch_size=64,
        window_size=3,
        slide_step=1,
        numeric_test=MEAN_SHIFT,
        numeric_threshold=0.8,
        category_threshold=0.20,
        config_version=1,
        features=default_features(),
        drift_events=default_drift_events(),
        limits=ResourceLimits(),
    )

