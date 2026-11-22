"""Deterministic window splitting and feature construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from .errors import ConfigInvalid
from .series import SeriesData
from .utils import array_digest, matrix_digest, sha256_json


@dataclass
class Window:
    window_id: str
    window_index: int
    start_index: int
    split: str
    history_start: int
    history_end: int
    target_start: int
    target_end: int
    feature_anchor_start: int
    feature_anchor_end: int
    feature_columns: list[str]
    feature_matrix: np.ndarray
    one_step_targets: np.ndarray
    target_vector: np.ndarray

    @property
    def feature_rows(self) -> int:
        return int(self.feature_matrix.shape[0])

    @property
    def feature_count(self) -> int:
        return int(self.feature_matrix.shape[1])

    @property
    def feature_digest(self) -> str:
        return matrix_digest(self.feature_matrix)

    @property
    def target_digest(self) -> str:
        return array_digest(self.target_vector)

    @property
    def boundary_digest(self) -> str:
        return sha256_json(
            {
                "window_id": self.window_id,
                "start_index": self.start_index,
                "history": [self.history_start, self.history_end],
                "target": [self.target_start, self.target_end],
                "feature_anchors": [
                    self.feature_anchor_start,
                    self.feature_anchor_end,
                ],
                "feature_columns": self.feature_columns,
                "feature_digest": self.feature_digest,
                "target_digest": self.target_digest,
            }
        )

    def summary(self) -> dict[str, Any]:
        column_stats: dict[str, dict[str, float]] = {}
        for column_index, name in enumerate(self.feature_columns):
            values = self.feature_matrix[:, column_index]
            column_stats[name] = {
                "mean": float(np.mean(values)),
                "std": float(np.std(values)),
                "min": float(np.min(values)),
                "max": float(np.max(values)),
            }
        return {
            "window_id": self.window_id,
            "window_index": self.window_index,
            "split": self.split,
            "start_index": self.start_index,
            "history_bounds": [self.history_start, self.history_end],
            "target_bounds": [self.target_start, self.target_end],
            "feature_anchor_bounds": [
                self.feature_anchor_start,
                self.feature_anchor_end,
            ],
            "feature_rows": self.feature_rows,
            "feature_columns": list(self.feature_columns),
            "feature_digest": self.feature_digest,
            "one_step_target_digest": array_digest(self.one_step_targets),
            "target_digest": self.target_digest,
            "boundary_digest": self.boundary_digest,
            "column_stats": column_stats,
        }


@dataclass
class WindowCollection:
    windows: list[Window]
    train_windows: list[Window]
    test_windows: list[Window]
    feature_columns: list[str]
    split_index: int
    separation_gap: int
    lookback: int

    @property
    def train_ids(self) -> list[str]:
        return [window.window_id for window in self.train_windows]

    @property
    def test_ids(self) -> list[str]:
        return [window.window_id for window in self.test_windows]

    @property
    def collection_digest(self) -> str:
        return sha256_json(
            {
                "split_index": self.split_index,
                "feature_columns": self.feature_columns,
                "windows": [
                    {
                        "id": window.window_id,
                        "split": window.split,
                        "boundary": window.boundary_digest,
                    }
                    for window in self.windows
                ],
            }
        )

    def summary(self) -> dict[str, Any]:
        train_end = max(
            (window.target_end for window in self.train_windows),
            default=0,
        )
        test_start = min(
            (window.start_index for window in self.test_windows),
            default=0,
        )
        return {
            "feature_columns": list(self.feature_columns),
            "lookback": self.lookback,
            "split_index": self.split_index,
            "train_count": len(self.train_windows),
            "test_count": len(self.test_windows),
            "train_window_ids": self.train_ids,
            "test_window_ids": self.test_ids,
            "train_target_end_max": train_end,
            "test_start_min": test_start,
            "separation_gap": self.separation_gap,
            "no_train_test_overlap": train_end <= test_start,
            "collection_digest": self.collection_digest,
        }


def feature_columns(config: Mapping[str, Any], series: SeriesData) -> list[str]:
    features = config["features"]
    columns: list[str] = []
    columns.extend(f"lag_{lag}" for lag in features["lags"])
    columns.extend(
        f"rolling_{item['stat']}_{item['window']}" for item in features["rolling"]
    )
    columns.extend(f"diff_{order}" for order in features["differences"])
    if features["include_exogenous"]:
        columns.extend(f"exog_{name}" for name in series.exogenous)
    if features["include_events"]:
        columns.extend(f"event_{name}" for name in series.events)
    if not columns:
        raise ConfigInvalid(
            "features",
            "at least one target feature column is required",
        )
    return columns


def _rolling_value(
    history: Sequence[float],
    anchor: int,
    origin: int,
    stat: str,
    size: int,
) -> float:
    local_anchor = anchor - origin
    values = np.asarray(history[local_anchor - size + 1 : local_anchor + 1], dtype=np.float64)
    if stat == "mean":
        return float(np.mean(values))
    if stat == "std":
        return float(np.std(values))
    if stat == "min":
        return float(np.min(values))
    if stat == "max":
        return float(np.max(values))
    raise ConfigInvalid("features.rolling.stat", f"unsupported rolling stat: {stat}")


def _feature_row_from_history(
    history: Sequence[float],
    origin: int,
    anchor: int,
    series: SeriesData,
    config: Mapping[str, Any],
) -> np.ndarray:
    local_anchor = anchor - origin
    features = config["features"]
    values: list[float] = []
    for lag in features["lags"]:
        values.append(float(history[local_anchor - lag + 1]))
    for item in features["rolling"]:
        values.append(
            _rolling_value(
                history,
                anchor,
                origin,
                str(item["stat"]),
                int(item["window"]),
            )
        )
    for order in features["differences"]:
        values.append(
            float(history[local_anchor]) - float(history[local_anchor - order])
        )
    if features["include_exogenous"]:
        for name in series.exogenous:
            values.append(float(series.exogenous[name][anchor]))
    if features["include_events"]:
        for name in series.events:
            values.append(float(series.events[name][anchor]))
    return np.asarray(values, dtype=np.float64)


def feature_row_for_anchor(
    series: SeriesData,
    anchor: int,
    config: Mapping[str, Any],
) -> np.ndarray:
    """Build one feature row using only target samples at or before anchor."""

    lookback = int(config["_feature_lookback"])
    if anchor < lookback:
        raise ConfigInvalid(
            "features",
            f"anchor {anchor} is before the required lookback {lookback}",
        )
    history = series.target[: anchor + 1]
    return _feature_row_from_history(history, 0, anchor, series, config)


def build_feature_row_from_history(
    history: Sequence[float],
    origin: int,
    anchor: int,
    series: SeriesData,
    config: Mapping[str, Any],
) -> np.ndarray:
    """Public feature operator for training and recursive prediction."""

    return _feature_row_from_history(history, origin, anchor, series, config)


def build_windows(series: SeriesData, config: Mapping[str, Any]) -> WindowCollection:
    """Build all windows and enforce a hard train/test temporal separation."""

    window_config = config["window"]
    length = int(window_config["length"])
    horizon = int(window_config["horizon"])
    split_index = int(window_config["split_index"])
    lookback = int(config["_feature_lookback"])
    series_length = series.length
    columns = feature_columns(config, series)
    max_start = series_length - length - horizon
    if max_start < 0:
        raise ConfigInvalid(
            "series.length",
            "series is shorter than window length plus horizon",
        )

    starts = list(range(max_start + 1))
    train_starts = [
        start for start in starts if start + length + horizon <= split_index
    ]
    test_starts = [start for start in starts if start >= split_index]
    if not train_starts or not test_starts:
        raise ConfigInvalid(
            "window.split_index",
            "split must produce at least one train and one test window",
        )

    windows: list[Window] = []
    for ordinal, start in enumerate(sorted(train_starts + test_starts)):
        split = "train" if start in train_starts else "test"
        history_start = start
        history_end = start + length
        target_start = history_end
        target_end = target_start + horizon
        feature_anchor_start = start + lookback
        feature_anchor_end = history_end
        anchors = range(feature_anchor_start, feature_anchor_end)
        features = [
            _feature_row_from_history(
                series.target[history_start:history_end],
                history_start,
                anchor,
                series,
                config,
            )
            for anchor in anchors
        ]
        feature_matrix = np.ascontiguousarray(
            np.vstack(features),
            dtype=np.float64,
        )
        one_step_targets = np.ascontiguousarray(
            series.target[feature_anchor_start + 1 : feature_anchor_end + 1],
            dtype=np.float64,
        )
        target_vector = np.ascontiguousarray(
            series.target[target_start:target_end],
            dtype=np.float64,
        )
        windows.append(
            Window(
                window_id=f"w{start:06d}",
                window_index=ordinal,
                start_index=start,
                split=split,
                history_start=history_start,
                history_end=history_end,
                target_start=target_start,
                target_end=target_end,
                feature_anchor_start=feature_anchor_start,
                feature_anchor_end=feature_anchor_end,
                feature_columns=columns,
                feature_matrix=feature_matrix,
                one_step_targets=one_step_targets,
                target_vector=target_vector,
            )
        )

    train_windows = [window for window in windows if window.split == "train"]
    test_windows = [window for window in windows if window.split == "test"]
    train_target_end_max = max(window.target_end for window in train_windows)
    test_start_min = min(window.start_index for window in test_windows)
    separation_gap = test_start_min - train_target_end_max
    if separation_gap < 0:
        raise ConfigInvalid(
            "window.split_index",
            "train and test windows overlap in time",
        )
    return WindowCollection(
        windows=windows,
        train_windows=train_windows,
        test_windows=test_windows,
        feature_columns=columns,
        split_index=split_index,
        separation_gap=separation_gap,
        lookback=lookback,
    )


def ordered_training_rows(
    collection: WindowCollection,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    """Flatten train windows in (window start, anchor) order."""

    matrices: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    row_map: list[dict[str, Any]] = []
    for window in collection.train_windows:
        matrices.append(window.feature_matrix)
        targets.append(window.one_step_targets)
        for offset, anchor in enumerate(
            range(window.feature_anchor_start, window.feature_anchor_end)
        ):
            row_map.append(
                {
                    "window_id": window.window_id,
                    "window_index": window.window_index,
                    "anchor_index": anchor,
                    "row_in_window": offset,
                }
            )
    return (
        np.ascontiguousarray(np.vstack(matrices), dtype=np.float64),
        np.ascontiguousarray(np.concatenate(targets), dtype=np.float64),
        row_map,
    )
