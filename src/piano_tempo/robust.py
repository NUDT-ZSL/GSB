"""滚动预测残差的稳健标准化（中位数 + MAD）异常判定。"""
from __future__ import annotations

import numpy as np

from .forecast import rolling_one_step
from .models import AnomalyInterval, TempoCurve


def robust_z(values: np.ndarray) -> np.ndarray:
    """基于中位数与中位数绝对偏差的稳健标准化值。"""
    values = np.asarray(values, dtype=float)
    median = np.nanmedian(values)
    mad = np.nanmedian(np.abs(values - median))
    scale = 1.4826 * mad
    if scale < 1e-9:
        std = np.nanstd(values)
        scale = std if std > 1e-9 else 1.0
    return (values - median) / scale


def detect_robust_anomalies(
    curve: TempoCurve,
    n_breakpoints: int,
    ar_order: int,
    min_train_points: int,
    z_threshold: float,
    merge_gap_beats: float,
) -> tuple[list[AnomalyInterval], dict[str, np.ndarray]]:
    """滚动一步预测残差经稳健标准化后，超阈值网格点合并为异常片段。

    每个片段附带证据区间：原始观测、预测值、残差与标准化值。
    """
    x = curve.grid_beats
    y = curve.tempo_bpm
    rolling = rolling_one_step(
        x, y, n_breakpoints, ar_order, min_train_points
    )
    predicted = rolling["predicted"]
    valid = ~np.isnan(predicted)
    residuals = np.full_like(y, np.nan)
    residuals[valid] = y[valid] - predicted[valid]
    standardized = np.full_like(y, np.nan)
    standardized[valid] = robust_z(residuals[valid])

    evidence = {
        "grid_beats": x,
        "observed": y,
        "predicted": predicted,
        "residual": residuals,
        "robust_z": standardized,
    }

    flagged = valid & (np.abs(standardized) > z_threshold)
    anomalies: list[AnomalyInterval] = []
    start: int | None = None
    gap_cells = max(1, int(round(merge_gap_beats / max(curve.grid_step_beats, 1e-9))))
    for idx in range(len(x)):
        if flagged[idx] and start is None:
            start = idx
        if start is not None:
            gap_end = min(len(x), idx + gap_cells + 1)
            nearby_flag = flagged[idx:gap_end].any()
            if not nearby_flag or idx == len(x) - 1:
                end = idx if not nearby_flag else idx + 1
                segment_z = standardized[start:end]
                direction = "accel" if np.nanmean(segment_z) > 0 else "ritard"
                anomalies.append(
                    AnomalyInterval(
                        start_beat=float(x[start]),
                        end_beat=float(x[end - 1] + curve.grid_step_beats),
                        direction=direction,
                        magnitude=float(
                            np.nanmax(np.abs(segment_z))
                        ),
                        kind="robust_residual",
                        detail={
                            "evidence_window": {
                                "grid_beats": x[start:end].tolist(),
                                "observed": y[start:end].tolist(),
                                "predicted": predicted[start:end].tolist(),
                                "residual": residuals[start:end].tolist(),
                                "robust_z": standardized[start:end].tolist(),
                            },
                            "z_threshold": z_threshold,
                        },
                    )
                )
                start = None
    return anomalies, evidence
