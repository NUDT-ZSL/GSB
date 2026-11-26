"""Bidirectional migration between v1 and the unified v2 representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .canonical import (
    ensure_timestamp_not_rolling_back,
    format_timestamp_ms,
    parse_timestamp_ms,
    require_field,
    require_int,
    require_mapping,
    require_string,
    require_threshold,
)
from .errors import E_SEMANTIC_LOSS, OK, WorkbenchError, corrupt
from .models import (
    PRECISIONS,
    THRESHOLD_KEYS,
    DiscardRecord,
    ProvenanceRecord,
    ThresholdConfig,
    ThresholdEntry,
)


@dataclass(frozen=True)
class MigrationResult:
    source_version: int
    target_version: int
    config: ThresholdConfig
    document: dict[str, Any] | None
    fill_records: tuple[ProvenanceRecord, ...] = field(default_factory=tuple)
    discard_records: tuple[DiscardRecord, ...] = field(default_factory=tuple)
    business_code: str = OK
    semantic_equivalent: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "business_code": self.business_code,
            "semantic_equivalent": self.semantic_equivalent,
            "fill_records": [record.to_dict() for record in self.fill_records],
            "discard_records": [record.to_dict() for record in self.discard_records],
            "document": self.document,
            "config": self.config.to_dict(),
        }


def _v1_body(document: dict[str, Any]) -> dict[str, Any]:
    if "config" in document:
        body = require_mapping(document["config"], "v1.config")
        conflicting = {
            key
            for key in (
                "thresholds",
                "route_budget",
                "candidate_plan_count",
                "scheduler_workers",
                "precision",
                "updated_at",
            )
            if key in document
        }
        if conflicting:
            raise corrupt(
                "v1 metadata appears both at top level and inside config",
                conflicting_fields=sorted(conflicting),
            )
        return body
    return document


def upgrade_v1_document(
    document: dict[str, Any],
    *,
    previous_updated_at_ms: int | None = None,
) -> tuple[ThresholdConfig, tuple[ProvenanceRecord, ...]]:
    """Parse a v1 document and fill every v2-only field with recorded provenance."""

    body = _v1_body(document)
    forbidden = sorted(
        set(body).intersection(
            {
                "generation",
                "revision",
                "created_at_ms",
                "updated_at_ms",
                "tombstones",
                "provenance",
            }
        )
    )
    if forbidden:
        raise corrupt(
            "v1 document contains v2-only metadata and cannot be safely defaulted",
            fields=forbidden,
        )

    thresholds = require_mapping(
        require_field(body, "thresholds", "v1.config"),
        "v1.config.thresholds",
    )
    extra_keys = sorted(set(thresholds).difference(THRESHOLD_KEYS))
    missing_keys = sorted(set(THRESHOLD_KEYS).difference(thresholds))
    if extra_keys or missing_keys:
        raise corrupt(
            "v1 threshold field set is not exact",
            missing=missing_keys,
            extra=extra_keys,
        )

    updated_at_ms = parse_timestamp_ms(
        require_field(body, "updated_at", "v1.config"),
        "v1.config.updated_at",
    )
    ensure_timestamp_not_rolling_back(
        updated_at_ms,
        previous_updated_at_ms,
        "v1.config.updated_at",
    )
    route_budget = require_int(
        require_field(body, "route_budget", "v1.config"),
        "v1.config.route_budget",
        minimum=1,
    )
    candidate_plan_count = require_int(
        require_field(body, "candidate_plan_count", "v1.config"),
        "v1.config.candidate_plan_count",
        minimum=1,
    )
    scheduler_workers = require_int(
        require_field(body, "scheduler_workers", "v1.config"),
        "v1.config.scheduler_workers",
        minimum=1,
    )
    precision = require_string(
        require_field(body, "precision", "v1.config"),
        "v1.config.precision",
    )
    if precision not in PRECISIONS:
        raise corrupt("unsupported precision", path="v1.config.precision", value=precision)
    if candidate_plan_count > 8:
        raise corrupt(
            "candidate plan count exceeds supported maximum",
            path="v1.config.candidate_plan_count",
            value=candidate_plan_count,
        )
    if scheduler_workers > 64:
        raise corrupt(
            "scheduler workers exceed supported maximum",
            path="v1.config.scheduler_workers",
            value=scheduler_workers,
        )

    entries = tuple(
        ThresholdEntry(
            key=key,
            threshold=require_threshold(thresholds[key], f"v1.config.thresholds.{key}"),
            revision=0,
            updated_at_ms=updated_at_ms,
        )
        for key in THRESHOLD_KEYS
    )
    config = ThresholdConfig(
        schema_version=2,
        generation=1,
        revision=0,
        created_at_ms=updated_at_ms,
        updated_at_ms=updated_at_ms,
        entries=entries,
        tombstones=(),
        route_budget=route_budget,
        candidate_plan_count=candidate_plan_count,
        scheduler_workers=scheduler_workers,
        precision=precision,
    )
    records = [
        ProvenanceRecord(
            field="updated_at_ms",
            source="v1.config.updated_at",
            rule="parse_rfc3339_utc_to_epoch_ms",
            value=updated_at_ms,
        ),
        ProvenanceRecord(
            field="created_at_ms",
            source="v1.config.updated_at",
            rule="single_v1_timestamp_copied",
            value=updated_at_ms,
        ),
        ProvenanceRecord(
            field="generation",
            source="v1.upgrade.policy",
            rule="single_file_snapshot_is_generation_1",
            value=1,
        ),
        ProvenanceRecord(
            field="revision",
            source="v1.upgrade.policy",
            rule="v1_has_no_incremental_revision",
            value=0,
        ),
        ProvenanceRecord(
            field="tombstones",
            source="v1.upgrade.policy",
            rule="v1_has_no_tombstone_channel",
            value=[],
        ),
    ]
    for key in THRESHOLD_KEYS:
        records.extend(
            [
                ProvenanceRecord(
                    field=f"entries.{key}.revision",
                    source=f"v1.config.thresholds.{key}",
                    rule="v1_entry_revision_defaults_to_zero",
                    value=0,
                ),
                ProvenanceRecord(
                    field=f"entries.{key}.updated_at_ms",
                    source="v1.config.updated_at",
                    rule="entry_timestamp_copied_from_snapshot",
                    value=updated_at_ms,
                ),
            ]
        )
    return config.with_provenance(records), tuple(records)


def migrate_v1_to_v2(
    document: dict[str, Any],
    *,
    previous_updated_at_ms: int | None = None,
) -> MigrationResult:
    config, records = upgrade_v1_document(
        document,
        previous_updated_at_ms=previous_updated_at_ms,
    )
    return MigrationResult(
        source_version=1,
        target_version=2,
        config=config,
        document=None,
        fill_records=records,
        business_code=OK,
        semantic_equivalent=True,
    )


def downgrade_v2_to_v1(
    config: ThresholdConfig,
    *,
    allow_explicit_revision_discard: bool = True,
    strict_lossless: bool = False,
) -> MigrationResult:
    """Project v2 to v1 under explicit discard rules.

    Incremental revisions are intentionally droppable metadata. Tombstones have
    routing semantics and cannot be projected, so their presence is semantic loss.
    """

    if config.tombstones:
        raise WorkbenchError(
            E_SEMANTIC_LOSS,
            "v1 cannot express tombstone invalidation semantics",
            {
                "tombstones": [item.to_dict() for item in config.tombstones],
                "rule": "v2_to_v1.tombstone_not_expressible",
            },
        )
    if not allow_explicit_revision_discard:
        raise WorkbenchError(
            E_SEMANTIC_LOSS,
            "v1 cannot express incremental revisions",
            {"rule": "v2_to_v1.entry_revision_discard_disabled"},
        )

    discard_records: list[DiscardRecord] = [
        DiscardRecord(
            field="generation",
            source_rule="v2_to_v1.generation_not_expressible",
            reason="v1 has no generation field; re-upgrade derives generation 1",
            previous_value=config.generation,
        ),
        DiscardRecord(
            field="revision",
            source_rule="v2_to_v1.config_revision_not_expressible",
            reason="v1 has no snapshot-level incremental revision",
            previous_value=config.revision,
        ),
        DiscardRecord(
            field="created_at_ms",
            source_rule="v2_to_v1.created_timestamp_collapsed",
            reason="v1 carries one updated_at timestamp only",
            previous_value=config.created_at_ms,
        ),
    ]
    for entry in config.entries:
        discard_records.append(
            DiscardRecord(
                field=f"entries.{entry.key}.revision",
                source_rule="v2_to_v1.entry_revision_discard",
                reason="explicit rule: v1 threshold entries have no revision number",
                previous_value=entry.revision,
            )
        )
        if entry.updated_at_ms != config.updated_at_ms:
            discard_records.append(
                DiscardRecord(
                    field=f"entries.{entry.key}.updated_at_ms",
                    source_rule="v2_to_v1.entry_timestamp_collapsed",
                    reason="v1 threshold entries share the snapshot timestamp",
                    previous_value=entry.updated_at_ms,
                )
            )

    thresholds = config.threshold_map()
    document: dict[str, Any] = {
        "schema": "mm-threshold-config",
        "format_version": 1,
        "config": {
            "thresholds": {key: thresholds[key] for key in THRESHOLD_KEYS},
            "route_budget": config.route_budget,
            "candidate_plan_count": config.candidate_plan_count,
            "scheduler_workers": config.scheduler_workers,
            "precision": config.precision,
            "updated_at": format_timestamp_ms(config.updated_at_ms),
        },
    }

    projected_entries = tuple(
        ThresholdEntry(
            key=entry.key,
            threshold=entry.threshold,
            revision=0,
            updated_at_ms=config.updated_at_ms,
        )
        for entry in config.entries
    )
    projected = ThresholdConfig(
        schema_version=2,
        generation=1,
        revision=0,
        created_at_ms=config.updated_at_ms,
        updated_at_ms=config.updated_at_ms,
        entries=projected_entries,
        tombstones=(),
        route_budget=config.route_budget,
        candidate_plan_count=config.candidate_plan_count,
        scheduler_workers=config.scheduler_workers,
        precision=config.precision,
    )

    common_fields_equal = all(
        [
            projected.threshold_map() == config.threshold_map(),
            projected.route_budget == config.route_budget,
            projected.candidate_plan_count == config.candidate_plan_count,
            projected.scheduler_workers == config.scheduler_workers,
            projected.precision == config.precision,
            projected.updated_at_ms == config.updated_at_ms,
        ]
    )
    if not common_fields_equal:
        raise WorkbenchError(
            E_SEMANTIC_LOSS,
            "v2 projection changed a field that v1 can express",
            {"rule": "v2_to_v1.common_field_mismatch"},
        )
    discarded_fields = {record.field for record in discard_records}
    semantics_discarded = (
        config.generation != 1
        or config.revision != 0
        or config.created_at_ms != config.updated_at_ms
        or any(entry.revision != 0 for entry in config.entries)
        or any(entry.updated_at_ms != config.updated_at_ms for entry in config.entries)
    )
    if strict_lossless and semantics_discarded:
        raise WorkbenchError(
            E_SEMANTIC_LOSS,
            "strict lossless downgrade rejected explicit revision discard",
            {"discarded_fields": sorted(discarded_fields)},
        )
    return MigrationResult(
        source_version=2,
        target_version=1,
        config=projected,
        document=document,
        discard_records=tuple(discard_records),
        business_code=OK,
        semantic_equivalent=not semantics_discarded,
    )


def migrate_config(config: ThresholdConfig, target_version: int) -> MigrationResult:
    if target_version == 2:
        if config.schema_version == 2:
            return MigrationResult(
                source_version=2,
                target_version=2,
                config=config,
                document=None,
                business_code=OK,
                semantic_equivalent=True,
            )
        raise corrupt("upgrade requires a v1 source document, not a projected config")
    if target_version == 1:
        return downgrade_v2_to_v1(config)
    raise corrupt("unsupported migration target", target_version=target_version)

