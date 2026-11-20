"""静音段能量事件归因。"""

import numpy as np

from .config import (
    AttributionConfig,
    CONTROLLED_CATEGORIES,
    CATEGORY_FLAT,
    CATEGORY_DEVICE,
    CATEGORY_CROSSTALK,
)
from .errors import DomainError, E_SOURCE_UNKNOWN
from .models import AttributionResult, category_name


def validate_controlled(allowed_categories):
    """配置声明的类别集合必须是受控集合的子集，否则 E_SOURCE_UNKNOWN。"""
    allowed = tuple(sorted(allowed_categories))
    for category in allowed:
        if category not in CONTROLLED_CATEGORIES:
            raise DomainError(
                E_SOURCE_UNKNOWN,
                "归因类别不在受控集合",
                category=category,
                controlled=list(CONTROLLED_CATEGORIES),
            )
    return allowed


def silence_metrics(energies, floor, segment):
    """段内能量相对噪声底估计的偏离量与波动幅度（dB）。"""
    block_energy = energies[segment.start_frame:segment.end_frame]
    block_floor = floor[segment.start_frame:segment.end_frame]
    residual = block_energy - block_floor
    deviation = float(np.mean(np.abs(residual)))
    flux = float(np.std(residual))
    return deviation, flux


def classify(deviation_db, flux_db, config: AttributionConfig):
    """显式规则归因；多类别同时命中时按类别编号升序打破平局。"""
    hits = []
    if (
        deviation_db >= config.dev_device_db
        and flux_db < config.flux_device_db
    ):
        hits.append(CATEGORY_DEVICE)
    if (
        flux_db >= config.flux_crosstalk_db
        and deviation_db >= config.dev_crosstalk_db
    ):
        hits.append(CATEGORY_CROSSTALK)
    if hits:
        return min(hits)
    return CATEGORY_FLAT


def attribute_segment(
    segment,
    energies,
    floor,
    config: AttributionConfig,
    noise_version,
    threshold_version,
):
    """对单个静音段产出归因结果与血缘记录。"""
    deviation, flux = silence_metrics(energies, floor, segment)
    category = classify(deviation, flux, config)
    if category not in CONTROLLED_CATEGORIES:
        raise DomainError(
            E_SOURCE_UNKNOWN,
            "归因类别不在受控集合",
            category=category,
            silence_id=segment.silence_id,
        )
    result = AttributionResult(
        silence_id=segment.silence_id,
        start_frame=segment.start_frame,
        end_frame=segment.end_frame,
        category=category,
        category_name=category_name(category),
        deviation_db=deviation,
        flux_db=flux,
        noise_version=noise_version,
        threshold_version=threshold_version,
    )
    lineage = {
        "silence_id": segment.silence_id,
        "frame_range": [segment.start_frame, segment.end_frame],
        "noise_window_frames": None,  # 由调用方填充
        "noise_hop_frames": None,
        "noise_quantile": None,
        "noise_version": noise_version,
        "threshold_version": threshold_version,
        "attribution_thresholds": {
            "dev_device_db": config.dev_device_db,
            "flux_device_db": config.flux_device_db,
            "flux_crosstalk_db": config.flux_crosstalk_db,
            "dev_crosstalk_db": config.dev_crosstalk_db,
        },
        "deviation_db": deviation,
        "flux_db": flux,
        "category": category,
    }
    return result, lineage


def attribute_all(
    silence, energies, floor, config, noise_version, threshold_version, noise_config
):
    """批量归因，并在血缘中固化估计窗口参数。"""
    results = []
    lineage = {}
    for segment in silence:
        result, record = attribute_segment(
            segment, energies, floor, config, noise_version, threshold_version
        )
        record["noise_window_frames"] = noise_config.window_frames
        record["noise_hop_frames"] = noise_config.hop_frames
        record["noise_quantile"] = noise_config.quantile
        results.append(result)
        lineage[segment.silence_id] = record
    return results, lineage

