from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .calibration import CalibrationStats, estimate_calibration
from .errors import (
    DiagnosticError,
    E_FORMAT_CORRUPT,
    E_LINEAGE_BROKEN,
    E_SEMANTIC_DRIFT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_STALE_RESULT,
    E_VERSION_UNSUPPORTED,
)
from .fixtures import FixturePaths, fixture_paths, generate_fixtures
from .formats import (
    LayerQuant,
    ParamSet,
    downgrade_v3_to_v1,
    parse_quant_file,
    parse_quant_params,
    upgrade_v1_to_v2,
    upgrade_v2_to_v3,
)
from .inference import (
    DeterministicEngine,
    LayerCache,
    adjudicate_semantic_equivalence,
    verify_trace,
)
from .model import LAYER_ORDER, NetworkWeights
from .snapshots import Snapshot, SnapshotStore, build_snapshot


@dataclass
class FixtureBundle:
    paths: FixturePaths
    weights: NetworkWeights
    images: np.ndarray
    calibration_images: np.ndarray
    calibration: CalibrationStats
    v1: ParamSet
    v2: ParamSet
    v3: ParamSet
    v3_dynamic: ParamSet
    v2_drift: ParamSet


def load_fixtures(root: str | Path) -> FixtureBundle:
    generate_fixtures(root)
    paths = fixture_paths(root)
    images = np.load(paths.normal_images, allow_pickle=False)
    calibration_images = np.load(paths.calibration_images, allow_pickle=False)
    weights = NetworkWeights()
    calibration_payload = json.loads(
        paths.calibration_stats.read_text(encoding="utf-8")
    )
    calibration = CalibrationStats.from_dict(calibration_payload)
    return FixtureBundle(
        paths=paths,
        weights=weights,
        images=images,
        calibration_images=calibration_images,
        calibration=calibration,
        v1=parse_quant_file(paths.v1).params,
        v2=parse_quant_file(paths.v2).params,
        v3=parse_quant_file(paths.v3).params,
        v3_dynamic=parse_quant_file(paths.v3_dynamic).params,
        v2_drift=parse_quant_file(paths.v2_drift).params,
    )


def _replace_layers(
    params: ParamSet, replacements: dict[str, LayerQuant]
) -> ParamSet:
    return replace(
        params,
        layers=tuple(
            replacements.get(layer.name, layer) for layer in params.layers
        ),
    )


def _scale_change(params: ParamSet, layer_name: str, factor: float) -> ParamSet:
    layer = params.layer(layer_name)
    scaled = replace(
        layer,
        weight_scale=layer.weight_scale * factor,
        channel_scales=tuple(value * factor for value in layer.channel_scales),
    )
    return _replace_layers(params, {layer_name: scaled})


def _dynamic_switch(params: ParamSet, layer_name: str) -> ParamSet:
    layer = params.layer(layer_name)
    return replace(
        _replace_layers(
            params, {layer_name: replace(layer, dynamic_range_mode="per_activation")}
        ),
        format_version="v3",
    )


