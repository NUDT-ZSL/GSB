"""YAML experiment configuration loading and isolated validation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import E_CONFIG_INVALID, Failure

SUPPORTED_SCHEMA_VERSION = 1
NORMALIZED_REGULARIZER_TYPES = {
    "l2": "l2",
    "ridge": "l2",
    "l1": "l1",
    "lasso": "l1",
    "elastic": "elastic_net",
    "elastic_net": "elastic_net",
    "elasticnet": "elastic_net",
}


@dataclass(frozen=True)
class DataConfig:
    spec_path: Path
    feature_columns: tuple[str, ...] = ()
    expected_column_types: dict[str, str] = field(default_factory=dict)
    target_column_override: str | None = None


@dataclass(frozen=True)
class OptimizerConfig:
    learning_rate: float = 0.05
    max_iterations: int = 80
    tolerance: float = 1.0e-7
    batch_size: int = 32
    fit_intercept: bool = True


@dataclass(frozen=True)
class SplitConfig:
    train_ratio: float = 0.8
    seed: int = 0


@dataclass(frozen=True)
class RuntimeConfig:
    concurrency: int = 1
    snapshot_commit_interval: int = 1
    numeric_tolerance: float = 1.0e-12
    state_dir: Path = Path(".lab_state")
    report_path: Path = Path("reports/latest.json")
    round_delay_seconds: float = 0.0


@dataclass(frozen=True)
class RegularizerSpec:
    name: str
    kind: str
    alpha: float
    l1_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.kind,
            "alpha": self.alpha,
            "l1_ratio": self.l1_ratio,
        }


@dataclass
class ExperimentConfig:
    schema_version: int
    source_path: Path
    data: DataConfig
    optimizer: OptimizerConfig
    split: SplitConfig
    runtime: RuntimeConfig
    regularizers: list[RegularizerSpec]
    failures: list[Failure] = field(default_factory=list)

    def with_failures(self, extra: list[Failure]) -> "ExperimentConfig":
        return ExperimentConfig(
            schema_version=self.schema_version,
            source_path=self.source_path,
            data=self.data,
            optimizer=self.optimizer,
            split=self.split,
            runtime=self.runtime,
            regularizers=self.regularizers,
            failures=[*self.failures, *extra],
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_finite_float(value: Any, default: float) -> tuple[float, bool]:
    if not _is_number(value) or not math.isfinite(float(value)):
        return default, False
    return float(value), True


def _as_int(value: Any, default: int) -> tuple[int, bool]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value, True
    return default, False


def _resolve_path(base: Path, value: Any, default: str) -> Path:
    raw = value if isinstance(value, str) and value.strip() else default
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _invalid(
    failures: list[Failure],
    message: str,
    *,
    scheme: str | None = None,
    column: str | None = None,
    phase: str = "config_validation",
    exception_type: str = "ConfigValidationError",
) -> None:
    failures.append(
        Failure(
            business_code=E_CONFIG_INVALID,
            phase=phase,
            stage=phase,
            message=message,
            exception_type=exception_type,
            scheme=scheme,
            column=column,
        )
    )


def _parse_data_section(raw: Any, base: Path, failures: list[Failure]) -> DataConfig:
    section = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        _invalid(failures, "data must be a mapping", column="data")
    spec = _resolve_path(base, section.get("spec_path"), "configs/data.yaml")

    feature_columns: tuple[str, ...] = ()
    raw_features = section.get("feature_columns", [])
    if raw_features is None:
        raw_features = []
    if not isinstance(raw_features, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_features
    ):
        _invalid(failures, "data.feature_columns must be a list of non-empty strings", column="data.feature_columns")
    else:
        feature_columns = tuple(item.strip() for item in raw_features)

    expected_types: dict[str, str] = {}
    raw_types = section.get("expected_column_types", {})
    if raw_types is None:
        raw_types = {}
    if not isinstance(raw_types, dict):
        _invalid(failures, "data.expected_column_types must be a mapping", column="data.expected_column_types")
    else:
        for raw_name, raw_type in raw_types.items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                _invalid(failures, "expected column name must be a non-empty string", column="data.expected_column_types")
                continue
            normalized = str(raw_type).strip().lower() if isinstance(raw_type, str) else ""
            if normalized not in {"numeric", "categorical", "redundant", "target"}:
                _invalid(
                    failures,
                    f"expected type for column {raw_name!r} is not a supported table column type",
                    column=raw_name,
                )
                continue
            expected_types[raw_name.strip()] = normalized

    target_override = section.get("target_column")
    if target_override is not None and (not isinstance(target_override, str) or not target_override.strip()):
        _invalid(failures, "data.target_column must be a non-empty string when provided", column="target")
        target_override = None

    return DataConfig(
        spec_path=spec,
        feature_columns=feature_columns,
        expected_column_types=expected_types,
        target_column_override=target_override.strip() if isinstance(target_override, str) else None,
    )


def _parse_optimizer(raw: Any, failures: list[Failure]) -> OptimizerConfig:
    section = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        _invalid(failures, "optimizer must be a mapping", column="optimizer")

    learning_rate, ok = _as_finite_float(section.get("learning_rate", 0.05), 0.05)
    if not ok or learning_rate <= 0.0:
        _invalid(failures, "optimizer.learning_rate must be a positive finite number", column="learning_rate")
        learning_rate = 0.05

    max_iterations, ok = _as_int(section.get("max_iterations", 80), 80)
    if not ok or max_iterations <= 0:
        _invalid(failures, "optimizer.max_iterations must be a positive integer", column="max_iterations")
        max_iterations = 80

    tolerance, ok = _as_finite_float(section.get("tolerance", 1.0e-7), 1.0e-7)
    if not ok or tolerance < 0.0:
        _invalid(failures, "optimizer.tolerance must be a non-negative finite number", column="tolerance")
        tolerance = 1.0e-7

    batch_size, ok = _as_int(section.get("batch_size", 32), 32)
    if not ok or batch_size <= 0:
        _invalid(failures, "optimizer.batch_size must be a positive integer", column="batch_size")
        batch_size = 32

    fit_intercept = section.get("fit_intercept", True)
    if not isinstance(fit_intercept, bool):
        _invalid(failures, "optimizer.fit_intercept must be a boolean", column="fit_intercept")
        fit_intercept = True

    return OptimizerConfig(
        learning_rate=learning_rate,
        max_iterations=max_iterations,
        tolerance=tolerance,
        batch_size=batch_size,
        fit_intercept=fit_intercept,
    )


def _parse_split(raw: Any, failures: list[Failure]) -> SplitConfig:
    section = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        _invalid(failures, "split must be a mapping", column="split")

    train_ratio, ok = _as_finite_float(section.get("train_ratio", 0.8), 0.8)
    if not ok or not 0.0 < train_ratio < 1.0:
        _invalid(failures, "split.train_ratio must be strictly between 0 and 1", column="train_ratio")
        train_ratio = 0.8

    seed, ok = _as_int(section.get("seed", 0), 0)
    if not ok or seed < 0:
        _invalid(failures, "split.seed must be a non-negative integer", column="seed")
        seed = 0
    return SplitConfig(train_ratio=train_ratio, seed=seed)


def _parse_runtime(raw: Any, base: Path, failures: list[Failure]) -> RuntimeConfig:
    section = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        _invalid(failures, "runtime must be a mapping", column="runtime")

    concurrency, ok = _as_int(section.get("concurrency", 1), 1)
    if not ok or concurrency <= 0:
        _invalid(failures, "runtime.concurrency must be a positive integer", column="concurrency")
        concurrency = 1

    interval, ok = _as_int(section.get("snapshot_commit_interval", 1), 1)
    if not ok or interval <= 0:
        _invalid(failures, "runtime.snapshot_commit_interval must be a positive integer", column="snapshot_commit_interval")
        interval = 1

    numeric_tolerance, ok = _as_finite_float(
        section.get("numeric_tolerance", 1.0e-12), 1.0e-12
    )
    if not ok or numeric_tolerance < 0.0:
        _invalid(failures, "runtime.numeric_tolerance must be a non-negative finite number", column="numeric_tolerance")
        numeric_tolerance = 1.0e-12

    round_delay, ok = _as_finite_float(section.get("round_delay_seconds", 0.0), 0.0)
    if not ok or round_delay < 0.0:
        _invalid(failures, "runtime.round_delay_seconds must be a non-negative finite number", column="round_delay_seconds")
        round_delay = 0.0

    return RuntimeConfig(
        concurrency=concurrency,
        snapshot_commit_interval=interval,
        numeric_tolerance=numeric_tolerance,
        state_dir=_resolve_path(base, section.get("state_dir"), ".lab_state"),
        report_path=_resolve_path(base, section.get("report_path"), "reports/latest.json"),
        round_delay_seconds=round_delay,
    )


def _parse_regularizers(raw: Any, failures: list[Failure]) -> list[RegularizerSpec]:
    if raw is None:
        _invalid(failures, "regularizers must contain at least one scheme", column="regularizers")
        return []
    if not isinstance(raw, list):
        _invalid(failures, "regularizers must be a list", column="regularizers")
        return []

    parsed: list[RegularizerSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        label = f"regularizers[{index}]"
        if not isinstance(item, dict):
            _invalid(failures, f"{label} must be a mapping", column=label)
            continue
        if item.get("enabled", True) is False:
            continue
        name_raw = item.get("name")
        if not isinstance(name_raw, str) or not name_raw.strip():
            _invalid(failures, f"{label}.name must be a non-empty string", column=f"{label}.name")
            continue
        name = name_raw.strip()
        if name in seen:
            _invalid(failures, f"duplicate regularization scheme name {name!r}", scheme=name, column=f"{label}.name")
            continue

        type_raw = item.get("type")
        if not isinstance(type_raw, str):
            _invalid(failures, f"{label}.type must be a string", scheme=name, column=f"{label}.type")
            continue
        kind = NORMALIZED_REGULARIZER_TYPES.get(type_raw.strip().lower())
        if kind is None:
            _invalid(
                failures,
                f"unsupported regularization type {type_raw!r}; expected l2, l1 or elastic_net",
                scheme=name,
                column=f"{label}.type",
            )
            continue

        alpha, ok = _as_finite_float(item.get("alpha"), math.nan)
        if not ok:
            _invalid(failures, f"{label}.alpha must be a finite number", scheme=name, column=f"{label}.alpha")
            continue
        if alpha < 0.0:
            _invalid(failures, f"{label}.alpha must not be negative", scheme=name, column=f"{label}.alpha")
            continue

        ratio_default = 1.0 if kind == "l1" else (0.0 if kind == "l2" else 0.5)
        l1_ratio, ok = _as_finite_float(item.get("l1_ratio", ratio_default), ratio_default)
        if not ok or not 0.0 <= l1_ratio <= 1.0:
            _invalid(
                failures,
                f"{label}.l1_ratio must be a finite number in [0, 1]",
                scheme=name,
                column=f"{label}.l1_ratio",
            )
            continue

        seen.add(name)
        parsed.append(RegularizerSpec(name=name, kind=kind, alpha=alpha, l1_ratio=l1_ratio))
    return parsed


def load_experiment(path: str | Path) -> ExperimentConfig:
    """Load YAML and collect validation failures without raising for field errors."""

    source = Path(path).expanduser().resolve()
    failures: list[Failure] = []
    try:
        text = source.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except Exception as exc:  # YAML/file failure is the only unrecoverable load boundary.
        _invalid(
            failures,
            f"unable to read YAML configuration: {exc}",
            phase="config_load",
            exception_type=type(exc).__name__,
        )
        raw = {}

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _invalid(failures, "top-level YAML value must be a mapping", phase="config_load")
        raw = {}

    schema_version, ok = _as_int(raw.get("schema_version", SUPPORTED_SCHEMA_VERSION), SUPPORTED_SCHEMA_VERSION)
    if not ok or schema_version != SUPPORTED_SCHEMA_VERSION:
        _invalid(
            failures,
            f"unsupported schema_version; expected {SUPPORTED_SCHEMA_VERSION}",
            column="schema_version",
        )
        schema_version = SUPPORTED_SCHEMA_VERSION

    base = source.parent
    data = _parse_data_section(raw.get("data"), base, failures)
    optimizer = _parse_optimizer(raw.get("optimizer"), failures)
    split = _parse_split(raw.get("split"), failures)
    runtime = _parse_runtime(raw.get("runtime"), base, failures)
    regularizers = _parse_regularizers(raw.get("regularizers"), failures)

    return ExperimentConfig(
        schema_version=schema_version,
        source_path=source,
        data=data,
        optimizer=optimizer,
        split=split,
        runtime=runtime,
        regularizers=regularizers,
        failures=failures,
    )
