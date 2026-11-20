"""End-to-end orchestration: validate, quantize, calibrate, infer.

The pipeline is deliberately explicit about four properties:

* **determinism** - every value is derived from the fixed seed inputs and the
  manifest, never from the clock or dict order;
* **atomic progress** - a progress point is committed only once *all* layers of
  a stage finish, via temp-file + rename under a non-blocking lock;
* **resume / replacement** - uncommitted work is discarded on restart while
  unaffected layers keep their checksummed cache, and a higher revision always
  replaces (never merges with) the older cached products;
* **attribution** - injected faults become records carrying stage, code and
  layer and drive explicit downstream degradation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from . import samplegen
from .calibrate import calibration_stats, validate_calibration_shape
from .deterministic import (
    Timer,
    checksum_arrays,
    checksum_json,
    estimate_dequant_bytes,
)
from .errors import (
    E_FAULT,
    E_PACKAGE_FORMAT,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK,
    E_UNATTACHED,
    STAGES,
    PipelineError,
)
from .infer import forward_layer, stable_softmax
from .models import (
    Degradation,
    FaultRecord,
    LayerSpec,
    ModelPackage,
    ResourceBudget,
    RunContext,
)
from .quantize import quantize_layer, validate_weight_shape
from .storage import CacheStore, FileLock, ProgressStore, WorkDir


@dataclass
class StageResult:
    stage: str
    progress_version: int
    completed_layers: List[int]
    reused_layers: List[int]
    recomputed_layers: List[int]
    degraded: bool
    degradation_categories: List[str]
    timing_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "progress_version": self.progress_version,
            "completed_layers": self.completed_layers,
            "reused_layers": self.reused_layers,
            "recomputed_layers": self.recomputed_layers,
            "degraded": self.degraded,
            "degradation_categories": self.degradation_categories,
            "timing_ms": round(self.timing_ms, 6),
        }


@dataclass
class PipelineSummary:
    model_tag: str
    seed: int
    head_version: int
    stage_results: List[StageResult]
    quant_errors: List[Dict[str, Any]]
    activation_checksums: List[Dict[str, Any]]
    prediction_vector: List[float]
    prediction_checksum: str
    affected_layers: List[int]
    reused_layers: List[int]
    recomputed_layers: List[int]
    fault_records: List[FaultRecord] = field(default_factory=list)
    degradations: List[Degradation] = field(default_factory=list)
    unattributed: List[Dict[str, Any]] = field(default_factory=list)
    resource_observations: Dict[str, Any] = field(default_factory=dict)
    degraded_mode: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_tag": self.model_tag,
            "seed": self.seed,
            "head_version": self.head_version,
            "stage_results": [s.to_dict() for s in self.stage_results],
            "quant_errors": self.quant_errors,
            "activation_checksums": self.activation_checksums,
            "prediction_vector": self.prediction_vector,
            "prediction_checksum": self.prediction_checksum,
            "affected_layers": self.affected_layers,
            "reused_layers": self.reused_layers,
            "recomputed_layers": self.recomputed_layers,
            "fault_records": [f.to_dict() for f in self.fault_records],
            "degradations": [d.to_dict() for d in self.degradations],
            "unattributed": self.unattributed,
            "resource_observations": self.resource_observations,
            "degraded_mode": self.degraded_mode,
        }


class QuantPipeline:
    """Stateful facade owning the working directory and all stage logic."""

    def __init__(
        self,
        data_dir: str,
        work_dir: str,
        *,
        budget: Optional[ResourceBudget] = None,
        context: Optional[RunContext] = None,
        calibration_revision: int = 1,
    ) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.work = WorkDir(work_dir)
        self.store = ProgressStore(self.work)
        self.cache = CacheStore(self.work)
        self.budget = budget or ResourceBudget()
        self.context = context or RunContext()
        self.calibration_revision = int(calibration_revision)
        self.fault_records: List[FaultRecord] = []
        self.degradations: List[Degradation] = []
        self.unattributed: List[Dict[str, Any]] = []
        self._manifest_path = os.path.join(self.data_dir, "manifest.jsonl")
        self._weights_dir = os.path.join(self.data_dir, "weights")
        self._calibration_path = os.path.join(self.data_dir, "calibration.npy")

    # ------------------------------------------------------------------
    # Fault injection helpers (input/parameter driven only)
    # ------------------------------------------------------------------
    def _matching_fault(
        self, stage: str, layer_index: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        for fault in self.context.faults_for(stage):
            target = fault.get("layer_index")
            if target is None or target == layer_index:
                return fault
        return None

    def _record_fault(
        self,
        stage: str,
        code: str,
        layer_index: Optional[int],
        message: str,
        layer_id: Optional[str] = None,
    ) -> FaultRecord:
        record = FaultRecord(
            stage=stage,
            code=code,
            layer_index=layer_index,
            layer_id=layer_id,
            message=message,
        )
        self.fault_records.append(record)
        return record

    def _degrade(
        self,
        category: str,
        trigger_code: str,
        stage: str,
        layer_index: int,
        reason: str,
    ) -> None:
        self.degradations.append(
            Degradation(
                category=category,
                trigger_code=trigger_code,
                stage=stage,
                layer_index=layer_index,
                reason=reason,
            )
        )

    def _maybe_inject(
        self, stage: str, layer_index: Optional[int]
    ) -> None:
        fault = self._matching_fault(stage, layer_index)
        if fault is None:
            return
        kind = str(fault.get("kind", "generic"))
        message = str(
            fault.get("message") or f"injected {kind} at {stage}"
        )
        code = str(fault.get("code") or E_FAULT)
        layer_id = fault.get("layer_id")
        self._record_fault(stage, code, layer_index, message, layer_id)
        raise PipelineError(
            code,
            message,
            stage=stage,
            layer_index=layer_index,
            layer_id=layer_id,
            detail={"injected": True, "kind": kind},
        )

    def _kill_if_requested(self, stage: str, layer_index: int) -> None:
        """Hard-terminate this process without writing any exit report."""
        if (
            self.context.kill_stage == stage
            and self.context.kill_after_layer == layer_index
        ):
            os._exit(137)

    # ------------------------------------------------------------------
    # Budget helpers (estimation only, not real isolation)
    # ------------------------------------------------------------------
    def _resident_estimate(self, specs: Sequence[LayerSpec]) -> int:
        return sum(estimate_dequant_bytes(spec.layer_shape) for spec in specs)

    def _enforce_layer_budget(self, stage: str, count: int) -> None:
        if count > self.budget.max_layers_per_stage:
            raise PipelineError(
                E_RESOURCE_LIMIT,
                (
                    f"{stage} layer count {count} exceeds "
                    f"max_layers_per_stage {self.budget.max_layers_per_stage}"
                ),
                stage=stage,
                detail={"count": count},
            )

    def _enforce_cache_budget(self) -> None:
        if self.cache.count_entries() >= self.budget.max_cache_entries:
            raise PipelineError(
                E_RESOURCE_LIMIT,
                (
                    f"cache entries {self.cache.count_entries()} reached "
                    f"max_cache_entries {self.budget.max_cache_entries}"
                ),
            )

    # ------------------------------------------------------------------
    # Stage 0: package validation
    # ------------------------------------------------------------------
    def _load_package(self) -> ModelPackage:
        package = samplegen.load_manifest(self._manifest_path)
        for spec in package.layers:
            weight = samplegen.load_weight(self._weights_dir, spec)
            validate_weight_shape(spec, weight)
        return package

    # ------------------------------------------------------------------
    # Stage 1: quantize
    # ------------------------------------------------------------------
    def _quantize_one(
        self, spec: LayerSpec
    ) -> Tuple[Any, np.ndarray]:
        weight = samplegen.load_weight(self._weights_dir, spec)
        validate_weight_shape(spec, weight)
        self._maybe_inject("quantize", spec.layer_index)
        result = quantize_layer(spec, weight)
        return result, weight

    # ------------------------------------------------------------------
    # Cache validity / replacement
    # ------------------------------------------------------------------
    def _cache_is_current(
        self,
        stage: str,
        layer_index: int,
        spec: LayerSpec,
    ) -> bool:
        entry = self.cache.read_entry(stage, layer_index)
        if entry is None:
            return False
        payload = entry["payload"]
        if payload.get("weight_revision") != spec.weight_revision:
            return False
        if stage in ("calibrate", "infer"):
            if payload.get("calibration_revision") != self.calibration_revision:
                return False
        return True

    def _replace_layer_cache(
        self, stages: Sequence[str], layer_index: int
    ) -> None:
        """Wholesale replacement: delete the old revision's cached products."""
        for stage in stages:
            self.cache.remove_layer(stage, layer_index)

    # ------------------------------------------------------------------
    # Stage execution (each commits one point only after all layers)
    # ------------------------------------------------------------------
    def _stage_layers(
        self,
        package: ModelPackage,
        stage: str,
        affected: Set[int],
        lock: FileLock,
    ) -> StageResult:
        self._enforce_layer_budget(stage, package.layer_count)
        head = self.store.head()
        base_version = head["progress_version"] if head else 0
        next_version = base_version + 1
        all_indices = package.indices()
        reused: List[int] = []
        recomputed: List[int] = []
        categories: List[str] = []

        resident = self._resident_estimate(package.layers)
        if resident > self.budget.max_resident_dequant_bytes:
            raise PipelineError(
                E_RESOURCE_LIMIT,
                "resident dequant estimate over hard limit",
                stage=stage,
            )
        streaming = resident > self.budget.stream_dequant_bytes
        self._enforce_cache_budget()

        timer = Timer(stage)
        with timer:
            if stage == "validate":
                self._run_validate(
                    package,
                    affected,
                    recomputed,
                    reused,
                    categories,
                )
            elif stage == "quantize":
                self._run_quantize(
                    package, affected, recomputed, reused, categories, streaming
                )
            elif stage == "calibrate":
                self._run_calibrate(
                    package, affected, recomputed, reused, categories, streaming
                )
            elif stage == "infer":
                self._run_infer(
                    package, affected, recomputed, reused, categories, streaming
                )
            else:  # pragma: no cover - guarded by STAGES constants
                raise PipelineError(E_PACKAGE_FORMAT, f"unknown stage {stage}")

            completed = sorted(set(recomputed) | set(reused))
            content_checksum = checksum_json(
                {
                    "stage": stage,
                    "completed_layers": completed,
                    "recomputed_layers": sorted(recomputed),
                    "reused_layers": sorted(reused),
                    "calibration_revision": self.calibration_revision,
                    "weight_revisions": {
                        str(spec.layer_index): spec.weight_revision
                        for spec in package.layers
                    },
                }
            )
            point = {
                "progress_version": next_version,
                "stage": stage,
                "model_tag": package.model_tag,
                "seed": self.context.seed,
                "completed_layers": completed,
                "recomputed_layers": sorted(recomputed),
                "reused_layers": sorted(reused),
                "affected_layers": sorted(affected),
                "calibration_revision": self.calibration_revision,
                "weight_revisions": {
                    str(spec.layer_index): spec.weight_revision
                    for spec in package.layers
                },
                "degraded": bool(categories),
                "degradation_categories": sorted(set(categories)),
                "streaming": bool(streaming),
                "content_checksum": content_checksum,
            }
            with lock:
                self.store.commit(point)

        return StageResult(
            stage=stage,
            progress_version=next_version,
            completed_layers=completed,
            reused_layers=sorted(reused),
            recomputed_layers=sorted(recomputed),
            degraded=bool(categories),
            degradation_categories=sorted(set(categories)),
            timing_ms=timer.elapsed_ms,
        )

    def _run_validate(
        self,
        package: ModelPackage,
        affected: Set[int],
        recomputed: List[int],
        reused: List[int],
        categories: List[str],
    ) -> None:
        for spec in package.layers:
            if spec.layer_index not in affected and self._cache_is_current(
                "validate", spec.layer_index, spec
            ):
                reused.append(spec.layer_index)
                continue
            weight = samplegen.load_weight(self._weights_dir, spec)
            validate_weight_shape(spec, weight)
            valid = True
            try:
                self._maybe_inject("validate", spec.layer_index)
            except PipelineError as exc:
                valid = False
                self._handle_layer_fault(
                    "validate", spec, exc, recomputed, categories
                )
            self.cache.write_entry(
                "validate",
                spec.layer_index,
                {
                    "layer_id": spec.layer_id,
                    "weight_revision": spec.weight_revision,
                    "shape": list(spec.layer_shape),
                    "valid": valid,
                    "weight_checksum": self._weight_checksum(spec, weight),
                },
            )
            recomputed.append(spec.layer_index)
            self._kill_if_requested("validate", spec.layer_index)

    @staticmethod
    def _weight_checksum(spec: LayerSpec, weight: np.ndarray) -> str:
        from .deterministic import checksum_array

        return checksum_array(weight)

    def _run_quantize(
        self,
        package: ModelPackage,
        affected: Set[int],
        recomputed: List[int],
        reused: List[int],
        categories: List[str],
        streaming: bool,
    ) -> None:
        for spec in package.layers:
            if spec.layer_index not in affected and self._cache_is_current(
                "quantize", spec.layer_index, spec
            ):
                reused.append(spec.layer_index)
                continue
            try:
                result, _ = self._quantize_one(spec)
            except PipelineError as exc:
                self._handle_layer_fault(
                    "quantize", spec, exc, recomputed, categories
                )
                continue
            self.cache.write_entry(
                "quantize",
                spec.layer_index,
                result.params_dict(),
                arrays={
                    "quant": result.quant_values,
                    "dequant": result.dequant,
                },
            )
            recomputed.append(spec.layer_index)
            if streaming:
                categories.append("streaming_quantize")
            self._kill_if_requested("quantize", spec.layer_index)

    def _run_calibrate(
        self,
        package: ModelPackage,
        affected: Set[int],
        recomputed: List[int],
        reused: List[int],
        categories: List[str],
        streaming: bool,
    ) -> None:
        calibration = samplegen.load_calibration(self._calibration_path)
        current = calibration
        last_index = package.layers[-1].layer_index
        for spec in package.layers:
            if spec.layer_index not in affected and self._cache_is_current(
                "calibrate", spec.layer_index, spec
            ):
                current = self.cache.read_array(
                    "calibrate", spec.layer_index, "activation"
                )
                reused.append(spec.layer_index)
                continue
            dequant, upstream_fallback = self._dequant_or_fallback(
                spec, "calibrate", categories
            )
            try:
                validate_calibration_shape(spec, current)
                if not upstream_fallback:
                    self._maybe_inject("calibrate", spec.layer_index)
                logits = current.astype(np.float32) @ dequant
                activation = (
                    stable_softmax(logits.astype(np.float64)).astype(np.float32)
                    if spec.layer_index == last_index
                    else np.maximum(logits, np.float32(0.0))
                )
                stats = calibration_stats(
                    spec,
                    activation,
                    calibration_revision=self.calibration_revision,
                )
            except PipelineError as exc:
                activation = np.zeros_like(current @ dequant)
                self._handle_layer_fault(
                    "calibrate", spec, exc, recomputed, categories
                )
                stats = calibration_stats(
                    spec,
                    activation,
                    calibration_revision=self.calibration_revision,
                )
            self.cache.write_entry(
                "calibrate",
                spec.layer_index,
                {
                    **stats.to_dict(),
                    "weight_revision": spec.weight_revision,
                },
                arrays={"activation": np.ascontiguousarray(activation)},
            )
            current = activation
            recomputed.append(spec.layer_index)
            if streaming:
                categories.append("streaming_calibrate")
            self._kill_if_requested("calibrate", spec.layer_index)

    def _run_infer(
        self,
        package: ModelPackage,
        affected: Set[int],
        recomputed: List[int],
        reused: List[int],
        categories: List[str],
        streaming: bool,
    ) -> None:
        calibration = samplegen.load_calibration(self._calibration_path)
        current = calibration[0:1].copy()
        last_index = package.layers[-1].layer_index
        for spec in package.layers:
            if spec.layer_index not in affected and self._cache_is_current(
                "infer", spec.layer_index, spec
            ):
                current = self.cache.read_array(
                    "infer", spec.layer_index, "activation"
                )
                reused.append(spec.layer_index)
                continue
            dequant, upstream_fallback = self._dequant_or_fallback(
                spec, "infer", categories
            )
            try:
                activation = forward_layer(
                    spec,
                    current,
                    dequant,
                    is_last=(spec.layer_index == last_index),
                )
                if not upstream_fallback:
                    self._maybe_inject("infer", spec.layer_index)
            except PipelineError as exc:
                fallback = np.zeros(
                    (current.shape[0], spec.out_features), dtype=np.float32
                )
                self._handle_layer_fault(
                    "infer", spec, exc, recomputed, categories
                )
                from .infer import LayerActivation

                activation = LayerActivation(
                    layer_index=spec.layer_index,
                    layer_id=spec.layer_id,
                    weight_revision=spec.weight_revision,
                    input_checksum="",
                    output_checksum="",
                    output=fallback,
                )
            self.cache.write_entry(
                "infer",
                spec.layer_index,
                {
                    **activation.to_dict(),
                    "calibration_revision": self.calibration_revision,
                },
                arrays={"activation": activation.output},
            )
            current = activation.output
            recomputed.append(spec.layer_index)
            if streaming:
                categories.append("streaming_infer")
            self._kill_if_requested("infer", spec.layer_index)

    def _handle_layer_fault(
        self,
        stage: str,
        spec: LayerSpec,
        exc: PipelineError,
        recomputed: List[int],
        categories: List[str],
    ) -> None:
        if not any(
            record.stage == stage
            and record.layer_index == spec.layer_index
            for record in self.fault_records
        ):
            self._record_fault(
                stage,
                exc.code,
                spec.layer_index,
                exc.message,
                spec.layer_id,
            )
        category = f"degraded_{stage}"
        categories.append(category)
        self._degrade(
            category=category,
            trigger_code=exc.code,
            stage=stage,
            layer_index=spec.layer_index,
            reason=exc.message,
        )
        recomputed.append(spec.layer_index)

    def _upstream_fault_code(self, stage: str, layer_index: int) -> Optional[str]:
        """Find the business code that already degraded this layer upstream."""
        for record in reversed(self.fault_records):
            if record.layer_index == layer_index:
                return record.code
        return None

    def _dequant_or_fallback(
        self, spec: "LayerSpec", stage: str, categories: List[str]
    ) -> Tuple[np.ndarray, bool]:
        """Return de-quantised weights, or a zero matrix after upstream fault."""
        if self.cache.has_array("quantize", spec.layer_index, "dequant"):
            return (
                self.cache.read_array(
                    "quantize", spec.layer_index, "dequant"
                ),
                False,
            )
        trigger = self._upstream_fault_code(stage, spec.layer_index) or E_FAULT
        category = f"degraded_{stage}_upstream"
        categories.append(category)
        self._degrade(
            category=category,
            trigger_code=trigger,
            stage=stage,
            layer_index=spec.layer_index,
            reason="upstream quantize product missing; zero-weight fallback",
        )
        return (
            np.zeros(spec.layer_shape, dtype=np.float32),
            True,
        )

    # ------------------------------------------------------------------
    # Public orchestration
    # ------------------------------------------------------------------
    def run_all(
        self,
        *,
        affected: Optional[Set[int]] = None,
    ) -> PipelineSummary:
        """Run the four stages in order with per-stage atomic commits."""
        package = self._load_package()
        if affected is None:
            affected = set(package.indices())
        stage_results: List[StageResult] = []
        for stage in STAGES:
            lock = FileLock(self.work, f"stage_{stage}")
            stage_results.append(
                self._stage_layers(package, stage, set(affected), lock)
            )
        return self.build_summary(package, stage_results, sorted(affected))

    def resume(self) -> PipelineSummary:
        """Resume from the latest committed point.

        Only whole completed stages are visible: a stage interrupted before its
        commit has no point, so every layer of that stage is rebuilt while all
        earlier stages reuse their checksummed cache.
        """
        head = self.store.head()
        package = self._load_package()
        if head is None:
            return self.run_all()

        completed_stage = head["stage"]
        try:
            start_index = STAGES.index(completed_stage) + 1
        except ValueError:  # pragma: no cover - persisted stage is controlled
            start_index = 0

        stage_results: List[StageResult] = []
        for stage in STAGES:
            lock = FileLock(self.work, f"stage_{stage}")
            if STAGES.index(stage) < start_index:
                stage_results.append(
                    StageResult(
                        stage=stage,
                        progress_version=self._version_of(stage),
                        completed_layers=list(head["completed_layers"]),
                        reused_layers=list(head["completed_layers"]),
                        recomputed_layers=[],
                        degraded=head.get("degraded", False),
                        degradation_categories=list(
                            head.get("degradation_categories", [])
                        ),
                        timing_ms=0.0,
                    )
                )
                continue
            result = self._stage_layers(
                package, stage, set(package.indices()), lock
            )
            stage_results.append(result)
        return self.build_summary(package, stage_results, package.indices())

    def _version_of(self, stage: str) -> int:
        for record in self.store.history():
            if record["stage"] == stage:
                return int(record["progress_version"])
        head = self.store.head()
        return int(head["progress_version"]) if head else 0

    def apply_revision(
        self,
        new_package: ModelPackage,
        *,
        changed_layer: int,
        new_weights: Dict[int, np.ndarray],
        calibration_revision: Optional[int] = None,
    ) -> PipelineSummary:
        """Apply a higher weight revision (and/or calibration) then recompute.

        The changed layer and all downstream layers are invalidated; unaffected
        layers keep their cache.  Old cached products of affected layers are
        deleted wholesale before new ones are written.
        """
        head = self.store.head()
        if head is not None:
            self._guard_no_rollback(head, new_package)

        if calibration_revision is not None:
            if calibration_revision < self.calibration_revision:
                raise PipelineError(
                    E_SNAPSHOT_ROLLBACK,
                    (
                        f"calibration revision {calibration_revision} older "
                        f"than {self.calibration_revision}"
                    ),
                )
            self.calibration_revision = calibration_revision

        affected = set(new_package.downstream_of(changed_layer))
        for layer_index in affected:
            self._replace_layer_cache(STAGES, layer_index)

        self._persist_new_weights(new_package, new_weights)
        return self.run_all(affected=affected)

    def apply_calibration_update(
        self,
        new_package: ModelPackage,
        *,
        new_calibration: np.ndarray,
        calibration_revision: int,
    ) -> PipelineSummary:
        """A calibration update invalidates every layer (full fan-out)."""
        head = self.store.head()
        if head is not None:
            self._guard_no_rollback(head, new_package)
        if calibration_revision <= self.calibration_revision:
            raise PipelineError(
                E_SNAPSHOT_ROLLBACK,
                (
                    f"calibration revision {calibration_revision} must exceed "
                    f"{self.calibration_revision}"
                ),
            )
        self.calibration_revision = calibration_revision
        tmp = self._calibration_path + ".tmp"
        with open(tmp, "wb") as handle:
            np.save(handle, np.ascontiguousarray(new_calibration), allow_pickle=False)
        os.replace(tmp, self._calibration_path)
        for layer_index in new_package.indices():
            self._replace_layer_cache(("calibrate", "infer"), layer_index)
        return self.run_all(affected=set(new_package.indices()))

    def _guard_no_rollback(
        self, head: Dict[str, Any], new_package: ModelPackage
    ) -> None:
        revisions = head.get("weight_revisions", {})
        for spec in new_package.layers:
            previous = revisions.get(str(spec.layer_index))
            if previous is not None and spec.weight_revision < int(previous):
                raise PipelineError(
                    E_SNAPSHOT_ROLLBACK,
                    (
                        f"layer {spec.layer_index} revision "
                        f"{spec.weight_revision} older than committed "
                        f"{previous}"
                    ),
                    layer_index=spec.layer_index,
                    layer_id=spec.layer_id,
                )

    def _persist_new_weights(
        self,
        package: ModelPackage,
        new_weights: Dict[int, np.ndarray],
    ) -> None:
        manifest_rows = [spec.to_manifest_dict() for spec in package.layers]
        from .samplegen import _write_jsonl, weight_filename

        _write_jsonl(self._manifest_path, manifest_rows)
        os.makedirs(self._weights_dir, exist_ok=True)
        for layer_index, weight in new_weights.items():
            spec = next(
                s for s in package.layers if s.layer_index == layer_index
            )
            path = os.path.join(
                self._weights_dir, weight_filename(spec)
            )
            tmp = path + ".tmp"
            with open(tmp, "wb") as handle:
                np.save(
                    handle,
                    np.ascontiguousarray(weight, dtype=np.float32),
                    allow_pickle=False,
                )
            os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Summary + resource observation
    # ------------------------------------------------------------------
    def build_summary(
        self,
        package: ModelPackage,
        stage_results: List[StageResult],
        affected: List[int],
    ) -> PipelineSummary:
        quant_errors: List[Dict[str, Any]] = []
        activation_checksums: List[Dict[str, Any]] = []
        dequant_arrays: List[np.ndarray] = []
        reused_total: Set[int] = set()
        recomputed_total: Set[int] = set()

        for spec in package.layers:
            entry = self.cache.read_entry("quantize", spec.layer_index)
            if entry is not None:
                payload = entry["payload"]
                quant_errors.append(
                    {
                        "layer_id": payload["layer_id"],
                        "layer_index": payload["layer_index"],
                        "weight_revision": payload["weight_revision"],
                        "max_abs_error": payload["max_abs_error"],
                        "mean_abs_error": payload["mean_abs_error"],
                        "rmse": payload["rmse"],
                        "snr_db": payload["snr_db"],
                        "dequant_checksum": payload["dequant_checksum"],
                    }
                )
                dequant_arrays.append(
                    self.cache.read_array(
                        "quantize", spec.layer_index, "dequant"
                    )
                )
            infer_entry = self.cache.read_entry("infer", spec.layer_index)
            if infer_entry is not None:
                activation_checksums.append(
                    {
                        "layer_index": spec.layer_index,
                        "layer_id": spec.layer_id,
                        "output_checksum": infer_entry["payload"][
                            "output_checksum"
                        ],
                    }
                )

        for result in stage_results:
            reused_total.update(result.reused_layers)
            recomputed_total.update(result.recomputed_layers)

        last_index = package.layers[-1].layer_index
        prediction = self.cache.read_array(
            "infer", last_index, "activation"
        )[0]
        prediction_vector = [float(v) for v in prediction.tolist()]
        head = self.store.head()
        observations = self._resource_observations(package, stage_results)

        return PipelineSummary(
            model_tag=package.model_tag,
            seed=self.context.seed,
            head_version=int(head["progress_version"]) if head else 0,
            stage_results=stage_results,
            quant_errors=sorted(quant_errors, key=lambda r: r["layer_index"]),
            activation_checksums=sorted(
                activation_checksums, key=lambda r: r["layer_index"]
            ),
            prediction_vector=prediction_vector,
            prediction_checksum=checksum_arrays(
                [np.ascontiguousarray(prediction, dtype=np.float32)]
            ),
            affected_layers=sorted(affected),
            reused_layers=sorted(reused_total),
            recomputed_layers=sorted(recomputed_total),
            fault_records=list(self.fault_records),
            degradations=list(self.degradations),
            unattributed=list(self.unattributed),
            resource_observations=observations,
            degraded_mode=any(r.degraded for r in stage_results),
        )

    def _resource_observations(
        self,
        package: ModelPackage,
        stage_results: List[StageResult],
    ) -> Dict[str, Any]:
        per_layer = [
            {
                "layer_index": spec.layer_index,
                "estimated_dequant_bytes": estimate_dequant_bytes(
                    spec.layer_shape
                ),
            }
            for spec in package.layers
        ]
        total_dequant = sum(
            item["estimated_dequant_bytes"] for item in per_layer
        )
        return {
            "budget": self.budget.to_dict(),
            "estimated_total_dequant_bytes": total_dequant,
            "estimated_cache_entries": self.cache.count_entries(),
            "per_layer_dequant_bytes": per_layer,
            "stage_timings_ms": [
                result.to_dict()["timing_ms"] for result in stage_results
            ],
            "note": (
                "timing and memory are local observations/estimates only; "
                "they do not enforce real resource isolation"
            ),
        }

    def record_unattributed(self, exc: BaseException) -> None:
        """Fallback sink for exceptions not attributed to any stage."""
        self.unattributed.append(
            {
                "code": E_UNATTACHED,
                "type": type(exc).__name__,
                "message": str(exc),
            }
        )