class Workbench:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.bundle = load_fixtures(self.root)
        self.engine = DeterministicEngine(self.bundle.weights)
        self.recent_errors: list[dict[str, Any]] = []
        self._error_sequence = 0

    def _record_error(self, error: DiagnosticError) -> dict[str, Any]:
        self._error_sequence += 1
        payload = {"sequence": self._error_sequence, **error.to_dict()}
        self.recent_errors.append(payload)
        if len(self.recent_errors) > 40:
            self.recent_errors = self.recent_errors[-40:]
        return payload

    def _capture(
        self, code: str, call: Callable[[], Any]
    ) -> tuple[DiagnosticError | None, Any]:
        try:
            return None, call()
        except DiagnosticError as error:
            self._record_error(error)
            if error.code != code:
                raise
            return error, None

    def _check(
        self, checks: list[dict[str, Any]], name: str, passed: bool, details: Any
    ) -> None:
        checks.append({"name": name, "passed": bool(passed), "details": details})

    def run_acceptance(self) -> dict[str, Any]:
        self.recent_errors = []
        self._error_sequence = 0
        checks: list[dict[str, Any]] = []
        sections: dict[str, Any] = {}
        bundle = self.bundle
        engine = DeterministicEngine(bundle.weights)

        # 1. Format dispatch and malformed inputs.
        parsed_v1 = parse_quant_file(bundle.paths.v1)
        parsed_v2 = parse_quant_file(bundle.paths.v2)
        parsed_v3 = parse_quant_file(bundle.paths.v3)
        unknown_error, _ = self._capture(
            E_VERSION_UNSUPPORTED,
            lambda: parse_quant_file(bundle.paths.unknown_version),
        )
        corrupt_error, _ = self._capture(
            E_FORMAT_CORRUPT,
            lambda: parse_quant_file(bundle.paths.corrupt_v2),
        )
        truncated_error, _ = self._capture(
            E_FORMAT_CORRUPT,
            lambda: parse_quant_params("version=v2\nfields=layer,weight_scale\n"),
        )
        format_section = {
            "loaded": {
                "v1": {
                    "layers": len(parsed_v1.params.layers),
                    "formats": parsed_v1.params.format_version,
                },
                "v2": {
                    "layers": len(parsed_v2.params.layers),
                    "per_channel": [
                        layer.per_channel for layer in parsed_v2.params.layers
                    ],
                },
                "v3": {
                    "layers": len(parsed_v3.params.layers),
                    "calibration_version": (
                        parsed_v3.calibration.version
                        if parsed_v3.calibration is not None
                        else None
                    ),
                },
            },
            "expected_errors": [
                error.to_dict()
                for error in (
                    unknown_error,
                    corrupt_error,
                    truncated_error,
                )
                if error is not None
            ],
        }
        sections["formats"] = format_section
        self._check(
            checks,
            "format_loading_and_dispatch",
            unknown_error is not None
            and corrupt_error is not None
            and truncated_error is not None
            and parsed_v1.params.format_version == "v1"
            and parsed_v2.params.format_version == "v2"
            and parsed_v3.params.format_version == "v3",
            {
                "unknown": unknown_error.code if unknown_error else None,
                "corrupt": corrupt_error.to_dict() if corrupt_error else None,
                "truncated": (
                    truncated_error.to_dict() if truncated_error else None
                ),
            },
        )

        # 2. Upgrade and downgrade migrations.
        v1_to_v2 = upgrade_v1_to_v2(bundle.v1)
        v2_to_v3 = upgrade_v2_to_v3(bundle.v2, bundle.calibration)
        v3_to_v1 = downgrade_v3_to_v1(bundle.v3_dynamic, channel_flatten_rule="mean")
        migration_section = {
            "v1_to_v2": {
                "fill_records": [item.to_dict() for item in v1_to_v2.fills],
            },
            "v2_to_v3": {
                "fill_records": [item.to_dict() for item in v2_to_v3.fills],
            },
            "v3_to_v1": {
                "drop_records": [item.to_dict() for item in v3_to_v1.drops],
                "semantic_loss": v3_to_v1.semantic_loss,
                "flatten_rule": "mean",
            },
        }
        sections["migrations"] = migration_section
        self._check(
            checks,
            "migration_fill_and_drop_provenance",
            len(v1_to_v2.fills) >= len(bundle.v1.layers) * 4
            and all(item.source for item in v1_to_v2.fills)
            and len(v2_to_v3.fills) >= len(bundle.v2.layers) * 2
            and any(
                item.field == "dynamic_range_mode" for item in v2_to_v3.fills
            )
            and v3_to_v1.semantic_loss
            and any(
                item.field == "dynamic_range_mode" and item.semantic_loss
                for item in v3_to_v1.drops
            ),
            migration_section,
        )

        # 3. Determinism and cache reuse.
        baseline = build_snapshot(
            bundle.v2,
            bundle.calibration,
            version=20,
            model_fingerprint=bundle.weights.fingerprint(),
        )
        cache = LayerCache()
        first = engine.infer(baseline, bundle.images, cache=cache)
        second = engine.infer(baseline, bundle.images, cache=cache)
        fresh_engine = DeterministicEngine(bundle.weights)
        fresh = fresh_engine.infer(
            baseline, bundle.images, cache=LayerCache()
        )
        conv2_key = cache.explain("conv2")
        layer_keys = {
            layer: cache.explain(layer) for layer in LAYER_ORDER
        }
        required_key_fields = {
            "image_hash",
            "input_hash",
            "upstream_hash",
            "upstream_key",
            "quant_version",
            "calibration_version",
            "per_channel",
        }
        cache_section = {
            "first": {
                "recomputed": list(first.recomputed_layers),
                "scores_checksum": first.scores_checksum,
                "layer_checksums": [
                    layer.activation_checksum for layer in first.layers
                ],
            },
            "second": {
                "reused": list(second.reused_layers),
                "recomputed": list(second.recomputed_layers),
                "scores_checksum": second.scores_checksum,
            },
            "fresh_cache": {
                "recomputed": list(fresh.recomputed_layers),
                "scores_checksum": fresh.scores_checksum,
                "result_checksum": fresh.result_checksum,
            },
            "conv2_key": conv2_key,
            "layer_keys": layer_keys,
        }
        sections["cache"] = cache_section
        self._check(
            checks,
            "byte_reproducible_and_content_addressed_cache",
            first.recomputed_layers == LAYER_ORDER
            and second.reused_layers == LAYER_ORDER
            and second.recomputed_layers == ()
            and first.scores_checksum == second.scores_checksum
            and first.result_checksum == second.result_checksum
            and first.result_checksum == fresh.result_checksum
            and conv2_key is not None
            and all(layer_keys.values())
            and required_key_fields.issubset(conv2_key["components"]),
            cache_section,
        )

        # 4. Directed invalidation for three change classes.
        invalidation_section: dict[str, Any] = {}
        invalidation_checks: list[bool] = []

        scale_cache = LayerCache()
        engine.infer(baseline, bundle.images, cache=scale_cache)
        scaled_params = _scale_change(bundle.v2, "conv2", 1.05)
        scaled_snapshot = build_snapshot(
            scaled_params,
            bundle.calibration,
            version=21,
            model_fingerprint=bundle.weights.fingerprint(),
        )
        scaled_analysis = engine.analyze_change(baseline, scaled_snapshot)
        scaled_result = engine.infer(
            scaled_snapshot, bundle.images, cache=scale_cache
        )
        invalidation_section["scale_change_conv2"] = {
            "analysis": scaled_analysis.to_dict(),
            "inference": {
                "reused": list(scaled_result.reused_layers),
                "recomputed": list(scaled_result.recomputed_layers),
            },
        }
        invalidation_checks.append(
            scaled_analysis.affected_layers == ("conv2", "fc")
            and scaled_analysis.reusable_layers == ("conv1",)
            and scaled_result.reused_layers == ("conv1",)
            and scaled_result.recomputed_layers == ("conv2", "fc")
            and {event.kind for event in scaled_analysis.events} == {"scale"}
        )

        sample_cache = LayerCache()
        engine.infer(baseline, bundle.images, cache=sample_cache)
        sample_calibration = estimate_calibration(
            bundle.weights,
            bundle.calibration_images,
            samples_by_layer={"conv1": 16, "conv2": 8, "fc": 16},
        )
        sample_snapshot = build_snapshot(
            bundle.v2,
            sample_calibration,
            version=22,
            model_fingerprint=bundle.weights.fingerprint(),
        )
        sample_analysis = engine.analyze_change(baseline, sample_snapshot)
        sample_result = engine.infer(
            sample_snapshot, bundle.images, cache=sample_cache
        )
        invalidation_section["calibration_samples_conv2"] = {
            "analysis": sample_analysis.to_dict(),
            "inference": {
                "reused": list(sample_result.reused_layers),
                "recomputed": list(sample_result.recomputed_layers),
            },
        }
        invalidation_checks.append(
            sample_analysis.affected_layers == ("conv2", "fc")
            and sample_analysis.reusable_layers == ("conv1",)
            and sample_result.reused_layers == ("conv1",)
            and sample_result.recomputed_layers == ("conv2", "fc")
            and "calibration_samples"
            in {event.kind for event in sample_analysis.events}
        )

        dynamic_cache = LayerCache()
        engine.infer(baseline, bundle.images, cache=dynamic_cache)
        dynamic_params = _dynamic_switch(bundle.v2, "conv2")
        dynamic_snapshot = build_snapshot(
            dynamic_params,
            bundle.calibration,
            version=23,
            model_fingerprint=bundle.weights.fingerprint(),
        )
        dynamic_analysis = engine.analyze_change(baseline, dynamic_snapshot)
        dynamic_result = engine.infer(
            dynamic_snapshot, bundle.images, cache=dynamic_cache
        )
        invalidation_section["dynamic_mode_conv2"] = {
            "analysis": dynamic_analysis.to_dict(),
            "inference": {
                "reused": list(dynamic_result.reused_layers),
                "recomputed": list(dynamic_result.recomputed_layers),
            },
        }
        invalidation_checks.append(
            dynamic_analysis.affected_layers == ("conv2", "fc")
            and dynamic_analysis.reusable_layers == ("conv1",)
            and dynamic_result.reused_layers == ("conv1",)
            and dynamic_result.recomputed_layers == ("conv2", "fc")
            and "dynamic_range_mode"
            in {event.kind for event in dynamic_analysis.events}
        )
        sections["invalidation"] = invalidation_section
        self._check(
            checks,
            "directed_dependency_invalidation",
            all(invalidation_checks),
            invalidation_section,
        )

        # 5. Trace replay, stale dependency rejection and broken lineage.
        trace_cache = LayerCache()
        trace_result = engine.infer(baseline, bundle.images, cache=trace_cache)
        record = engine.make_trace(trace_result, bundle.images, register=True)
        replay = verify_trace(
            engine, baseline, record, cache=trace_cache
        )
        stale_error, _ = self._capture(
            E_STALE_RESULT,
            lambda: verify_trace(
                engine, scaled_snapshot, record, cache=trace_cache
            ),
        )
        engine.traces.delete(record.record_id)
        missing_error, _ = self._capture(
            E_LINEAGE_BROKEN,
            lambda: verify_trace(engine, baseline, record.record_id),
        )
        engine.traces.put(record)
        tampered_scores = tuple(
            tuple(value + 0.25 for value in row) for row in record.scores
        )
        tampered_record = replace(record, scores=tampered_scores)
        broken_error, _ = self._capture(
            E_LINEAGE_BROKEN,
            lambda: verify_trace(
                engine, baseline, tampered_record, cache=trace_cache
            ),
        )
        trace_section = {
            "record_id": record.record_id,
            "recorded_snapshot_version": record.snapshot_version,
            "recorded_params_version": record.params_version,
            "recorded_calibration_version": record.calibration_version,
            "cache_keys": {
                item.layer: item.cache_key for item in record.layers
            },
            "replay": replay,
            "expected_errors": [
                error.to_dict()
                for error in (stale_error, missing_error, broken_error)
                if error is not None
            ],
        }
        sections["trace"] = trace_section
        self._check(
            checks,
            "lineage_replay_stale_and_broken_detection",
            replay["scores_checksum"] == record.scores_checksum
            and stale_error is not None
            and stale_error.layer is not None
            and stale_error.source_version is not None
            and missing_error is not None
            and broken_error is not None
            and broken_error.details.get("first_deviation_layer") == "fc"
            and broken_error.details.get("deviation") is not None,
            trace_section,
        )

        # 6. Cross-format equivalence and error-spectrum attribution.
        equivalent_snapshots = [
            build_snapshot(
                bundle.v1,
                bundle.calibration,
                version=31,
                model_fingerprint=bundle.weights.fingerprint(),
            ),
            build_snapshot(
                bundle.v2,
                bundle.calibration,
                version=32,
                model_fingerprint=bundle.weights.fingerprint(),
            ),
            build_snapshot(
                bundle.v3,
                bundle.calibration,
                version=33,
                model_fingerprint=bundle.weights.fingerprint(),
            ),
        ]
        equivalence = adjudicate_semantic_equivalence(
            engine, equivalent_snapshots, bundle.images
        )
        migrated_upgrade_snapshots = [
            equivalent_snapshots[0],
            build_snapshot(
                v1_to_v2.params,
                bundle.calibration,
                version=36,
                model_fingerprint=bundle.weights.fingerprint(),
            ),
            build_snapshot(
                v2_to_v3.params,
                bundle.calibration,
                version=37,
                model_fingerprint=bundle.weights.fingerprint(),
            ),
        ]
        migration_equivalence = adjudicate_semantic_equivalence(
            engine, migrated_upgrade_snapshots, bundle.images
        )
        drift_snapshot = build_snapshot(
            bundle.v2_drift,
            bundle.calibration,
            version=34,
            model_fingerprint=bundle.weights.fingerprint(),
        )
        drift_error, _ = self._capture(
            E_SEMANTIC_DRIFT,
            lambda: adjudicate_semantic_equivalence(
                engine,
                [equivalent_snapshots[1], drift_snapshot],
                bundle.images,
            ),
        )
        dynamic_snapshot = build_snapshot(
            bundle.v3_dynamic,
            bundle.calibration,
            version=35,
            model_fingerprint=bundle.weights.fingerprint(),
        )
        dynamic_spectrum = engine.compute_error_spectrum(
            dynamic_snapshot, bundle.images
        )
        semantic_section = {
            "equivalence": equivalence,
            "migration_equivalence": migration_equivalence,
            "drift_error": drift_error.to_dict() if drift_error else None,
            "baseline_error_spectrum": equivalence["spectra"][1],
            "dynamic_overlay_spectrum": dynamic_spectrum,
        }
        sections["semantic"] = semantic_section
        self._check(
            checks,
            "cross_format_semantics_and_error_spectrum",
            equivalence["equivalent"]
            and len(set(equivalence["scores_checksums"])) == 1
            and migration_equivalence["equivalent"]
            and len(set(migration_equivalence["scores_checksums"])) == 1
            and drift_error is not None
            and drift_error.layer == "conv2"
            and all(
                "p50" in layer["weight_rounding"]
                and "p99" in layer["per_channel_scaling"]
                and "max" in layer["calibration_range"]
                and "amplification_ratio" in layer
                and layer["dominant_source_level"]
                for layer in equivalence["spectra"][1]["layers"]
            ),
            semantic_section,
        )

        # 7. Whole-snapshot replacement, rollback, corruption and stage busy.
        initial_snapshot = Snapshot.from_file(bundle.paths.current_snapshot)
        store = SnapshotStore(initial_snapshot)
        rollback_candidate = Snapshot.from_file(bundle.paths.rollback_snapshot)
        rollback_error, _ = self._capture(
            E_SNAPSHOT_ROLLBACK,
            lambda: store.commit(rollback_candidate),
        )
        corrupt_error, _ = self._capture(
            E_SNAPSHOT_CORRUPT,
            lambda: Snapshot.from_file(bundle.paths.corrupt_snapshot),
        )
        replacement = build_snapshot(
            bundle.v3,
            bundle.calibration,
            version=store.current.version + 1,
            model_fingerprint=bundle.weights.fingerprint(),
        )
        old_reference = store.current
        with store.hold_stage():
            busy_error, _ = self._capture(
                E_STAGE_BUSY,
                lambda: store.commit(replacement),
            )
        committed = store.commit(replacement)
        snapshot_section = {
            "initial_version": initial_snapshot.version,
            "committed_version": committed.version,
            "old_reference_unchanged": (
                old_reference.checksum == initial_snapshot.checksum
            ),
            "whole_replacement": committed is replacement,
            "store_history": [item.version for item in store.history],
            "expected_errors": [
                error.to_dict()
                for error in (rollback_error, corrupt_error, busy_error)
                if error is not None
            ],
        }
        sections["snapshots"] = snapshot_section
        self._check(
            checks,
            "snapshot_rollback_corruption_busy_and_whole_replace",
            rollback_error is not None
            and corrupt_error is not None
            and busy_error is not None
            and committed.version == 3
            and store.current is replacement
            and snapshot_section["old_reference_unchanged"],
            snapshot_section,
        )

        passed = all(check["passed"] for check in checks)
        return {
            "workbench": "deterministic-cnn-quant-diagnostics",
            "root": str(self.root),
            "network": {
                "layers": list(LAYER_ORDER),
                "image_shape": list(bundle.images.shape[1:]),
                "batch_size": int(bundle.images.shape[0]),
                "model_fingerprint": bundle.weights.fingerprint(),
            },
            "passed": passed,
            "checks": checks,
            "sections": sections,
            "recent_errors": list(self.recent_errors),
        }


def write_acceptance_report(
    root: str | Path, output: str | Path | None = None
) -> tuple[dict[str, Any], Path]:
    workbench = Workbench(root)
    report = workbench.run_acceptance()
    destination = (
        Path(output)
        if output is not None
        else Path(root) / "acceptance_report.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report, destination
