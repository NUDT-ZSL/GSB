"""Pipeline orchestration, incremental invalidation, and artifact output."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .cache import CacheLookup, ContentAddressedCache, matrix_to_bytes
from .canonical import atomic_write_bytes, atomic_write_json, sha256_bytes, sha256_obj
from .config import FeatureConfig
from .data import sample_content_digest
from .errors import (
    E_CACHE_CORRUPT,
    E_CONFIG_INVALID,
    E_UNCLASSIFIED,
    BusinessError,
    Degradation,
    ErrorRecord,
)
from .injection import InjectedUnclassified, InjectionConfig, parse_injection_config
from .snapshots import SnapshotStore, SnapshotWrite
from .stages import (
    Diagnostics,
    FeatureBlock,
    SelectionOutcome,
    StageBlocks,
    encode_categories,
    expand_cross_slots,
    matrix_version,
    select_features,
    standardize_numeric,
    _rank_scores,
    _variance_scores,
)


@dataclass
class PipelineResult:
    config: FeatureConfig
    sample_digest: str
    matrix: np.ndarray
    columns: list[str]
    category_blocks: list[FeatureBlock]
    numeric_blocks: list[FeatureBlock]
    cross_blocks: list[FeatureBlock]
    selection: SelectionOutcome | None
    diagnostics: Diagnostics
    snapshot_writes: list[SnapshotWrite] = field(default_factory=list)

    @property
    def errors(self) -> list[ErrorRecord]:
        return self.diagnostics.errors

    @property
    def degradations(self) -> list[Degradation]:
        return self.diagnostics.degradations

    @property
    def error_codes(self) -> list[str]:
        return [error.code for error in self.errors]

    @property
    def selected_features(self) -> list[str]:
        return [] if self.selection is None else list(self.selection.selected_features)

    @property
    def scores(self) -> dict[str, float]:
        return {} if self.selection is None else dict(self.selection.scores)

    @property
    def matrix_digest(self) -> str:
        return matrix_version(self.matrix, self.columns)

    @property
    def unclassified_errors(self) -> list[ErrorRecord]:
        return [error for error in self.errors if error.code == E_UNCLASSIFIED]

    @property
    def cache_report(self) -> list[dict[str, Any]]:
        return [lookup.to_dict() for lookup in self.diagnostics.cache_lookups]

    def _block_summary(self, block: FeatureBlock, *, echo_params: bool = True) -> dict[str, Any]:
        summary = block.summary()
        if not echo_params:
            summary.pop("params", None)
        if block.cache_lookup is not None:
            summary["cache"] = {
                "status": block.cache_lookup.status,
                "cache_key": block.cache_lookup.cache_key,
                "content_hash": block.cache_lookup.content_hash,
            }
        return summary

    def to_report(self) -> dict[str, Any]:
        selection_report: dict[str, Any] | None = None
        if self.selection is not None:
            selection_report = {
                "selected_features": self.selection.selected_features,
                "scores_ranked": [
                    {"feature": name, "score": self.selection.scores[name]}
                    for name in sorted(
                        self.selection.scores,
                        key=lambda item: (-self.selection.scores[item], item),
                    )
                ],
                "matrix_version": self.selection.matrix_version,
                "method": self.selection.method,
                "mode": self.selection.mode,
                "threshold": self.selection.threshold,
                "target_count": self.selection.target_count,
                "score_context": self.selection.score_context,
                "degradation_category": self.selection.degradation_category,
                "tie_break": "score_desc_then_feature_name_asc",
            }
        return {
            "status": "ok" if not self.errors else "degraded",
            "config_version": self.config.config_version,
            "statistics_version": self.config.statistics_version,
            "selection_version": self.config.selection_version,
            "config_digest": self.config.digest,
            "sample_digest": self.sample_digest,
            "matrix_version": self.matrix_digest,
            "matrix": {
                "rows": int(self.matrix.shape[0]),
                "columns": int(self.matrix.shape[1]),
                "nbytes": int(self.matrix.nbytes),
                "column_names": self.columns,
            },
            "encoding": [self._block_summary(block) for block in self.category_blocks],
            "standardization": [self._block_summary(block) for block in self.numeric_blocks],
            "cross_expansion": [self._block_summary(block) for block in self.cross_blocks],
            "cross_summary": {
                "column_count": sum(len(block.columns) for block in self.cross_blocks),
                "memory_estimate_bytes": sum(
                    int(block.params["memory_estimate_bytes"])
                    for block in self.cross_blocks
                ),
                "formula": "rows * columns * 8 bytes summed over dense float64 slot matrices",
            },
            "selection": selection_report,
            "errors": [error.to_dict() for error in self.errors],
            "unclassified_errors": [error.to_dict() for error in self.unclassified_errors],
            "error_codes": list(dict.fromkeys(self.error_codes)),
            "degradations": [degradation.to_dict() for degradation in self.degradations],
            "cache": {
                "entries": self.cache_report,
                "hit_count": sum(
                    1 for lookup in self.diagnostics.cache_lookups if lookup.status == "hit"
                ),
                "recomputed_count": sum(
                    1 for lookup in self.diagnostics.cache_lookups if lookup.status != "hit"
                ),
            },
            "snapshots": [write.to_dict() for write in self.snapshot_writes],
        }


def _error_from_exception(stage: str, exc: BaseException) -> ErrorRecord:
    if isinstance(exc, InjectedUnclassified):
        action = exc.action
        return ErrorRecord(
            stage=stage,
            code=E_UNCLASSIFIED,
            message="injected exception was not attributed by any stage",
            feature=action.feature,
            row_id=action.row_id,
            details={
                "injection_kind": action.kind,
                "origin_code": action.code,
                **action.details,
            },
        )
    return ErrorRecord(
        stage=stage,
        code=E_UNCLASSIFIED,
        message=f"unclassified {type(exc).__name__}: {exc}",
        feature=None,
        row_id=None,
        details={"exception_type": type(exc).__name__, "exception_message": str(exc)},
    )


def _empty_stage_blocks(features: Sequence[str], error: ErrorRecord) -> StageBlocks:
    return StageBlocks(blocks=[], failed_features=list(features), errors=[error])


def _fallback_selection_after_failure(
    blocks: Sequence[FeatureBlock],
    cfg: FeatureConfig,
    diagnostics: Diagnostics,
    *,
    n_rows: int,
) -> SelectionOutcome:
    matrices = [block.matrix for block in blocks if block.matrix.shape[1] > 0]
    matrix = (
        np.concatenate(matrices, axis=1)
        if matrices
        else np.zeros((n_rows, 0), dtype=np.float64)
    )
    columns = [column for block in blocks for column in block.columns]
    seen: set[str] = set()
    keep: list[int] = []
    deduped_columns: list[str] = []
    for index, column in enumerate(columns):
        if column not in seen:
            seen.add(column)
            keep.append(index)
            deduped_columns.append(column)
    if len(keep) != len(columns):
        matrix = matrix[:, keep]
    columns = deduped_columns
    version = matrix_version(matrix, columns)
    scores, context = _variance_scores(matrix, columns)
    context["fallback"] = True
    threshold = cfg.selector.default_variance_threshold
    eligible = [column for column in columns if scores[column] >= threshold]
    target = cfg.selector.target_count
    selected = _rank_scores(scores, eligible=eligible)
    if target is not None:
        selected = selected[:target]
    diagnostics.add_degradation(
        Degradation(
            stage="select",
            category="fallback_variance_after_unclassified",
            triggered_by=E_UNCLASSIFIED,
            feature="selector",
            details={"default_variance_threshold": threshold},
        )
    )
    lookup = CacheLookup(
        status="bypassed_failure",
        stage="select",
        feature="_selection",
        cache_key=sha256_obj(
            {
                "stage": "select",
                "feature": "_selection",
                "reason": E_UNCLASSIFIED,
                "matrix_version": version,
            }
        ),
        content_hash=None,
        components={"fallback": "variance", "triggered_by": E_UNCLASSIFIED},
    )
    diagnostics.add_lookup(lookup)
    return SelectionOutcome(
        selected_features=selected,
        scores=scores,
        matrix_version=version,
        method="variance",
        mode="top_k" if target is not None else "threshold",
        threshold=threshold,
        target_count=target,
        score_context=context,
        cache_lookup=lookup,
        degradation_category="fallback_variance_after_unclassified",
    )


class Pipeline:
    def __init__(
        self,
        cache_dir: str | Path,
        snapshot_dir: str | Path,
        *,
        clear: bool = False,
    ) -> None:
        self.cache = ContentAddressedCache(cache_dir, clear=clear)
        self.snapshots = SnapshotStore(snapshot_dir, clear=clear)

    def run(
        self,
        rows: Sequence[dict[str, Any]],
        cfg: FeatureConfig,
        injection: InjectionConfig | None = None,
    ) -> PipelineResult:
        injection = injection or parse_injection_config(None)
        diagnostics = Diagnostics()
        all_fields = sorted(set().union(*(row.keys() for row in rows))) if rows else []
        sample_digest = sample_content_digest(
            rows,
            id_column=cfg.id_column,
            columns=[key for key in all_fields if key != cfg.id_column],
        )
        if not rows:
            diagnostics.add_error(
                ErrorRecord(
                    stage="config",
                    code=E_CONFIG_INVALID,
                    message="sample must contain at least one row",
                    feature=None,
                    row_id=None,
                    details={"field": "sample"},
                )
            )
            return PipelineResult(
                config=cfg,
                sample_digest=sample_digest,
                matrix=np.zeros((0, 0), dtype=np.float64),
                columns=[],
                category_blocks=[],
                numeric_blocks=[],
                cross_blocks=[],
                selection=None,
                diagnostics=diagnostics,
            )

        snapshot_writes: list[SnapshotWrite] = []
        try:
            snapshot_writes.append(
                self.snapshots.put("config", cfg.config_version, cfg.raw)
            )
        except BusinessError as exc:
            diagnostics.add_error(exc.record)
            return PipelineResult(
                config=cfg,
                sample_digest=sample_digest,
                matrix=np.zeros((len(rows), 0), dtype=np.float64),
                columns=[],
                category_blocks=[],
                numeric_blocks=[],
                cross_blocks=[],
                selection=None,
                diagnostics=diagnostics,
            )

        category_names = [feature.name for feature in cfg.category_features]
        numeric_names = [feature.name for feature in cfg.numeric_features]

        try:
            encoded = encode_categories(rows, cfg, self.cache, injection, diagnostics)
        except BusinessError as exc:
            diagnostics.add_error(exc.record)
            encoded = _empty_stage_blocks(category_names, exc.record)
        except Exception as exc:
            record = _error_from_exception("encode", exc)
            diagnostics.add_error(record)
            encoded = _empty_stage_blocks(category_names, record)

        try:
            standardized = standardize_numeric(
                rows, cfg, self.cache, injection, diagnostics
            )
        except BusinessError as exc:
            diagnostics.add_error(exc.record)
            standardized = _empty_stage_blocks(numeric_names, exc.record)
        except Exception as exc:
            record = _error_from_exception("standardize", exc)
            diagnostics.add_error(record)
            standardized = _empty_stage_blocks(numeric_names, record)

        try:
            crossed = expand_cross_slots(
                rows,
                cfg,
                self.cache,
                injection,
                diagnostics,
                failed_encoded_features=encoded.failed_features,
            )
        except BusinessError as exc:
            diagnostics.add_error(exc.record)
            crossed = _empty_stage_blocks(
                [slot.slot_id for slot in cfg.cross_slots],
                exc.record,
            )
        except Exception as exc:
            record = _error_from_exception("cross", exc)
            diagnostics.add_error(record)
            crossed = _empty_stage_blocks(
                [slot.slot_id for slot in cfg.cross_slots],
                record,
            )

        upstream_codes = [
            error.code
            for error in diagnostics.errors
            if error.stage in {"encode", "standardize", "cross"}
            and error.code != E_CACHE_CORRUPT
        ]
        all_blocks = [*encoded.blocks, *standardized.blocks, *crossed.blocks]
        try:
            selection = select_features(
                rows,
                cfg,
                all_blocks,
                self.cache,
                injection,
                diagnostics,
                upstream_codes=upstream_codes,
            )
        except BusinessError as exc:
            diagnostics.add_error(exc.record)
            selection = _fallback_selection_after_failure(
                all_blocks,
                cfg,
                diagnostics,
                n_rows=len(rows),
            )
        except Exception as exc:
            record = _error_from_exception("select", exc)
            diagnostics.add_error(record)
            selection = _fallback_selection_after_failure(
                all_blocks,
                cfg,
                diagnostics,
                n_rows=len(rows),
            )

        matrices = [block.matrix for block in all_blocks if block.matrix.shape[1] > 0]
        matrix = (
            np.concatenate(matrices, axis=1)
            if matrices
            else np.zeros((len(rows), 0), dtype=np.float64)
        )
        columns = [column for block in all_blocks for column in block.columns]

        stats_payload = {
            "statistics_version": cfg.statistics_version,
            "config_version": cfg.config_version,
            "config_digest": cfg.digest,
            "sample_digest": sample_digest,
            "matrix_version": matrix_version(matrix, columns),
            "columns": columns,
            "encoding": [block.summary() for block in encoded.blocks],
            "standardization": [block.summary() for block in standardized.blocks],
            "cross_expansion": [block.summary() for block in crossed.blocks],
            "errors": [error.to_dict() for error in diagnostics.errors],
            "degradations": [
                degradation.to_dict() for degradation in diagnostics.degradations
            ],
        }
        try:
            snapshot_writes.append(
                self.snapshots.put(
                    "statistics",
                    cfg.statistics_version,
                    stats_payload,
                )
            )
        except BusinessError as exc:
            diagnostics.add_error(exc.record)

        selection_snapshot = {
            "selection_version": cfg.selection_version,
            "matrix_version": selection.matrix_version,
            "selected_features": selection.selected_features,
            "scores": selection.scores,
            "method": selection.method,
            "mode": selection.mode,
            "threshold": selection.threshold,
            "target_count": selection.target_count,
            "score_context": selection.score_context,
            "degradation_category": selection.degradation_category,
            "tie_break": "score_desc_then_feature_name_asc",
        }
        try:
            snapshot_writes.append(
                self.snapshots.put(
                    "selection",
                    cfg.selection_version,
                    selection_snapshot,
                )
            )
        except BusinessError as exc:
            diagnostics.add_error(exc.record)

        return PipelineResult(
            config=cfg,
            sample_digest=sample_digest,
            matrix=matrix,
            columns=columns,
            category_blocks=encoded.blocks,
            numeric_blocks=standardized.blocks,
            cross_blocks=crossed.blocks,
            selection=selection,
            diagnostics=diagnostics,
            snapshot_writes=snapshot_writes,
        )

    def run_incremental(
        self,
        old_rows: Sequence[dict[str, Any]],
        old_cfg: FeatureConfig,
        new_rows: Sequence[dict[str, Any]],
        new_cfg: FeatureConfig,
        injection: InjectionConfig | None = None,
    ) -> tuple[PipelineResult, "IncrementalRun"]:
        plan = compute_invalidation(old_rows, old_cfg, new_rows, new_cfg)
        result = self.run(new_rows, new_cfg, injection)
        observed_recomputed: list[str] = []
        observed_reused: list[str] = []
        recomputed_columns: list[str] = []
        reused_columns: list[str] = []
        lookup_by_feature: dict[str, CacheLookup] = {
            f"{lookup.stage}:{lookup.feature}": lookup
            for lookup in result.diagnostics.cache_lookups
        }
        for feature in plan.universe:
            lookup = lookup_by_feature.get(feature)
            if lookup is None:
                if feature in plan.recompute_features:
                    observed_recomputed.append(feature)
                continue
            if lookup.status == "hit":
                observed_reused.append(feature)
            else:
                observed_recomputed.append(feature)
        for block in [
            *result.category_blocks,
            *result.numeric_blocks,
            *result.cross_blocks,
        ]:
            if block.cache_lookup is not None and block.cache_lookup.status == "hit":
                reused_columns.extend(block.columns)
            else:
                recomputed_columns.extend(block.columns)
        for feature in plan.recompute_features:
            if feature not in observed_recomputed and feature not in observed_reused:
                observed_recomputed.append(feature)
        observed_recomputed = sorted(set(observed_recomputed))
        observed_reused = sorted(set(observed_reused) - set(observed_recomputed))
        recomputed_columns = sorted(set(recomputed_columns) - set(reused_columns))
        reused_columns = sorted(set(reused_columns))
        stages = sorted({feature.split(":", 1)[0] for feature in observed_recomputed})
        incremental = IncrementalRun(
            plan=plan,
            observed_recomputed_features=observed_recomputed,
            observed_reused_features=observed_reused,
            recompute_stages=stages,
            recomputed_columns=recomputed_columns,
            reused_columns=reused_columns,
        )
        return result, incremental


def run_pipeline(
    rows: Sequence[dict[str, Any]],
    cfg: FeatureConfig,
    *,
    cache_dir: str | Path,
    snapshot_dir: str | Path,
    injection: InjectionConfig | None = None,
    clear: bool = False,
) -> PipelineResult:
    return Pipeline(cache_dir, snapshot_dir, clear=clear).run(rows, cfg, injection)


def write_result_artifacts(result: PipelineResult, output_dir: str | Path) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    matrix_path = root / "matrix.npy"
    atomic_write_bytes(matrix_path, matrix_to_bytes(result.matrix))
    columns = {"columns": result.columns}
    atomic_write_json(root / "columns.json", columns, pretty=True)
    standardization = {
        "features": {
            block.logical_feature: {
                "columns": block.columns,
                "params": block.params,
            }
            for block in result.numeric_blocks
        }
    }
    atomic_write_json(root / "standardization.json", standardization, pretty=True)
    encoding = {
        "features": {
            block.logical_feature: {
                "columns": block.columns,
                "params": block.params,
            }
            for block in result.category_blocks
        }
    }
    atomic_write_json(root / "encoding.json", encoding, pretty=True)
    crossing = {
        "slots": {
            block.logical_feature: {
                "columns": block.columns,
                "params": block.params,
            }
            for block in result.cross_blocks
        }
    }
    atomic_write_json(root / "cross.json", crossing, pretty=True)
    selection = {
        "selected_features": result.selected_features,
        "scores": result.scores,
        "matrix_version": None if result.selection is None else result.selection.matrix_version,
        "method": None if result.selection is None else result.selection.method,
        "mode": None if result.selection is None else result.selection.mode,
        "score_context": None if result.selection is None else result.selection.score_context,
        "degradation_category": (
            None if result.selection is None else result.selection.degradation_category
        ),
        "tie_break": "score_desc_then_feature_name_asc",
    }
    atomic_write_json(root / "selection.json", selection, pretty=True)
    diagnostics = {
        "errors": [error.to_dict() for error in result.errors],
        "unclassified_errors": [error.to_dict() for error in result.unclassified_errors],
        "degradations": [degradation.to_dict() for degradation in result.degradations],
        "cache": result.cache_report,
        "snapshots": [write.to_dict() for write in result.snapshot_writes],
    }
    atomic_write_json(root / "diagnostics.json", diagnostics, pretty=True)
    report = result.to_report()
    atomic_write_json(root / "report.json", report, pretty=True)
    hashes = {
        path.name: sha256_bytes(path.read_bytes())
        for path in sorted(root.glob("*"))
        if path.is_file()
    }
    atomic_write_json(
        root / "manifest.json",
        {"artifacts": hashes, "matrix_version": report["matrix_version"]},
        pretty=True,
    )
    return hashes


@dataclass
class InvalidationPlan:
    affected_stages: list[str]
    affected_features: dict[str, list[str]]
    recompute_features: list[str]
    reused_features: list[str]
    universe: list[str]
    reasons: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_stages": self.affected_stages,
            "affected_features": self.affected_features,
            "recompute_features": self.recompute_features,
            "reused_features": self.reused_features,
            "recompute_reuse_disjoint": not bool(
                set(self.recompute_features) & set(self.reused_features)
            ),
            "reasons": self.reasons,
        }


@dataclass
class IncrementalRun:
    plan: InvalidationPlan
    observed_recomputed_features: list[str]
    observed_reused_features: list[str]
    recompute_stages: list[str]
    recomputed_columns: list[str]
    reused_columns: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "observed": {
                "recompute_stages": self.recompute_stages,
                "recomputed_features": self.observed_recomputed_features,
                "reused_features": self.observed_reused_features,
                "recomputed_columns": self.recomputed_columns,
                "reused_columns": self.reused_columns,
                "recompute_reuse_disjoint": not bool(
                    set(self.observed_recomputed_features)
                    & set(self.observed_reused_features)
                ),
            },
        }


def _universe(cfg: FeatureConfig) -> list[str]:
    return [
        *[f"encode:{feature.name}" for feature in cfg.category_features],
        *[f"standardize:{feature.name}" for feature in cfg.numeric_features],
        *[f"cross:{slot.slot_id}" for slot in cfg.cross_slots],
        "select:_selection",
    ]


def compute_invalidation(
    old_rows: Sequence[dict[str, Any]],
    old_cfg: FeatureConfig,
    new_rows: Sequence[dict[str, Any]],
    new_cfg: FeatureConfig,
    *,
    statistics_version_changed: bool | None = None,
) -> InvalidationPlan:
    universe = _universe(new_cfg)
    affected: dict[str, set[str]] = {
        "encode": set(),
        "standardize": set(),
        "cross": set(),
        "select": set(),
    }
    reasons: list[dict[str, Any]] = []

    def mark(stage: str, feature: str, reason: str, details: dict[str, Any] | None = None) -> None:
        affected[stage].add(feature)
        reasons.append(
            {
                "stage": stage,
                "feature": feature,
                "reason": reason,
                "details": details or {},
            }
        )

    def mark_all(reason: str) -> None:
        for feature in new_cfg.category_features:
            mark("encode", feature.name, reason)
        for feature in new_cfg.numeric_features:
            mark("standardize", feature.name, reason)
        for slot in new_cross_slots(new_cfg):
            mark("cross", slot, reason)
        mark("select", "_selection", reason)

    def new_cross_slots(cfg: FeatureConfig) -> list[str]:
        return [slot.slot_id for slot in cfg.cross_slots]

    if old_cfg.id_column != new_cfg.id_column or old_cfg.label_column != new_cfg.label_column:
        mark_all("row_identity_or_label_semantics_changed")
    if old_cfg.config_version != new_cfg.config_version:
        mark_all("config_version_changed")
    stats_changed = (
        old_cfg.statistics_version != new_cfg.statistics_version
        if statistics_version_changed is None
        else statistics_version_changed
    )
    if stats_changed:
        mark_all("statistics_version_changed")
    if old_cfg.selection_version != new_cfg.selection_version:
        mark("select", "_selection", "selection_version_changed")
    if old_cfg.seed != new_cfg.seed:
        mark_all("random_seed_changed")

    old_categories = {feature.name: feature for feature in old_cfg.category_features}
    new_categories = {feature.name: feature for feature in new_cfg.category_features}
    for name in sorted(set(old_categories) | set(new_categories)):
        if name not in new_categories:
            reasons.append(
                {
                    "stage": "encode",
                    "feature": name,
                    "reason": "category_feature_removed",
                    "details": {},
                }
            )
            for slot in new_cfg.cross_slots:
                if name in slot.columns:
                    mark("cross", slot.slot_id, "category_dependency_removed", {"column": name})
            mark("select", "_selection", "category_feature_removed", {"feature": name})
            continue
        if name not in old_categories or old_categories[name].raw != new_categories[name].raw:
            mark("encode", name, "category_encoding_changed")
            mark("select", "_selection", "encoded_matrix_changed", {"feature": name})

    old_numeric = {feature.name: feature for feature in old_cfg.numeric_features}
    new_numeric = {feature.name: feature for feature in new_cfg.numeric_features}
    for name in sorted(set(old_numeric) | set(new_numeric)):
        if name not in new_numeric:
            reasons.append(
                {
                    "stage": "standardize",
                    "feature": name,
                    "reason": "numeric_feature_removed",
                    "details": {},
                }
            )
            mark("select", "_selection", "numeric_feature_removed", {"feature": name})
            continue
        if name not in old_numeric or old_numeric[name].raw != new_numeric[name].raw:
            mark("standardize", name, "numeric_standardization_changed")
            mark("select", "_selection", "standardized_matrix_changed", {"feature": name})

    old_slots = {slot.slot_id: slot for slot in old_cfg.cross_slots}
    new_slots = {slot.slot_id: slot for slot in new_cfg.cross_slots}
    for slot_id in sorted(set(old_slots) | set(new_slots)):
        if slot_id not in new_slots:
            reasons.append(
                {
                    "stage": "cross",
                    "feature": slot_id,
                    "reason": "cross_slot_removed",
                    "details": {},
                }
            )
            mark("select", "_selection", "cross_slot_removed", {"slot": slot_id})
            continue
        if slot_id not in old_slots or old_slots[slot_id].raw != new_slots[slot_id].raw:
            mark("cross", slot_id, "cross_slot_changed")
            mark("select", "_selection", "cross_features_changed", {"slot": slot_id})

    if old_cfg.selector.raw != new_cfg.selector.raw:
        mark("select", "_selection", "selector_changed")

    if list(old_rows) and list(new_rows):
        old_ids = [str(row[old_cfg.id_column]) for row in old_rows]
        new_ids = [str(row[new_cfg.id_column]) for row in new_rows]
        if old_ids != new_ids:
            mark_all("row_sequence_changed")
        else:
            for old_row, new_row in zip(old_rows, new_rows, strict=True):
                row_id = str(new_row[new_cfg.id_column])
                fields = set(old_row) | set(new_row)
                for field_name in sorted(fields):
                    if field_name == new_cfg.id_column:
                        continue
                    if old_row.get(field_name) == new_row.get(field_name):
                        continue
                    if field_name == new_cfg.label_column:
                        for feature in new_cfg.category_features:
                            if feature.encoding == "target":
                                mark(
                                    "encode",
                                    feature.name,
                                    "label_changed",
                                    {"row_id": row_id},
                                )
                        mark("select", "_selection", "label_changed", {"row_id": row_id})
                    elif field_name in new_categories:
                        mark(
                            "encode",
                            field_name,
                            "row_value_changed",
                            {"row_id": row_id},
                        )
                        for slot in new_cfg.cross_slots:
                            if field_name in slot.columns:
                                mark(
                                    "cross",
                                    slot.slot_id,
                                    "category_dependency_changed",
                                    {"row_id": row_id, "column": field_name},
                                )
                        mark(
                            "select",
                            "_selection",
                            "encoded_matrix_changed",
                            {"row_id": row_id, "feature": field_name},
                        )
                    elif field_name in new_numeric:
                        mark(
                            "standardize",
                            field_name,
                            "row_value_changed",
                            {"row_id": row_id},
                        )
                        mark(
                            "select",
                            "_selection",
                            "standardized_matrix_changed",
                            {"row_id": row_id, "feature": field_name},
                        )
                    else:
                        mark("select", "_selection", "sample_changed", {"row_id": row_id})
    elif list(old_rows) != list(new_rows):
        mark_all("sample_presence_changed")

    affected_features = {
        stage: sorted(features, key=str) for stage, features in affected.items()
    }
    recompute: list[str] = []
    for stage in ("encode", "standardize", "cross", "select"):
        recompute.extend(f"{stage}:{feature}" for feature in affected_features[stage])
    recompute = sorted(set(recompute))
    reused = sorted(set(universe) - set(recompute))
    affected_stages = [
        stage for stage in ("encode", "standardize", "cross", "select") if affected_features[stage]
    ]
    return InvalidationPlan(
        affected_stages=affected_stages,
        affected_features=affected_features,
        recompute_features=recompute,
        reused_features=reused,
        universe=universe,
        reasons=reasons,
    )

