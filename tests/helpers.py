from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from numeric_workbench.config import TrainingConfig
from numeric_workbench.data import (
    Dataset,
    DatasetSplit,
    generate_deterministic_samples,
    load_dataset,
    make_stratified_split,
)
from numeric_workbench.defaults import default_config_dict
from numeric_workbench.features import PreparedData, prepare_dataset
from numeric_workbench.precision import make_engine


def config_with(**overrides: Any) -> TrainingConfig:
    payload = default_config_dict("test-job")
    payload.update(overrides)
    return TrainingConfig.from_dict(payload)


def make_dataset(directory: str | Path, *, rows: int = 160) -> tuple[Path, Dataset, DatasetSplit]:
    path = Path(directory) / "samples.jsonl"
    generate_deterministic_samples(path, row_count=rows)
    config = config_with()
    dataset = load_dataset(path, config)
    split = make_stratified_split(dataset, config)
    return path, dataset, split


def prepare_for(
    dataset: Dataset,
    split: DatasetSplit,
    config: TrainingConfig,
    precision: str | None = None,
) -> PreparedData:
    engine = make_engine(
        precision or config.precision,
        fixed_scale=config.fixed_scale,
        rounding_mode=config.rounding_mode,
    )
    return prepare_dataset(dataset, split, config, engine)
