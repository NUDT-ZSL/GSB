"""Replayable bidirectional feature-specification migration."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from .errors import E_FORMAT_INVALID, E_VERSION_UNSUPPORTED, fail
from .hashing import sha256_text
from .models import (
    CategoricalFeature,
    CrossFeature,
    FeatureSpec,
    NumericFeature,
    StatSpec,
)
from .specs import (
    LoadedSpec,
    normalize_spec,
    serialize_spec,
)


@dataclass(frozen=True)
class MigrationRecord:
    op: str
    path: str
    value: Any
    source: str
    target_expressible: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "path": self.path,
            "value": self.value,
            "source": self.source,
            "target_expressible": self.target_expressible,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MigrationResult:
    source_version: int
    target_version: int
    source_checksum: str
    target_spec: FeatureSpec
    target_text: str
    target_checksum: str
    records: tuple[MigrationRecord, ...]
    replay_ok: bool

    @property
    def fills(self) -> tuple[MigrationRecord, ...]:
        return tuple(record for record in self.records if record.op == "fill")

    @property
    def drops(self) -> tuple[MigrationRecord, ...]:
        return tuple(record for record in self.records if record.op == "drop")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "source_checksum": self.source_checksum,
            "target_checksum": self.target_checksum,
            "replay_ok": self.replay_ok,
            "fill_count": len(self.fills),
            "drop_count": len(self.drops),
            "records": [record.to_dict() for record in self.records],
        }


def migrate_spec(source: LoadedSpec | FeatureSpec, target_version: int) -> MigrationResult:
    if target_version not in {1, 2, 3}:
        fail(
            E_VERSION_UNSUPPORTED,
            f"target feature specification version {target_version!r} is unsupported",
            field="target_version",
            version=target_version,
        )
    source_spec = source.spec if isinstance(source, LoadedSpec) else source
    source_checksum = (
        source.checksum if isinstance(source, LoadedSpec) else source_spec.checksum
    )
    target, records = _build_target(source_spec, target_version)
    target_text = serialize_spec(target, target_version)
    target_checksum = sha256_text(target_text)
    replayed = replay_migration(source_spec, records, target_version)
    replay_ok = replayed.to_dict() == target.to_dict() and (
        replayed.checksum == target.checksum
    )
    if not replay_ok:
        fail(
            E_FORMAT_INVALID,
            "migration records do not replay to the target internal representation",
            field="migration_records",
        )
    return MigrationResult(
        source_version=source_spec.source_version,
        target_version=target_version,
        source_checksum=source_checksum,
        target_spec=target,
        target_text=target_text,
        target_checksum=target_checksum,
        records=records,
        replay_ok=replay_ok,
    )


def _build_target(
    source: FeatureSpec, target_version: int
) -> tuple[FeatureSpec, tuple[MigrationRecord, ...]]:
    normalized_source = normalize_spec(source)
    records: list[MigrationRecord] = []

    numeric: list[NumericFeature] = []
    for item in source.numeric:
        normalized = normalized_source.numeric_map()[item.name]
        if target_version >= 2:
            strategy = item.missing_strategy
            if strategy is None:
                strategy = normalized.missing_strategy
                records.append(
                    _fill(
                        f"numeric.{item.name}.missing_strategy",
                        strategy,
                        source="compatibility:v1_implicit_numeric_missing_zero",
                        reason="v2/v3 require an explicit per-feature missing strategy",
                        target_expressible=True,
                    )
                )
            elif target_version == 2 and strategy == "default":
                records.append(
                    _drop(
                        f"numeric.{item.name}.missing_strategy",
                        strategy,
                        reason=(
                            "v2 has no per-feature default strategy; flatten to zero"
                        ),
                        fallback="zero",
                    )
                )
                strategy = "zero"
        else:
            strategy = None
            if item.missing_strategy is not None:
                records.append(
                    _drop(
                        f"numeric.{item.name}.missing_strategy",
                        item.missing_strategy,
                        reason="v1 has no per-feature missing strategy field",
                        fallback=normalized.missing_strategy,
                    )
                )
        default = item.default
        if default is None:
            if target_version == 3:
                default = normalized.default
            else:
                effective_strategy = strategy if target_version >= 2 else "zero"
                default = item.mean if effective_strategy == "mean" else 0.0
            records.append(
                _fill(
                    f"numeric.{item.name}.default",
                    default,
                    source=f"compatibility:{_source_default_name(source.source_version)}",
                    reason=(
                        "v3 requires an explicit feature default value"
                        if target_version == 3
                        else "kept in the unified internal representation; target serialization omits it"
                    ),
                    target_expressible=target_version == 3,
                )
            )
        elif target_version < 3:
            if item.default is not None:
                records.append(
                    _drop(
                        f"numeric.{item.name}.default",
                        item.default,
                        reason=f"v{target_version} cannot express a per-feature default value",
                        fallback=normalized.default,
                    )
                )
            default = None
        numeric.append(
            NumericFeature(
                item.name,
                item.mean,
                item.std,
                item.weight,
                strategy,
                default,
            )
        )

    categorical: list[CategoricalFeature] = []
    for item in source.categorical:
        normalized = normalized_source.categorical_map()[item.name]
        if target_version >= 2:
            strategy = item.missing_strategy
            if strategy is None:
                strategy = normalized.missing_strategy
                records.append(
                    _fill(
                        f"categorical.{item.name}.missing_strategy",
                        strategy,
                        source="compatibility:v1_implicit_categorical_default",
                        reason="v2/v3 require an explicit per-feature missing strategy",
                        target_expressible=True,
                    )
                )
        else:
            strategy = None
            if item.missing_strategy is not None:
                records.append(
                    _drop(
                        f"categorical.{item.name}.missing_strategy",
                        item.missing_strategy,
                        reason="v1 has no per-feature missing strategy field",
                        fallback=normalized.missing_strategy,
                    )
                )
        default = item.default
        if default is None:
            default = (
                normalized.default if target_version == 3 else "__MISSING__"
            )
            records.append(
                _fill(
                    f"categorical.{item.name}.default",
                    default,
                    source="compatibility:v1_v2_missing_category",
                    reason=(
                        "v3 requires an explicit feature default value"
                        if target_version == 3
                        else "kept in the unified internal representation; target serialization omits it"
                    ),
                    target_expressible=target_version == 3,
                )
            )
        elif target_version < 3:
            if item.default is not None:
                records.append(
                    _drop(
                        f"categorical.{item.name}.default",
                        item.default,
                        reason=f"v{target_version} cannot express a per-feature default value",
                        fallback=normalized.default,
                    )
                )
            default = None
        categorical.append(
            CategoricalFeature(
                item.name,
                item.weight,
                item.buckets,
                strategy,
                default,
            )
        )

    cross: list[CrossFeature] = []
    for item in source.cross:
        normalized = normalized_source.cross_map()[item.name]
        if target_version >= 2:
            strategy = item.missing_strategy
            if strategy is None:
                strategy = normalized.missing_strategy
                records.append(
                    _fill(
                        f"cross.{item.name}.missing_strategy",
                        strategy,
                        source="compatibility:v1_implicit_cross_zero",
                        reason="v2/v3 require an explicit missing strategy",
                        target_expressible=True,
                    )
                )
        else:
            strategy = None
            if item.missing_strategy is not None:
                records.append(
                    _drop(
                        f"cross.{item.name}.missing_strategy",
                        item.missing_strategy,
                        reason="v1 has no per-feature missing strategy field",
                        fallback=normalized.missing_strategy,
                    )
                )
        slots = item.slots or normalized.slots
        cross.append(
            CrossFeature(
                item.name,
                item.left,
                item.right,
                item.weight,
                tuple(slots),
                strategy,
            )
        )

    statistics: list[StatSpec] = []
    for item in source.statistics:
        normalized = normalized_source.stat_map()[item.metric]
        sem_name = item.semantics_name
        sem_version = item.semantics_version
        target_sem_name, target_sem_version = {
            1: ("legacy_prior_v1", 1),
            2: ("snapshot_bayes_v2", 2),
            3: ("nested_snapshot_bayes_v3", 3),
        }[target_version]
        if sem_name is None:
            sem_name = target_sem_name
            records.append(
                _fill(
                    f"statistics.{item.metric}.semantics_name",
                    sem_name,
                    source=f"compatibility:{_source_default_name(source.source_version)}",
                    reason=(
                        "v3 requires versioned statistics semantics"
                        if target_version == 3
                        else "kept in the unified internal representation; target serialization omits it"
                    ),
                    target_expressible=target_version == 3,
                )
            )
        elif target_version < 3:
            if item.semantics_name is not None:
                records.append(
                    _drop(
                        f"statistics.{item.metric}.semantics_name",
                        item.semantics_name,
                        reason=f"v{target_version} cannot express versioned statistics semantics",
                        fallback=normalized.semantics_name,
                    )
                )
            sem_name = None
        if sem_version is None:
            sem_version = target_sem_version
            records.append(
                _fill(
                    f"statistics.{item.metric}.semantics_version",
                    sem_version,
                    source="compatibility:target_statistics_semantics_version",
                    reason=(
                        "v3 requires versioned statistics semantics"
                        if target_version == 3
                        else "kept in the unified internal representation; target serialization omits it"
                    ),
                    target_expressible=target_version == 3,
                )
            )
        elif target_version < 3:
            if item.semantics_version is not None:
                records.append(
                    _drop(
                        f"statistics.{item.metric}.semantics_version",
                        item.semantics_version,
                        reason=f"v{target_version} cannot express versioned statistics semantics",
                        fallback=normalized.semantics_version,
                    )
                )
            sem_version = None
        statistics.append(
            StatSpec(
                item.metric,
                item.prior,
                item.smoothing,
                sem_name,
                sem_version,
            )
        )

    target = FeatureSpec(
        source_version=target_version,
        numeric=tuple(numeric),
        categorical=tuple(categorical),
        cross=tuple(cross),
        statistics=tuple(statistics),
        metadata=_target_metadata(source, target_version),
    )
    return normalize_spec(target), tuple(records)


def _fill(
    path: str,
    value: Any,
    *,
    source: str,
    reason: str,
    target_expressible: bool,
) -> MigrationRecord:
    return MigrationRecord(
        op="fill",
        path=path,
        value=value,
        source=source,
        target_expressible=target_expressible,
        reason=reason,
    )


def _drop(
    path: str,
    value: Any,
    *,
    reason: str,
    fallback: Any,
) -> MigrationRecord:
    return MigrationRecord(
        op="drop",
        path=path,
        value={"dropped": value, "fallback": fallback},
        source="downgrade_rule:explicit",
        target_expressible=False,
        reason=reason,
    )


def _source_default_name(version: int) -> str:
    return {
        1: "v1_implicit",
        2: "v2_implicit",
        3: "v3_explicit",
    }.get(version, f"v{version}_implicit")


def _target_metadata(source: FeatureSpec, target_version: int) -> dict[str, Any]:
    format_name = {
        1: "v1-pipe",
        2: "v2-keyed-json",
        3: "v3-nested-json",
    }[target_version]
    return {
        "format": format_name,
        "migrated_from": source.source_version,
    }


def replay_migration(
    source: LoadedSpec | FeatureSpec,
    records: tuple[MigrationRecord, ...] | list[MigrationRecord],
    target_version: int,
) -> FeatureSpec:
    """Apply migration records to the explicit source representation."""

    if target_version not in {1, 2, 3}:
        fail(
            E_VERSION_UNSUPPORTED,
            f"target feature specification version {target_version!r} is unsupported",
            field="target_version",
            version=target_version,
        )
    source_spec = source.spec if isinstance(source, LoadedSpec) else source
    current = source_spec
    for record in records:
        current = _apply_record(current, record)
    current = dataclasses.replace(
        current,
        source_version=target_version,
        metadata=_target_metadata(source_spec, target_version),
    )
    return normalize_spec(current)


def _apply_record(spec: FeatureSpec, record: MigrationRecord) -> FeatureSpec:
    parts = record.path.split(".")
    if len(parts) < 3:
        fail(
            E_FORMAT_INVALID,
            f"invalid migration record path {record.path!r}",
            field="migration.path",
        )
    kind, name, field = parts[0], parts[1], parts[2]
    value = record.value
    if record.op == "drop":
        value = None
    elif record.op != "fill":
        fail(
            E_FORMAT_INVALID,
            f"invalid migration operation {record.op!r}",
            field="migration.op",
        )

    if kind == "numeric":
        numeric = []
        found = False
        for item in spec.numeric:
            if item.name == name:
                found = True
                if field not in {"missing_strategy", "default"}:
                    fail(
                        E_FORMAT_INVALID,
                        f"invalid numeric migration field {field!r}",
                        field="migration.path",
                    )
                item = dataclasses.replace(item, **{field: value})
            numeric.append(item)
        if not found:
            _unknown_migration_name(kind, name)
        return dataclasses.replace(spec, numeric=tuple(numeric))

    if kind == "categorical":
        categorical = []
        found = False
        for item in spec.categorical:
            if item.name == name:
                found = True
                if field not in {"missing_strategy", "default"}:
                    fail(
                        E_FORMAT_INVALID,
                        f"invalid categorical migration field {field!r}",
                        field="migration.path",
                    )
                item = dataclasses.replace(item, **{field: value})
            categorical.append(item)
        if not found:
            _unknown_migration_name(kind, name)
        return dataclasses.replace(spec, categorical=tuple(categorical))

    if kind == "cross":
        cross = []
        found = False
        for item in spec.cross:
            if item.name == name:
                found = True
                if field != "missing_strategy":
                    fail(
                        E_FORMAT_INVALID,
                        f"invalid cross migration field {field!r}",
                        field="migration.path",
                    )
                item = dataclasses.replace(item, **{field: value})
            cross.append(item)
        if not found:
            _unknown_migration_name(kind, name)
        return dataclasses.replace(spec, cross=tuple(cross))

    if kind == "statistics":
        statistics = []
        found = False
        for item in spec.statistics:
            if item.metric == name:
                found = True
                if field not in {"semantics_name", "semantics_version"}:
                    fail(
                        E_FORMAT_INVALID,
                        f"invalid statistics migration field {field!r}",
                        field="migration.path",
                    )
                item = dataclasses.replace(item, **{field: value})
            statistics.append(item)
        if not found:
            _unknown_migration_name(kind, name)
        return dataclasses.replace(spec, statistics=tuple(statistics))

    fail(
        E_FORMAT_INVALID,
        f"invalid migration record kind {kind!r}",
        field="migration.path",
    )


def _unknown_migration_name(kind: str, name: str) -> None:
    fail(
        E_FORMAT_INVALID,
        f"migration record refers to unknown {kind} object {name!r}",
        field=f"migration.{kind}.{name}",
    )


def replay_to_text(
    source: LoadedSpec | FeatureSpec,
    records: tuple[MigrationRecord, ...] | list[MigrationRecord],
    target_version: int,
) -> tuple[str, str]:
    spec = replay_migration(source, records, target_version)
    text = serialize_spec(spec, target_version)
    return text, sha256_text(text)


def validate_migration_roundtrip(
    source: LoadedSpec | FeatureSpec, target_version: int
) -> MigrationResult:
    return migrate_spec(source, target_version)


__all__ = [
    "MigrationRecord",
    "MigrationResult",
    "migrate_spec",
    "replay_migration",
    "replay_to_text",
    "validate_migration_roundtrip",
]

