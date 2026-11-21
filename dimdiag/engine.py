"""End-to-end diagnostic run orchestration."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .config import (
    method_label,
    precision_identity,
    validate_config,
)
from .core import (
    ExecutionResult,
    deterministic_projection_matrix,
    execute_core,
    plan_resources,
)
from .data import Dataset, load_dataset
from .diagnostics import (
    compute_error_spectrum,
    enforce_error_tolerance,
    threshold_trace,
)
from .errors import (
    E_INCONSISTENT,
    E_PRECISION_CHANGED,
    E_SNAPSHOT_CORRUPT,
    BusinessError,
)
from .precision import PrecisionOps, PrecisionSpec
from .serialization import (
    array_checksum,
    atomic_write_bytes,
    decode_array,
    encode_array,
    json_safe,
    pretty_json_bytes,
    sha256_json,
)
from .snapshot import (
    SCHEMA_VERSION,
    SnapshotStore,
    finalize_snapshot,
    make_section,
    verify_snapshot,
)


@dataclass
class RunOutcome:
    config: dict[str, Any]
    dataset: Dataset
    observed: ExecutionResult
    reference: ExecutionResult
    error_spectrum: dict[str, Any]
    threshold_trace: dict[str, Any]
    snapshot: dict[str, Any] | None
    selfcheck: dict[str, Any] | None
    response: dict[str, Any]


def _encoded_array(value: np.ndarray) -> dict[str, Any]:
    return encode_array(np.asarray(value))


def _projection_stage_for_snapshot(stage: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (
            _encoded_array(value)
            if key == "matrix"
            else json_safe(copy.deepcopy(value))
        )
        for key, value in stage.items()
    }


def _coordinate_tolerance(precision: PrecisionSpec, scale: float) -> float:
    if precision.mode == "float64":
        return max(1.0e-12, 32.0 * np.finfo(np.float64).eps * max(1.0, scale))
    if precision.mode == "float32":
        return max(1.0e-7, 32.0 * np.finfo(np.float32).eps * max(1.0, scale))
    return 1.0e-12


def reconstruct_single_coordinate(
    snapshot: Mapping[str, Any],
    dataset: Dataset,
    row_index: int,
) -> dict[str, Any]:
    sections = snapshot["sections"]
    standard = sections["standardization"]["content"]
    model = sections["model"]["content"]
    precision = PrecisionSpec(
        mode=snapshot["precision"]["mode"],
        fixed_scale=int(snapshot["precision"]["fixed_scale"]),
        rounding=snapshot["precision"]["rounding"],
    )
    ops = PrecisionOps(precision)
    mean = decode_array(standard["numeric_mean"], "standardization.numeric_mean")
    scale = decode_array(standard["numeric_scale"], "standardization.numeric_scale")
    source = ops.cast(dataset.numeric[row_index : row_index + 1])
    if bool(standard["standardize"]):
        if ops.mode == "fixed":
            fixed_scale = float(precision.fixed_scale)
            standardized = ops._round_values(
                (source.astype(np.int64) - mean.astype(np.int64)).astype(np.float64)
                * fixed_scale
                / scale.astype(np.float64)
            )
        else:
            standardized = ((source - mean) / scale).astype(ops.storage_dtype)
    else:
        standardized = source
    one_hot, _ = ops.one_hot(
        dataset.categorical[row_index : row_index + 1],
        list(standard["categorical_columns"]),
        standard["category_levels"],
    )
    encoded = ops.concatenate_features(standardized, one_hot)
    center = decode_array(standard["encoded_center"], "standardization.encoded_center")
    centered = ops.subtract_center(encoded, center)

    pca_content = model.get("pca")
    projection_stages = model["projection_stages"]
    if model["effective_method"] == "pca":
        components = decode_array(pca_content["components"], "model.pca.components")
        reconstructed = ops.matmul(centered, components.T)
        intermediate = None
    elif model["effective_method"] == "random_projection":
        matrix = decode_array(
            projection_stages[0]["matrix"],
            "model.projection_stages.0.matrix",
        )
        reconstructed = ops.matmul(encoded, matrix)
        intermediate = None
    elif model["effective_method"] == "pca_random_projection":
        components = decode_array(pca_content["components"], "model.pca.components")
        intermediate = ops.matmul(centered, components.T)
        matrix = decode_array(
            projection_stages[1]["matrix"],
            "model.projection_stages.1.matrix",
        )
        reconstructed = ops.matmul(intermediate, matrix)
    else:
        raise BusinessError(
            E_INCONSISTENT,
            "snapshot method cannot be reconstructed",
            {"category": "single_sample_reconstruction", "method": model["effective_method"]},
        )
    return {
        "coordinate": reconstructed,
        "intermediate": intermediate,
        "encoded": encoded,
        "centered": centered,
    }


def run_self_check(
    snapshot: Mapping[str, Any],
    dataset: Dataset,
    config: Mapping[str, Any],
    *,
    raise_on_fail: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    try:
        verify_snapshot(snapshot)
        checks.append(
            {
                "category": "snapshot_integrity",
                "passed": True,
                "message": "overall and section checksums match",
            }
        )
    except BusinessError as exc:
        checks.append(
            {
                "category": "snapshot_integrity",
                "passed": False,
                "message": exc.message,
                "details": exc.details,
            }
        )

    sections = snapshot["sections"]
    coordinate_content = sections["coordinates"]["content"]
    evaluation_content = sections["evaluation"]["content"]
    coordinates = decode_array(coordinate_content["values"], "coordinates.values")
    coordinate_checksum = array_checksum(coordinates)
    expected_checksum = evaluation_content["coordinates_array_checksum"]
    checks.append(
        {
            "category": "coordinate_checksum",
            "passed": coordinate_checksum == expected_checksum,
            "coordinate_checksum": coordinate_checksum,
            "evaluation_checksum": expected_checksum,
        }
    )
    checks.append(
        {
            "category": "evaluation_coordinate_version",
            "passed": (
                int(evaluation_content["coordinates_version"])
                == int(snapshot["snapshot_version"])
            ),
            "coordinates_version": int(evaluation_content["coordinates_version"]),
            "snapshot_version": int(snapshot["snapshot_version"]),
        }
    )

    row_index = 0
    model = sections["model"]["content"]
    expected_coordinate = coordinates[row_index]
    try:
        rebuilt = reconstruct_single_coordinate(snapshot, dataset, row_index)
        rebuilt_float = np.asarray(
            PrecisionOps(
                PrecisionSpec(
                    mode=snapshot["precision"]["mode"],
                    fixed_scale=int(snapshot["precision"]["fixed_scale"]),
                    rounding=snapshot["precision"]["rounding"],
                )
            ).to_float(rebuilt["coordinate"], np.float64)
        ).reshape(-1)
        expected_float = (
            PrecisionOps(
                PrecisionSpec(
                    mode=snapshot["precision"]["mode"],
                    fixed_scale=int(snapshot["precision"]["fixed_scale"]),
                    rounding=snapshot["precision"]["rounding"],
                )
            )
            .to_float(expected_coordinate, np.float64)
            .reshape(-1)
        )
        difference = float(np.max(np.abs(rebuilt_float - expected_float)))
        scale = max(
            1.0,
            float(np.max(np.abs(expected_float))) if expected_float.size else 1.0,
        )
        tolerance = _coordinate_tolerance(
            PrecisionSpec(
                mode=snapshot["precision"]["mode"],
                fixed_scale=int(snapshot["precision"]["fixed_scale"]),
                rounding=snapshot["precision"]["rounding"],
            ),
            scale,
        )
        checks.append(
            {
                "category": "single_sample_reconstruction",
                "passed": bool(difference <= tolerance),
                "row_index": row_index,
                "row_id": dataset.row_ids[row_index],
                "max_abs_difference": difference,
                "tolerance": tolerance,
                "method": model["effective_method"],
            }
        )
    except Exception as exc:
        checks.append(
            {
                "category": "single_sample_reconstruction",
                "passed": False,
                "row_index": row_index,
                "message": str(exc),
            }
        )

    ops = PrecisionOps(
        PrecisionSpec(
            mode=snapshot["precision"]["mode"],
            fixed_scale=int(snapshot["precision"]["fixed_scale"]),
            rounding=snapshot["precision"]["rounding"],
        )
    )
    regeneration_ok = True
    regeneration_details: list[dict[str, Any]] = []
    for index, stage in enumerate(model["projection_stages"]):
        if stage["stage"] != "random_projection":
            continue
        parameters = stage["parameters"]
        stored = decode_array(
            stage["matrix"], f"model.projection_stages.{index}.matrix"
        )
        regenerated_float = deterministic_projection_matrix(
            int(stage["input_dim"]),
            int(stage["output_dim"]),
            int(parameters["seed"]),
            float(parameters["density"]),
        )
        regenerated = ops.cast(regenerated_float)
        equal = bool(np.array_equal(stored, regenerated))
        regeneration_ok = regeneration_ok and equal
        regeneration_details.append(
            {
                "stage_index": index,
                "seed": int(parameters["seed"]),
                "density": float(parameters["density"]),
                "exact_match": equal,
                "stored_checksum": array_checksum(stored),
                "regenerated_checksum": array_checksum(regenerated),
            }
        )
    checks.append(
        {
            "category": "random_projection_matrix_reproducible",
            "passed": regeneration_ok,
            "applicable": bool(regeneration_details),
            "stages": regeneration_details,
        }
    )

    if model["effective_method"] == "pca_random_projection":
        try:
            intermediate = decode_array(
                coordinate_content["intermediate_values"],
                "coordinates.intermediate_values",
            )
            rp_matrix = decode_array(
                model["projection_stages"][1]["matrix"],
                "model.projection_stages.1.matrix",
            )
            reapplied = ops.matmul(intermediate, rp_matrix)
            exact = bool(np.array_equal(reapplied, coordinates))
            checks.append(
                {
                    "category": "two_stage_projection_consistency",
                    "passed": exact,
                    "intermediate_dim": int(intermediate.shape[1]),
                    "target_dim": int(coordinates.shape[1]),
                    "exact_match": exact,
                }
            )

            components = ops.to_float(
                decode_array(model["pca"]["components"], "model.pca.components"),
                np.float64,
            )
            center_values = decode_array(
                sections["standardization"]["content"]["encoded_center"],
                "standardization.encoded_center",
            )
            encoded = _reconstruct_encoded_matrix(snapshot, dataset)
            centered_float = ops.to_float(encoded, np.float64) - ops.to_float(
                center_values, np.float64
            )
            rp_float = ops.to_float(rp_matrix, np.float64)
            composed = np.matmul(components.T, rp_float)
            equivalent_direct = np.matmul(centered_float, composed)
            actual_float = ops.to_float(coordinates, np.float64)
            max_difference = float(
                np.max(np.abs(equivalent_direct - actual_float))
            )
            scale = max(1.0, float(np.max(np.abs(actual_float))))
            if ops.mode == "float64":
                tolerance = max(
                    float(config["error_tolerance"]["two_stage_max_abs"]),
                    128.0 * np.finfo(np.float64).eps * scale,
                )
            elif ops.mode == "float32":
                tolerance = max(
                    float(config["error_tolerance"]["two_stage_max_abs"]),
                    64.0 * np.finfo(np.float32).eps * scale,
                )
            else:
                tolerance = max(
                    float(config["error_tolerance"]["two_stage_max_abs"]),
                    2.0
                    * np.sqrt(float(intermediate.shape[1]))
                    / float(snapshot["precision"]["fixed_scale"]),
                )
            checks.append(
                {
                    "category": "random_projection_and_two_stage_consistency",
                    "passed": bool(max_difference <= tolerance),
                    "same_seed": True,
                    "same_density": True,
                    "second_stage_seed": int(
                        model["projection_stages"][1]["parameters"]["seed"]
                    ),
                    "second_stage_density": float(
                        model["projection_stages"][1]["parameters"]["density"]
                    ),
                    "equivalent_direct_max_abs_difference": max_difference,
                    "tolerance": tolerance,
                    "note": (
                        "The direct path uses the composed matrix "
                        "PCA.components.T @ second_stage_matrix with the same "
                        "second-stage seed and density."
                    ),
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "category": "random_projection_and_two_stage_consistency",
                    "passed": False,
                    "message": str(exc),
                }
            )

    failed = [check for check in checks if not check["passed"]]
    result = {
        "overall_passed": not failed,
        "checks": checks,
        "failed_categories": [check["category"] for check in failed],
        "snapshot_version": int(snapshot["snapshot_version"]),
        "evaluation_coordinate_version": int(
            evaluation_content["coordinates_version"]
        ),
        "checked_samples": [dataset.row_ids[row_index]],
    }
    if failed and raise_on_fail:
        raise BusinessError(
            E_INCONSISTENT,
            "cross-method consistency self-check failed",
            {
                "categories": result["failed_categories"],
                "checks": failed,
                "snapshot_version": result["snapshot_version"],
            },
        )
    return result


def _reconstruct_encoded_matrix(
    snapshot: Mapping[str, Any], dataset: Dataset
) -> np.ndarray:
    standard = snapshot["sections"]["standardization"]["content"]
    precision = PrecisionSpec(
        mode=snapshot["precision"]["mode"],
        fixed_scale=int(snapshot["precision"]["fixed_scale"]),
        rounding=snapshot["precision"]["rounding"],
    )
    ops = PrecisionOps(precision)
    mean = decode_array(standard["numeric_mean"], "standardization.numeric_mean")
    scale = decode_array(standard["numeric_scale"], "standardization.numeric_scale")
    source = ops.cast(dataset.numeric)
    if bool(standard["standardize"]):
        if ops.mode == "fixed":
            fixed_scale = float(precision.fixed_scale)
            numeric = ops._round_values(
                (source.astype(np.int64) - mean.astype(np.int64)).astype(np.float64)
                * fixed_scale
                / scale.astype(np.float64)
            )
        else:
            numeric = ((source - mean) / scale).astype(ops.storage_dtype)
    else:
        numeric = source
    one_hot, _ = ops.one_hot(
        dataset.categorical,
        list(standard["categorical_columns"]),
        standard["category_levels"],
    )
    return ops.concatenate_features(numeric, one_hot)


class DiagnosticEngine:
    def __init__(self, workspace_root: str | Path, *, base_dir: str | Path | None = None):
        self.workspace_root = Path(workspace_root)
        self.base_dir = Path(base_dir) if base_dir is not None else self.workspace_root
        self.runtime_dir = self.workspace_root / "runtime"
        self.store = SnapshotStore(self.runtime_dir)
        self.error_log_path = self.store.cache_dir / "recent_errors.json"
        self.last_run_path = self.store.cache_dir / "last_run.json"

    def _load_errors(self) -> list[dict[str, Any]]:
        if not self.error_log_path.exists():
            return []
        import json

        try:
            with self.error_log_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            return list(value) if isinstance(value, list) else []
        except Exception:
            return []

    def _record_error(self, error: BusinessError) -> None:
        errors = self._load_errors()
        errors.append(
            {
                "code": error.code,
                "message": error.message,
                "details": json_safe(error.details),
            }
        )
        errors = errors[-20:]
        atomic_write_bytes(self.error_log_path, pretty_json_bytes(errors))

    def recent_errors(self) -> list[dict[str, Any]]:
        return self._load_errors()

    def clear_recent_errors(self) -> None:
        atomic_write_bytes(self.error_log_path, pretty_json_bytes([]))

    def _check_precision_switch(self, config: Mapping[str, Any]) -> None:
        active = self.store.load_active()
        if active is None:
            return
        current = precision_identity(config)
        previous = dict(active["precision"])
        if current == previous:
            return
        invalidated = self.store.invalidate_active(
            reason=E_PRECISION_CHANGED,
            requested_precision=current,
        )
        error = BusinessError(
            E_PRECISION_CHANGED,
            "precision change invalidated the active snapshot; retry the run",
            {
                "field": "precision",
                "previous_precision": previous,
                "requested_precision": current,
                "invalidated_snapshot": invalidated,
                "retry_required": True,
            },
        )
        self._record_error(error)
        raise error

    def run(
        self,
        config: Mapping[str, Any],
        *,
        base_dir: str | Path | None = None,
        commit: bool = True,
    ) -> RunOutcome:
        validated = validate_config(config)
        effective_base = Path(base_dir) if base_dir is not None else self.base_dir
        try:
            return self._run_inner(validated, effective_base, commit=commit)
        except BusinessError as error:
            if error.code != E_PRECISION_CHANGED:
                self._record_error(error)
            raise

    def _run_inner(
        self,
        config: dict[str, Any],
        base_dir: Path,
        *,
        commit: bool,
    ) -> RunOutcome:
        if commit:
            self._check_precision_switch(config)
        dataset = load_dataset(config, base_dir)
        precision = PrecisionSpec.from_config(config)
        resource_plan = plan_resources(config, dataset, precision)
        reference_precision = PrecisionSpec(mode="float64")
        reference = execute_core(
            dataset,
            config,
            method=resource_plan["effective_method"],
            precision=reference_precision,
            resource_plan=resource_plan,
        )
        observed = execute_core(
            dataset,
            config,
            method=resource_plan["effective_method"],
            precision=precision,
            resource_plan=resource_plan,
        )
        spectrum = compute_error_spectrum(observed, reference, config)
        enforce_error_tolerance(spectrum)
        decisions = threshold_trace(observed, config)

        snapshot = None
        selfcheck = None
        if commit:
            version = self.store.next_version()
            draft = self._build_snapshot(
                config=config,
                dataset=dataset,
                observed=observed,
                spectrum=spectrum,
                decisions=decisions,
                version=version,
            )
            draft = finalize_snapshot(draft)
            verify_snapshot(draft)
            selfcheck = run_self_check(
                draft,
                dataset,
                config,
                raise_on_fail=True,
            )
            draft["sections"]["selfcheck"] = make_section(selfcheck, version)
            draft = finalize_snapshot(draft)
            verify_snapshot(draft)
            snapshot = self.store.commit(draft)
            self.base_dir = base_dir

        response = self._build_response(
            dataset=dataset,
            observed=observed,
            reference=reference,
            spectrum=spectrum,
            decisions=decisions,
            snapshot=snapshot,
            selfcheck=selfcheck,
        )
        atomic_write_bytes(self.last_run_path, pretty_json_bytes(response))
        return RunOutcome(
            config=config,
            dataset=dataset,
            observed=observed,
            reference=reference,
            error_spectrum=spectrum,
            threshold_trace=decisions,
            snapshot=snapshot,
            selfcheck=selfcheck,
            response=response,
        )

    def _build_snapshot(
        self,
        *,
        config: Mapping[str, Any],
        dataset: Dataset,
        observed: ExecutionResult,
        spectrum: Mapping[str, Any],
        decisions: Mapping[str, Any],
        version: int,
    ) -> dict[str, Any]:
        precision = observed.precision.to_dict()
        standardization_content = {
            "standardize": bool(config["standardize"]),
            "numeric_columns": list(dataset.numeric_columns),
            "categorical_columns": list(dataset.categorical_columns),
            "category_levels": copy.deepcopy(dataset.category_levels),
            "feature_names": list(observed.feature_names),
            "storage_dtype": observed.precision.storage_dtype.str,
            "numeric_mean": _encoded_array(observed.numeric_mean),
            "numeric_scale": _encoded_array(observed.numeric_scale),
            "numeric_standardized": _encoded_array(observed.numeric_standardized),
            "encoded_center": _encoded_array(observed.encoded_center),
            "constant_features": [
                dataset.numeric_columns[index]
                for index, value in enumerate(observed.constant_features)
                if bool(value)
            ],
            "numeric_mean_checksum": array_checksum(observed.numeric_mean),
            "numeric_scale_checksum": array_checksum(observed.numeric_scale),
            "encoded_center_checksum": array_checksum(observed.encoded_center),
        }
        pca_content = None
        if observed.pca_components is not None:
            pca_content = {
                "components": _encoded_array(observed.pca_components),
                "eigenvalues": _encoded_array(observed.pca_eigenvalues),
                "variance_ratio": _encoded_array(
                    np.asarray(observed.pca_variance_ratio, dtype=np.float64)
                ),
                "stage_dim": int(observed.pca_stage_dim),
                "components_checksum": array_checksum(observed.pca_components),
            }
        model_content = {
            "requested_method": observed.requested_method,
            "effective_method": observed.effective_method,
            "requested_method_label": method_label(observed.requested_method),
            "effective_method_label": method_label(observed.effective_method),
            "target_dim": int(config["target_dim"]),
            "intermediate_dim": (
                int(config["two_stage"]["intermediate_dim"])
                if observed.effective_method == "pca_random_projection"
                else None
            ),
            "precision": precision,
            "standardize": bool(config["standardize"]),
            "pca": pca_content,
            "projection_stages": [
                _projection_stage_for_snapshot(stage)
                for stage in observed.projection_stages
            ],
            "feature_names": list(observed.feature_names),
            "fidelity_spectrum": copy.deepcopy(observed.fidelity_spectrum),
            "resource_plan": copy.deepcopy(observed.resource_plan),
            "degradation": copy.deepcopy(
                observed.resource_plan.get("degradation")
            ),
            "model_checksum": observed.model_checksum(),
        }
        coordinate_content: dict[str, Any] = {
            "values": _encoded_array(observed.coordinates),
            "row_ids": list(dataset.row_ids),
            "labels": [str(value) for value in dataset.labels],
            "method": observed.effective_method,
            "row_count": dataset.n_rows,
            "target_dim": int(observed.coordinates.shape[1]),
            "array_checksum": observed.coordinate_checksum(),
        }
        if observed.intermediate_coordinates is not None:
            coordinate_content["intermediate_values"] = _encoded_array(
                observed.intermediate_coordinates
            )
            coordinate_content["intermediate_dim"] = int(
                observed.intermediate_coordinates.shape[1]
            )
            coordinate_content["intermediate_array_checksum"] = array_checksum(
                observed.intermediate_coordinates
            )
        evaluation_content = {
            "class_consistency": float(observed.class_consistency),
            "variance_explained": observed.variance_explained,
            "fidelity_spectrum": copy.deepcopy(observed.fidelity_spectrum),
            "per_sample_class_consistency": _encoded_array(
                observed.per_sample_consistency
            ),
            "neighbor_indices": _encoded_array(observed.neighbor_indices),
            "k_neighbors": int(
                min(
                    int(config["evaluation"]["k_neighbors"]),
                    max(0, dataset.n_rows - 1),
                )
            ),
            "threshold_trace": copy.deepcopy(decisions),
            "coordinates_version": int(version),
            "coordinates_array_checksum": observed.coordinate_checksum(),
            "coordinate_method": observed.effective_method,
            "precision": precision,
        }
        config_content = copy.deepcopy(config)
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_version": int(version),
            "status": "active",
            "requested_method": observed.requested_method,
            "effective_method": observed.effective_method,
            "precision": precision,
            "input": {
                "checksum": dataset.checksum,
                "source": dataset.source,
                "rows": dataset.n_rows,
                "encoded_features": dataset.n_features,
            },
            "config_checksum": sha256_json(config_content),
            "degraded": bool(observed.resource_plan.get("degraded", False)),
            "resource_plan": copy.deepcopy(observed.resource_plan),
            "sections": {
                "config": make_section(config_content, version),
                "standardization": make_section(
                    standardization_content, version
                ),
                "model": make_section(model_content, version),
                "coordinates": make_section(coordinate_content, version),
                "evaluation": make_section(evaluation_content, version),
                "error_spectrum": make_section(
                    copy.deepcopy(dict(spectrum)), version
                ),
            },
        }
        return snapshot

    def _build_response(
        self,
        *,
        dataset: Dataset,
        observed: ExecutionResult,
        reference: ExecutionResult,
        spectrum: Mapping[str, Any],
        decisions: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None,
        selfcheck: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        snapshot_summary = None
        if snapshot is not None:
            snapshot_summary = {
                "version": int(snapshot["snapshot_version"]),
                "overall_checksum": snapshot["overall_checksum"],
                "precision": snapshot["precision"],
                "requested_method": snapshot["requested_method"],
                "effective_method": snapshot["effective_method"],
                "config_checksum": snapshot["config_checksum"],
                "input_checksum": snapshot["input"]["checksum"],
                "sections": {
                    name: {
                        "version": section["version"],
                        "checksum": section["checksum"],
                    }
                    for name, section in snapshot["sections"].items()
                },
            }
        return {
            "ok": True,
            "snapshot": snapshot_summary,
            "dataset": {
                "checksum": dataset.checksum,
                "rows": dataset.n_rows,
                "features": dataset.n_features,
                "source": dataset.source,
            },
            "method": {
                "requested": observed.requested_method,
                "effective": observed.effective_method,
                "requested_label": method_label(observed.requested_method),
                "effective_label": method_label(observed.effective_method),
                "degraded": bool(observed.resource_plan.get("degraded", False)),
                "degradation": observed.resource_plan.get("degradation"),
                "stage_parameters": [
                    {
                        "stage": stage["stage"],
                        "input_dim": stage["input_dim"],
                        "output_dim": stage["output_dim"],
                        "matrix_checksum": stage["matrix_checksum"],
                        "parameters": stage["parameters"],
                    }
                    for stage in observed.projection_stages
                ],
            },
            "precision": observed.precision.to_dict(),
            "metrics": {
                "class_consistency": observed.class_consistency,
                "variance_explained": observed.variance_explained,
                "fidelity_spectrum": observed.fidelity_spectrum,
            },
            "threshold_trace": decisions,
            "error_spectrum": spectrum,
            "reference_precision": "float64",
            "checksums": {
                "coordinates": observed.coordinate_checksum(),
                "model": observed.model_checksum(),
                "standardized_numeric": array_checksum(
                    observed.numeric_standardized
                ),
            },
            "resource_plan": observed.resource_plan,
            "selfcheck": selfcheck,
            "recent_errors": self.recent_errors(),
        }

    def inspect(self) -> dict[str, Any]:
        try:
            active = self.store.load_active()
        except BusinessError as error:
            self._record_error(error)
            raise
        errors = self.recent_errors()
        if active is None:
            return {
                "ok": True,
                "active": None,
                "recent_errors": errors,
                "store_status": self.store.status(),
            }
        sections = active["sections"]
        coordinate_content = sections["coordinates"]["content"]
        model_content = sections["model"]["content"]
        evaluation_content = sections["evaluation"]["content"]
        spectrum_content = sections["error_spectrum"]["content"]
        coordinates = decode_array(coordinate_content["values"], "coordinates.values")
        ops = PrecisionOps(
            PrecisionSpec(
                mode=active["precision"]["mode"],
                fixed_scale=int(active["precision"]["fixed_scale"]),
                rounding=active["precision"]["rounding"],
            )
        )
        display_coordinates = ops.to_float(coordinates, np.float64)
        return {
            "ok": True,
            "active": {
                "snapshot_version": int(active["snapshot_version"]),
                "overall_checksum": active["overall_checksum"],
                "precision": active["precision"],
                "requested_method": active["requested_method"],
                "effective_method": active["effective_method"],
                "input": active["input"],
                "degraded": active["degraded"],
                "resource_plan": active["resource_plan"],
                "sections": {
                    name: {
                        "version": section["version"],
                        "checksum": section["checksum"],
                    }
                    for name, section in sections.items()
                },
                "coordinates": display_coordinates[:, :3].tolist(),
                "coordinates_checksum": array_checksum(coordinates),
                "labels": coordinate_content["labels"],
                "row_ids": coordinate_content["row_ids"],
                "metrics": {
                    "class_consistency": evaluation_content["class_consistency"],
                    "variance_explained": evaluation_content[
                        "variance_explained"
                    ],
                    "fidelity_spectrum": evaluation_content["fidelity_spectrum"],
                },
                "threshold_trace": evaluation_content["threshold_trace"],
                "error_spectrum": spectrum_content,
                "model": {
                    "requested_method": model_content["requested_method"],
                    "effective_method": model_content["effective_method"],
                    "target_dim": model_content["target_dim"],
                    "intermediate_dim": model_content["intermediate_dim"],
                    "projection_stages": [
                        {
                            "stage": stage["stage"],
                            "input_dim": stage["input_dim"],
                            "output_dim": stage["output_dim"],
                            "matrix_checksum": stage["matrix_checksum"],
                            "parameters": stage["parameters"],
                        }
                        for stage in model_content["projection_stages"]
                    ],
                    "pca_stage_dim": (
                        model_content["pca"]["stage_dim"]
                        if model_content.get("pca")
                        else None
                    ),
                },
                "selfcheck": sections.get("selfcheck", {}).get("content"),
            },
            "recent_errors": errors,
            "store_status": self.store.status(),
        }

    def selfcheck_current(self) -> dict[str, Any]:
        active = self.store.load_active()
        if active is None:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "no active snapshot is available for self-check",
                {"field": "active_snapshot"},
            )
        config = validate_config(active["sections"]["config"]["content"])
        source = active.get("input", {}).get("source")
        if (
            config["dataset"]["kind"] == "csv"
            and isinstance(source, str)
            and Path(source).is_absolute()
            and Path(source).exists()
        ):
            config["dataset"]["path"] = source
        dataset = load_dataset(config, self.base_dir)
        try:
            check = run_self_check(
                active,
                dataset,
                config,
                raise_on_fail=True,
            )
        except BusinessError as error:
            self._record_error(error)
            raise
        return {
            "ok": True,
            "snapshot_version": int(active["snapshot_version"]),
            "selfcheck": check,
            "recent_errors": self.recent_errors(),
        }
