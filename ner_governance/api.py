"""Integrable domain facade."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .annotations import load_annotations
from .config import GovernanceConfig, load_config
from .merge import MergeBundle, merge_annotations
from .migration import migrate_type_set
from .models import AnnotationRecord, MigrationResult, TypeSet
from .selfcheck import run_self_check
from .semantic import compare_format_views
from .snapshots import Snapshot, SnapshotStore
from .trace import trace_label, verify_all_traces
from .typesets import load_type_set, read_type_set_text


class NerGovernance:
    """Stable entry points for loading, migration, merging and replay."""

    @staticmethod
    def load_type_set(path: str | Path) -> TypeSet:
        return load_type_set(path)

    @staticmethod
    def parse_type_set(text: str, *, source_name: str | None = None) -> TypeSet:
        return read_type_set_text(text, source_name=source_name)

    @staticmethod
    def load_type_set_views(
        paths: dict[str, str | Path],
    ) -> dict[str, TypeSet]:
        return {
            name: load_type_set(path)
            for name, path in sorted(paths.items())
        }

    @staticmethod
    def load_config(path: str | Path) -> GovernanceConfig:
        return load_config(path)

    @staticmethod
    def load_annotations(path: str | Path) -> list[AnnotationRecord]:
        return load_annotations(path)

    @staticmethod
    def migrate(
        type_set: TypeSet,
        target_format: str,
        config: GovernanceConfig,
    ) -> MigrationResult:
        return migrate_type_set(type_set, target_format, config)

    @staticmethod
    def migrate_matrix(
        type_set: TypeSet,
        config: GovernanceConfig,
    ) -> dict[str, MigrationResult]:
        targets = ["v1", "v2", "v3"]
        return {
            target: migrate_type_set(type_set, target, config)
            for target in targets
        }

    @staticmethod
    def merge(
        records: list[AnnotationRecord],
        type_set: TypeSet,
        config: GovernanceConfig,
    ) -> MergeBundle:
        return merge_annotations(records, type_set, config)

    @staticmethod
    def compare_views(
        views: dict[str, TypeSet],
        records: list[AnnotationRecord],
        config: GovernanceConfig,
    ) -> dict[str, Any]:
        return compare_format_views(views, records, config)

    @staticmethod
    def trace(
        bundle: MergeBundle,
        records: list[AnnotationRecord],
        type_set: TypeSet,
        config: GovernanceConfig,
        doc_id: str,
        position: int,
    ) -> dict[str, Any]:
        return trace_label(
            bundle,
            records,
            type_set,
            config,
            doc_id,
            position,
        )

    @staticmethod
    def verify_all_traces(
        bundle: MergeBundle,
        records: list[AnnotationRecord],
        type_set: TypeSet,
        config: GovernanceConfig,
    ) -> dict[str, Any]:
        return verify_all_traces(bundle, records, type_set, config)

    @staticmethod
    def self_check(
        records: list[AnnotationRecord],
        type_sets: dict[str, TypeSet],
        config: GovernanceConfig,
    ) -> dict[str, Any]:
        return run_self_check(records, type_sets, config)

    @staticmethod
    def write_type_set_snapshot(
        store: SnapshotStore,
        type_set: TypeSet,
        *,
        base_version: int | None = None,
        expected_checksum: str | None = None,
    ) -> Snapshot:
        return store.write(
            "type_set",
            type_set.to_payload(),
            base_version=base_version,
            expected_checksum=expected_checksum,
        )

    @staticmethod
    def write_merge_snapshot(
        store: SnapshotStore,
        bundle: MergeBundle,
        *,
        base_version: int | None = None,
        expected_checksum: str | None = None,
    ) -> Snapshot:
        return store.write(
            "merge_result",
            bundle.to_dict(),
            base_version=base_version,
            expected_checksum=expected_checksum,
        )
