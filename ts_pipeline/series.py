"""Deterministic local multivariate time-series generation and persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .config import normalize_config
from .errors import ConfigInvalid
from .utils import array_digest, sha256_json, to_jsonable


@dataclass
class SeriesData:
    """In-memory deterministic sample.

    Every array is float64. Event arrays contain only 0.0 and 1.0.
    """

    target: np.ndarray
    exogenous: dict[str, np.ndarray] = field(default_factory=dict)
    events: dict[str, np.ndarray] = field(default_factory=dict)
    seed: int = 0

    def __post_init__(self) -> None:
        self.target = np.ascontiguousarray(self.target, dtype=np.float64)
        if self.target.ndim != 1 or self.target.size == 0:
            raise ConfigInvalid("series.target", "target must be a non-empty 1-D array")
        self.exogenous = {
            str(name): np.ascontiguousarray(values, dtype=np.float64)
            for name, values in self.exogenous.items()
        }
        self.events = {
            str(name): np.ascontiguousarray(values, dtype=np.float64)
            for name, values in self.events.items()
        }
        for name, values in {**self.exogenous, **self.events}.items():
            if values.ndim != 1 or values.size != self.target.size:
                raise ConfigInvalid(
                    f"series.{name}",
                    f"{name} must be a 1-D array with the target length",
                )
        for name, values in self.events.items():
            if not np.all((values == 0.0) | (values == 1.0)):
                raise ConfigInvalid(
                    f"series.events.{name}",
                    "event arrays must contain only 0 and 1",
                )

    @property
    def length(self) -> int:
        return int(self.target.size)

    @property
    def digest(self) -> str:
        return array_digest(self.target)

    def column(self, name: str) -> np.ndarray:
        if name in self.exogenous:
            return self.exogenous[name]
        if name in self.events:
            return self.events[name]
        raise KeyError(name)

    def summary(self) -> dict[str, Any]:
        def stats(values: np.ndarray) -> dict[str, Any]:
            return {
                "count": int(values.size),
                "mean": float(np.mean(values)),
                "std": float(np.std(values)),
                "min": float(np.min(values)),
                "max": float(np.max(values)),
                "sha256": array_digest(values),
            }

        return {
            "seed": self.seed,
            "length": self.length,
            "target": stats(self.target),
            "exogenous": {
                name: stats(values) for name, values in self.exogenous.items()
            },
            "events": {name: stats(values) for name, values in self.events.items()},
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "format": "ts-precision-series/v1",
            "seed": self.seed,
            "length": self.length,
            "target": [float(value) for value in self.target],
            "exogenous": [
                {"name": name, "values": [float(value) for value in values]}
                for name, values in self.exogenous.items()
            ],
            "events": [
                {"name": name, "values": [float(value) for value in values]}
                for name, values in self.events.items()
            ],
        }

    def content_checksum(self) -> str:
        return sha256_json(
            {
                "seed": self.seed,
                "target_sha256": array_digest(self.target),
                "exogenous": {
                    name: array_digest(values)
                    for name, values in self.exogenous.items()
                },
                "events": {
                    name: array_digest(values) for name, values in self.events.items()
                },
            }
        )


def _cycle_component(index: np.ndarray, cycles: list[Mapping[str, Any]]) -> np.ndarray:
    result = np.zeros(index.shape, dtype=np.float64)
    for cycle in cycles:
        result += float(cycle["amplitude"]) * np.sin(
            (2.0 * np.pi * index / float(cycle["period"]))
            + float(cycle["phase"])
        )
    return result


def generate_series(config_or_raw: Mapping[str, Any] | None = None) -> SeriesData:
    """Generate the complete sample using only explicit per-stream seeds."""

    config = (
        config_or_raw
        if "_feature_lookback" in (config_or_raw or {})
        else normalize_config(config_or_raw)
    )
    series_config = config["series"]
    length = int(series_config["length"])
    index = np.arange(length, dtype=np.float64)

    exogenous: dict[str, np.ndarray] = {}
    for item in series_config["exogenous"]:
        rng = np.random.default_rng(int(item["seed"]))
        trend = float(item["trend"]) * index
        cycles = _cycle_component(
            index,
            [
                {
                    "amplitude": item["amplitude"],
                    "period": item["period"],
                    "phase": item["phase"],
                }
            ],
        )
        noise = rng.normal(0.0, float(item["noise_std"]), size=length)
        exogenous[str(item["name"])] = np.ascontiguousarray(
            trend + cycles + noise,
            dtype=np.float64,
        )

    events: dict[str, np.ndarray] = {}
    for item in series_config["events"]:
        rng = np.random.default_rng(int(item["seed"]))
        draws = rng.random(length)
        events[str(item["name"])] = np.ascontiguousarray(
            (draws < float(item["probability"])).astype(np.float64),
            dtype=np.float64,
        )

    target_config = series_config["target"]
    target_rng = np.random.default_rng(int(config["seed"]))
    target = float(target_config["trend"]) * index
    target = target + _cycle_component(index, target_config["cycles"])
    target = target + target_rng.normal(
        0.0,
        float(target_config["noise_std"]),
        size=length,
    )
    event_effects = target_config.get("event_effects", {})
    for name, weight in event_effects.items():
        if name not in events:
            raise ConfigInvalid(
                f"series.target.event_effects.{name}",
                f"event effect references unknown event {name!r}",
            )
        impulse = np.zeros(length, dtype=np.float64)
        if length > 1:
            impulse[1:] = events[name][:-1]
        target = target + float(weight) * impulse

    return SeriesData(
        target=np.ascontiguousarray(target, dtype=np.float64),
        exogenous=exogenous,
        events=events,
        seed=int(config["seed"]),
    )


def save_series(series: SeriesData, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = series.to_payload()
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            to_jsonable(payload),
            handle,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    return destination


def load_series(path: str | Path) -> SeriesData:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigInvalid("series_file", f"series file not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigInvalid(
            "series_file",
            f"series file is not valid JSON: {exc.msg} at line {exc.lineno}",
        ) from exc
    if not isinstance(payload, Mapping):
        raise ConfigInvalid("series_file", "series payload must be an object")
    target_raw = payload.get("target")
    if not isinstance(target_raw, list):
        raise ConfigInvalid("series_file.target", "target must be an array")
    exogenous_raw = payload.get("exogenous", [])
    events_raw = payload.get("events", [])
    if not isinstance(exogenous_raw, list) or not isinstance(events_raw, list):
        raise ConfigInvalid("series_file", "exogenous/events must be arrays")
    try:
        exotic = {
            str(item["name"]): np.asarray(item["values"], dtype=np.float64)
            for item in exogenous_raw
        }
        events = {
            str(item["name"]): np.asarray(item["values"], dtype=np.float64)
            for item in events_raw
        }
        target = np.asarray(target_raw, dtype=np.float64)
        seed = int(payload.get("seed", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigInvalid("series_file", f"invalid series payload: {exc}") from exc
    series = SeriesData(
        target=target,
        exogenous=exotic,
        events=events,
        seed=seed,
    )
    declared_length = payload.get("length")
    if declared_length is not None and int(declared_length) != series.length:
        raise ConfigInvalid(
            "series_file.length",
            "declared length does not match target array",
        )
    return series
