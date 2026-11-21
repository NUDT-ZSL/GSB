from __future__ import annotations

import json
import os
import signal
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .budgets import (
    BudgetLedger,
    check_layer_count,
    check_quant_macs,
    choose_quant_group_cap,
    plan_inference,
    simple_ledger,
)
from .bundle import (
    LoadedBundle,
    ModelLayer,
    bundle_summary,
    generate_bias,
    generate_calibration,
    load_bundle,
)
from .config import LabConfig
from .errors import (
    DeployError,
    E_CALIB_SAMPLE_MISMATCH,
    E_CALIBRATION_TRUNCATED,
    E_INFER_DIM_MISMATCH,
    E_MANIFEST_CHANGED_MID_RUN,
    E_PACKAGE_MANIFEST_TRUNCATED,
    E_SNAPSHOT_ROLLBACK,
    E_UNATTRIBUTED,
    STAGES,
    STAGE_LABELS,
)
from .faults import FaultInjector
from .quant import quantize_layer
from .store import SnapshotStore
from .util import (
    array_checksum,
    array_digest,
    canonical_json,
    decode_array,
    digest_bytes,
    digest_json,
    encode_array,
    float_hex,
    stable_int,
)


def _manifest_payload(bundle: LoadedBundle) -> dict[str, Any]:
    return {
        "model_tag": bundle.model_tag,
        "manifest_digest": bundle.manifest_digest,
        "bundle_root": str(bundle.root.resolve()),
        "layers": [
            {**layer.to_dict(), "layer_shape": list(layer.layer_shape)}
            for layer in bundle.layers
        ],
    }


def _field_signature(layer: ModelLayer) -> dict[str, Any]:
    result = layer.to_dict()
    result["layer_shape"] = list(layer.layer_shape)
    return result


def _normalize_calibration_updates(
    bundle: LoadedBundle,
    updates: dict[str, Any] | None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, dict[str, int]]:
    base_revisions = bundle.calibration_revisions
    revisions: dict[str, int] = {}
    seeds: dict[str, int] = {}
    baseline_revisions = (baseline or {}).get("revisions", {})
    baseline_seeds = (baseline or {}).get("seeds", {})
    for layer in bundle.layers:
        bundle_revision = int(base_revisions.get(layer.layer_id, 0))
        previous_revision = int(baseline_revisions.get(layer.layer_id, 0))
        revisions[layer.layer_id] = max(bundle_revision, previous_revision)
        if previous_revision > bundle_revision:
            seeds[layer.layer_id] = int(
                baseline_seeds.get(layer.layer_id, layer.calib_seed)
            )
        else:
            seeds[layer.layer_id] = int(layer.calib_seed)
    for layer_id, raw in (updates or {}).items():
        if layer_id not in seeds:
            raise DeployError(
                E_MANIFEST_CHANGED_MID_RUN,
                "calibration",
                f"calibration update targets unknown layer {layer_id}",
                layer_id=layer_id,
            )
        if isinstance(raw, int) and not isinstance(raw, bool):
            revision = int(raw)
            seed_value = None
        elif isinstance(raw, dict):
            revision = int(raw.get("revision", revisions[layer_id]))
            seed_value = raw.get("seed")
        else:
            raise ValueError(f"invalid calibration update for {layer_id}: {raw!r}")
        revisions[layer_id] = revision
        if seed_value is not None:
            seeds[layer_id] = int(seed_value)
        elif revision != int(base_revisions.get(layer_id, 0)):
            seeds[layer_id] = stable_int(
                bundle.model_tag,
                layer_id,
                int(seeds[layer_id]),
                revision,
                "calibration-update-seed-v1",
                bits=63,
            )
    return {"revisions": revisions, "seeds": seeds}


def _calibration_config_json(config: dict[str, dict[str, int]]) -> str:
    return canonical_json(config)


def _generation_plan(
    *,
    bundle: LoadedBundle,
    latest: dict[str, Any] | None,
    calibration_config: dict[str, dict[str, int]],
    force_new: bool,
) -> tuple[int, dict[str, Any], bool, bool]:
    manifest = _manifest_payload(bundle)
    if latest is None:
        affected = [layer.layer_id for layer in bundle.layers]
        plan = {
            "trigger": "initial",
            "source_generation": None,
            "changed_layers": affected,
            "affected_layers": affected,
            "reused_layers": [],
            "recomputed_layers": affected,
            "full_layers": affected,
        }
        return 1, plan, False, True
    if not latest["completed"]:
        if latest["manifest_digest"] != manifest["manifest_digest"]:
            raise DeployError(
                E_MANIFEST_CHANGED_MID_RUN,
                "model_validation",
                "manifest differs from the incomplete committed generation",
                details={
                    "latest_generation": latest["generation"],
                    "committed_manifest_digest": latest["manifest_digest"],
                    "requested_manifest_digest": manifest["manifest_digest"],
                },
            )
        if _calibration_config_json(
            latest["calibration_config"]
        ) != _calibration_config_json(calibration_config):
            raise DeployError(
                E_MANIFEST_CHANGED_MID_RUN,
                "model_validation",
                "calibration configuration differs while a generation is incomplete",
            )
        return latest["generation"], latest["invalidation"], True, True

    if (
        latest["manifest_digest"] == manifest["manifest_digest"]
        and _calibration_config_json(latest["calibration_config"])
        == _calibration_config_json(calibration_config)
        and not force_new
    ):
        return latest["generation"], latest["invalidation"], False, False

    old_layers = {
        layer["layer_id"]: layer for layer in latest["manifest"]["layers"]
    }
    old_calib = latest["calibration_config"]
    changed: list[str] = []
    for layer in bundle.layers:
        old = old_layers[layer.layer_id]
        old_revision = int(old["weight_revision"])
        new_revision = int(layer.weight_revision)
        if new_revision < old_revision:
            raise DeployError(
                E_SNAPSHOT_ROLLBACK,
                "model_validation",
                f"weight_revision rollback for {layer.layer_id}: "
                f"{old_revision} -> {new_revision}",
                layer_id=layer.layer_id,
                field="weight_revision",
            )
        old_calib_rev = int(old_calib["revisions"].get(layer.layer_id, 0))
        new_calib_rev = int(calibration_config["revisions"].get(layer.layer_id, 0))
        if new_calib_rev < old_calib_rev:
            raise DeployError(
                E_SNAPSHOT_ROLLBACK,
                "model_validation",
                f"calibration revision rollback for {layer.layer_id}: "
                f"{old_calib_rev} -> {new_calib_rev}",
                layer_id=layer.layer_id,
                field="calibration_revision",
            )
        old_signature = dict(old)
        new_signature = _field_signature(layer)
        old_signature.pop("weight_revision", None)
        new_signature.pop("weight_revision", None)
        calib_seed_changed = int(old_calib["seeds"].get(layer.layer_id, layer.calib_seed)) != int(
            calibration_config["seeds"].get(layer.layer_id, layer.calib_seed)
        )
        if (
            new_revision != old_revision
            or old_signature != new_signature
            or new_calib_rev != old_calib_rev
            or calib_seed_changed
        ):
            changed.append(layer.layer_id)
    if not changed and not force_new:
        return latest["generation"], latest["invalidation"], False, False
    if force_new and not changed:
        changed = [layer.layer_id for layer in bundle.layers]
    indexes = {layer.layer_id: layer.layer_index for layer in bundle.layers}
    first_index = min(indexes[layer_id] for layer_id in changed)
    affected = [
        layer.layer_id for layer in bundle.layers if layer.layer_index >= first_index
    ]
    reused = [
        layer.layer_id for layer in bundle.layers if layer.layer_index < first_index
    ]
    plan = {
        "trigger": "forced_rebuild" if force_new else "revision_or_calibration_change",
        "source_generation": latest["generation"],
        "changed_layers": changed,
        "affected_layers": affected,
        "reused_layers": reused,
        "recomputed_layers": affected,
        "full_layers": [layer.layer_id for layer in bundle.layers],
        "invariant": sorted(set(reused).intersection(affected)) == []
        and sorted(set(reused).union(affected))
        == sorted(layer.layer_id for layer in bundle.layers),
    }
    return latest["generation"] + 1, plan, False, True


def deterministic_linear(
    weight: np.ndarray,
    activation: np.ndarray,
    bias: np.ndarray,
    *,
    tile_rows: int | None = None,
) -> np.ndarray:
    if weight.ndim != 2 or activation.ndim != 1 or bias.ndim != 1:
        raise ValueError("linear requires 2-D weight and 1-D activation/bias")
    out_features, in_features = weight.shape
    if activation.shape[0] != in_features or bias.shape[0] != out_features:
        raise ValueError("linear dimension mismatch")
    rows = int(tile_rows or out_features)
    rows = max(1, min(rows, out_features))
    output = np.empty(out_features, dtype="<f8")
    for start in range(0, out_features, rows):
        end = min(start + rows, out_features)
        tile = np.asarray(weight[start:end, :], dtype="<f8")
        output[start:end] = (
            np.sum(tile * activation[None, :], axis=1, dtype="<f8")
            + np.asarray(bias[start:end], dtype="<f8")
        )
    return output


class Pipeline:
    def __init__(
        self,
        bundle_path: str | Path,
        db_path: str | Path,
        *,
        config: LabConfig | None = None,
        faults: FaultInjector | None = None,
        calibration_updates: dict[str, Any] | None = None,
        force_new: bool = False,
    ) -> None:
        self.bundle_path = Path(bundle_path)
        self.db_path = Path(db_path)
        self.config = config or LabConfig()
        self.config.validate()
        marker = str(self.db_path) + ".crash_marker.json"
        self.faults = faults or FaultInjector.build(crash_marker_path=marker)
        if self.faults.crash_marker_path is None:
            self.faults.crash_marker_path = marker
        self.calibration_updates = calibration_updates
        self.force_new = force_new
        self.store = SnapshotStore(
            self.db_path,
            busy_timeout_ms=self.config.sqlite_busy_timeout_ms,
            error_busy_timeout_ms=self.config.error_busy_timeout_ms,
        )
        self._run_stage: str = STAGES[0]
        self._run_generation: int | None = None

    def _load_bundle(self) -> LoadedBundle:
        if self.faults.hit("model_validation", "truncate_manifest", None):
            raise DeployError(
                E_PACKAGE_MANIFEST_TRUNCATED,
                "model_validation",
                "manifest read was truncated by the configured fault injection",
                field="manifest",
                injected=True,
            )
        return load_bundle(self.bundle_path)

    def _record_failure(self, error: DeployError, *, attributed: bool = True) -> None:
        try:
            self.store.record_error(error, attributed=attributed)
        except Exception:
            # The primary failure must not be masked by an audit-write failure.
            pass

    def run(self) -> dict[str, Any]:
        try:
            return self._run()
        except DeployError as error:
            self._record_failure(error, attributed=True)
            raise
        except Exception as exc:
            wrapped = DeployError(
                E_UNATTRIBUTED,
                self._run_stage,
                f"unattributed {type(exc).__name__}: {exc}",
                details={"exception_type": type(exc).__name__},
            )
            self._record_failure(wrapped, attributed=False)
            raise wrapped from exc

    def _run(self) -> dict[str, Any]:
        bundle = self._load_bundle()
        latest = self.store.latest_generation()
        calibration_config = _normalize_calibration_updates(
            bundle,
            self.calibration_updates,
            baseline=latest["calibration_config"] if latest else None,
        )
        manifest = _manifest_payload(bundle)
        generation, invalidation, resumed_existing, should_run = _generation_plan(
            bundle=bundle,
            latest=latest,
            calibration_config=calibration_config,
            force_new=self.force_new,
        )
        self._run_generation = generation
        if not should_run:
            return self._result_from_store(
                bundle=bundle,
                generation=generation,
                already_complete=True,
                recovery_override=None,
            )

        existing_snapshots = self.store.generation_snapshots(generation)
        committed_by_stage = {
            snapshot["stage"]: snapshot["completed_layers"]
            for snapshot in existing_snapshots
        }
        last_snapshot = existing_snapshots[-1] if existing_snapshots else None
        crash_marker = self._read_crash_marker(generation)
        recovery = {
            "resumed_from_generation": generation,
            "resumed_from_version": (
                last_snapshot["version"] if last_snapshot is not None else 0
            ),
            "resumed_from_stage": (
                last_snapshot["stage"] if last_snapshot is not None else None
            ),
            "existing_generation_resumed": resumed_existing
            and bool(existing_snapshots),
            "committed_layers_by_stage": committed_by_stage,
            "rebuilt_layers_by_stage": {},
            "reused_layers_by_stage": {},
            "discarded_uncommitted": crash_marker,
            "only_committed_snapshots_used": True,
            "invalidation": invalidation,
        }
        forced_layers = set(invalidation["affected_layers"])
        for stage in STAGES:
            existing = next(
                (item for item in existing_snapshots if item["stage"] == stage), None
            )
            if existing is not None:
                continue
            self._run_stage = stage
            if stage == "model_validation":
                rebuilt, reused = self._stage_validation(
                    bundle,
                    generation,
                    manifest,
                    calibration_config,
                    invalidation,
                    forced_layers,
                    recovery,
                )
            elif stage == "quantization":
                rebuilt, reused = self._stage_quantization(
                    bundle,
                    generation,
                    manifest,
                    calibration_config,
                    invalidation,
                    forced_layers,
                    recovery,
                )
            elif stage == "calibration":
                rebuilt, reused = self._stage_calibration(
                    bundle,
                    generation,
                    manifest,
                    calibration_config,
                    invalidation,
                    forced_layers,
                    recovery,
                )
            elif stage == "inference":
                rebuilt, reused = self._stage_inference(
                    bundle,
                    generation,
                    manifest,
                    calibration_config,
                    invalidation,
                    forced_layers,
                    recovery,
                )
            else:
                raise AssertionError(stage)
            recovery["rebuilt_layers_by_stage"][stage] = rebuilt
            recovery["reused_layers_by_stage"][stage] = reused
        self._run_stage = "inference"
        return self._result_from_store(
            bundle=bundle,
            generation=generation,
            already_complete=False,
            recovery_override=recovery,
        )

    def _read_crash_marker(self, generation: int) -> list[str]:
        path = self.faults.crash_marker_path
        if not path or not Path(path).exists():
            return []
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if int(data.get("generation", -1)) != generation:
            return []
        if not data.get("uncommitted"):
            return []
        return [str(value) for value in data.get("uncommitted_layers", [])]

    def _maybe_crash(
        self,
        stage: str,
        processed_layers: list[str],
        generation: int,
    ) -> None:
        if (
            self.faults.crash_stage != stage
            or self.faults.crash_after_layers <= 0
            or len(processed_layers) < self.faults.crash_after_layers
        ):
            return
        marker = {
            "generation": generation,
            "stage": stage,
            "uncommitted_layers": list(processed_layers),
            "uncommitted": True,
            "committed": False,
            "note": "diagnostic marker only; recovery never reads it as progress",
        }
        path = Path(self.faults.crash_marker_path or (str(self.db_path) + ".crash_marker.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write(canonical_json(marker) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.kill(os.getpid(), signal.SIGKILL)

    @staticmethod
    def _cache_key(base: dict[str, Any], *, force: bool, generation: int, stage: str, layer_id: str) -> str:
        payload = dict(base)
        # Generation 1 is the repository baseline. Those artifacts are stored
        # under their content key so later generations can reuse unaffected
        # upstream layers. Forced recomputation in later generations needs a
        # nonce to avoid linking an older artifact with the same content key.
        if force and generation > 1:
            payload["rerun_generation"] = generation
            payload["rerun_stage"] = stage
            payload["rerun_layer"] = layer_id
        return digest_json(payload)

    def _cached_or_none(
        self,
        *,
        stage: str,
        cache_key: str,
        force: bool,
    ) -> dict[str, Any] | None:
        if force:
            return None
        return self.store.find_cached_artifact(stage, cache_key)

    def _artifact(
        self,
        *,
        layer_id: str,
        cache_key: str,
        payload: dict[str, Any] | None = None,
        payload_text: str | None = None,
        disposition: str,
    ) -> dict[str, Any]:
        if payload_text is None:
            if payload is None:
                raise ValueError("artifact payload required")
            payload_text = canonical_json(payload)
        return {
            "layer_id": layer_id,
            "cache_key": cache_key,
            "payload": payload_text,
            "disposition": disposition,
            "digest": digest_bytes(payload_text.encode("utf-8")),
        }

    def _commit(
        self,
        *,
        generation: int,
        stage: str,
        artifacts: list[dict[str, Any]],
        metadata: dict[str, Any],
        bundle: LoadedBundle,
        manifest: dict[str, Any],
        calibration_config: dict[str, dict[str, int]],
        invalidation: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = dict(metadata)
        metadata.setdefault("stage_label", STAGE_LABELS[stage])
        metadata.setdefault("model_tag", bundle.model_tag)
        return self.store.commit_stage(
            generation=generation,
            stage=stage,
            completed_layers=bundle.layer_ids,
            artifacts=artifacts,
            metadata=metadata,
            manifest=manifest,
            calibration_config=calibration_config,
            invalidation=invalidation,
        )

    def _stage_validation(
        self,
        bundle: LoadedBundle,
        generation: int,
        manifest: dict[str, Any],
        calibration_config: dict[str, dict[str, int]],
        invalidation: dict[str, Any],
        forced_layers: set[str],
        recovery: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        stage = "model_validation"
        check_layer_count(stage, len(bundle.layers), self.config.budget)
        ledger = simple_ledger(stage, bundle, self.config.budget)
        artifacts: list[dict[str, Any]] = []
        rebuilt: list[str] = []
        reused: list[str] = []
        processed: list[str] = []
        for layer in bundle.layers:
            truncate = self.faults.hit(stage, "truncate_weight", layer.layer_id)
            try:
                weight, weight_file_digest = bundle.load_weight(
                    layer, truncate=truncate
                )
            except DeployError as error:
                if truncate:
                    error.injected = True
                raise
            calibration_path = bundle.calibration_path(layer)
            if not calibration_path.exists():
                raise DeployError(
                    "E_PACKAGE_CALIBRATION_MISSING",
                    stage,
                    f"calibration file does not exist: {calibration_path}",
                    layer_id=layer.layer_id,
                    field="calibration",
                )
            calib_file_digest = digest_bytes(calibration_path.read_bytes())
            force = layer.layer_id in forced_layers or truncate
            base = {
                "kind": "validation",
                "layer": _field_signature(layer),
                "weight_file_digest": weight_file_digest,
                "calibration_file_digest": calib_file_digest,
            }
            cache_key = self._cache_key(
                base,
                force=force,
                generation=generation,
                stage=stage,
                layer_id=layer.layer_id,
            )
            cached = self._cached_or_none(stage=stage, cache_key=cache_key, force=force)
            if cached is not None:
                artifacts.append(
                    self._artifact(
                        layer_id=layer.layer_id,
                        cache_key=cache_key,
                        payload_text=cached["payload_text"],
                        disposition="reused",
                    )
                )
                reused.append(layer.layer_id)
            else:
                payload = {
                    "schema": 1,
                    "layer_id": layer.layer_id,
                    "layer_index": layer.layer_index,
                    "weight_revision": layer.weight_revision,
                    "weight_file_digest": weight_file_digest,
                    "weight_array_digest": array_digest(weight),
                    "calibration_file_digest": calib_file_digest,
                    "validated": True,
                }
                artifacts.append(
                    self._artifact(
                        layer_id=layer.layer_id,
                        cache_key=cache_key,
                        payload=payload,
                        disposition="recomputed",
                    )
                )
                rebuilt.append(layer.layer_id)
            processed.append(layer.layer_id)
            self._maybe_crash(stage, processed, generation)
        self._commit(
            generation=generation,
            stage=stage,
            artifacts=artifacts,
            metadata={"budget": ledger.to_dict()},
            bundle=bundle,
            manifest=manifest,
            calibration_config=calibration_config,
            invalidation=invalidation,
        )
        return rebuilt, reused

    def _stage_quantization(
        self,
        bundle: LoadedBundle,
        generation: int,
        manifest: dict[str, Any],
        calibration_config: dict[str, dict[str, int]],
        invalidation: dict[str, Any],
        forced_layers: set[str],
        recovery: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        stage = "quantization"
        check_layer_count(stage, len(bundle.layers), self.config.budget)
        check_quant_macs(bundle, self.config.budget)
        group_cap, ledger = choose_quant_group_cap(bundle, self.config.budget)
        artifacts: list[dict[str, Any]] = []
        rebuilt: list[str] = []
        reused: list[str] = []
        processed: list[str] = []
        for layer in bundle.layers:
            weight, weight_file_digest = bundle.load_weight(layer)
            bias = generate_bias(layer)
            dtype_injected = self.faults.hit(
                stage, "unknown_quant_dtype", layer.layer_id
            )
            hint_injected = self.faults.hit(
                stage, "invalid_scale_hint", layer.layer_id
            )
            zp_injected = self.faults.hit(
                stage, "invalid_zero_point", layer.layer_id
            )
            injected = dtype_injected or hint_injected or zp_injected
            force = layer.layer_id in forced_layers or injected
            base = {
                "kind": "quantization",
                "layer": _field_signature(layer),
                "weight_file_digest": weight_file_digest,
                "group_size_cap": group_cap,
            }
            cache_key = self._cache_key(
                base,
                force=force,
                generation=generation,
                stage=stage,
                layer_id=layer.layer_id,
            )
            cached = self._cached_or_none(stage=stage, cache_key=cache_key, force=force)
            if cached is not None:
                artifacts.append(
                    self._artifact(
                        layer_id=layer.layer_id,
                        cache_key=cache_key,
                        payload_text=cached["payload_text"],
                        disposition="reused",
                    )
                )
                reused.append(layer.layer_id)
            else:
                effective_layer = layer
                dtype_override = None
                if dtype_injected:
                    dtype_override = "int3"
                if hint_injected:
                    effective_layer = replace(effective_layer, scale_hint="per_block")
                if zp_injected:
                    effective_layer = replace(effective_layer, zero_point=999)
                try:
                    payload = quantize_layer(
                        effective_layer,
                        weight,
                        bias,
                        group_size_cap=group_cap,
                        quant_dtype_override=dtype_override,
                    )
                except DeployError as error:
                    if injected:
                        error.injected = True
                    raise
                payload["layer_id"] = layer.layer_id
                payload["layer_index"] = layer.layer_index
                payload["weight_revision"] = layer.weight_revision
                payload["weight_file_digest"] = weight_file_digest
                artifacts.append(
                    self._artifact(
                        layer_id=layer.layer_id,
                        cache_key=cache_key,
                        payload=payload,
                        disposition="recomputed",
                    )
                )
                rebuilt.append(layer.layer_id)
            processed.append(layer.layer_id)
            self._maybe_crash(stage, processed, generation)
        self._commit(
            generation=generation,
            stage=stage,
            artifacts=artifacts,
            metadata={
                "budget": ledger.to_dict(),
                "group_size_cap": group_cap,
                "scale_hint_effective": {
                    layer.layer_id: (
                        f"{layer.scale_hint.split(':', 1)[0]}:group="
                        f"{min(int(layer.scale_hint.split('group=', 1)[1]), group_cap)}"
                        if group_cap is not None and "group=" in layer.scale_hint
                        else layer.scale_hint
                    )
                    for layer in bundle.layers
                },
            },
            bundle=bundle,
            manifest=manifest,
            calibration_config=calibration_config,
            invalidation=invalidation,
        )
        return rebuilt, reused

    def _calibration_array(
        self,
        bundle: LoadedBundle,
        layer: ModelLayer,
        calibration_config: dict[str, dict[str, int]],
        *,
        inject_mismatch: bool,
        inject_truncated: bool,
    ) -> tuple[np.ndarray, str, int, int]:
        revision = int(calibration_config["revisions"][layer.layer_id])
        seed = int(calibration_config["seeds"][layer.layer_id])
        sidecar_revision = int(bundle.calibration_revisions.get(layer.layer_id, 0))
        if revision == sidecar_revision and seed == layer.calib_seed:
            array, _ = bundle.load_calibration(layer)
        else:
            array = generate_calibration(
                layer,
                seed=seed,
                samples=bundle.calib_samples,
            )
        if inject_truncated:
            raise DeployError(
                E_CALIBRATION_TRUNCATED,
                "calibration",
                "calibration array was truncated by configured fault injection",
                layer_id=layer.layer_id,
                field="calibration",
                injected=True,
            )
        if inject_mismatch:
            wrong = np.asarray(array)
            if wrong.shape[0] > 1:
                wrong = wrong[:-1, :]
            else:
                wrong = np.concatenate(
                    [wrong, np.zeros((1, wrong.shape[1]), dtype="<f8")], axis=0
                )
            array = wrong
        if array.ndim != 2 or array.shape[1] != layer.in_features:
            raise DeployError(
                E_CALIB_SAMPLE_MISMATCH,
                "calibration",
                f"calibration shape {tuple(array.shape)} incompatible with layer "
                f"input dimension {layer.in_features}",
                layer_id=layer.layer_id,
                field="calibration",
                details={"shape": list(array.shape), "input_features": layer.in_features},
                injected=inject_mismatch,
            )
        if array.shape[0] != bundle.calib_samples:
            raise DeployError(
                E_CALIB_SAMPLE_MISMATCH,
                "calibration",
                f"calibration sample count {array.shape[0]} != "
                f"{bundle.calib_samples}",
                layer_id=layer.layer_id,
                field="calibration",
                details={
                    "actual_samples": int(array.shape[0]),
                    "expected_samples": bundle.calib_samples,
                },
                injected=inject_mismatch,
            )
        if not np.all(np.isfinite(array)):
            raise DeployError(
                E_CALIB_SAMPLE_MISMATCH,
                "calibration",
                "calibration array contains non-finite values",
                layer_id=layer.layer_id,
                field="calibration",
                injected=inject_mismatch,
            )
        return np.ascontiguousarray(array, dtype="<f8"), array_digest(array), revision, seed

    def _stage_calibration(
        self,
        bundle: LoadedBundle,
        generation: int,
        manifest: dict[str, Any],
        calibration_config: dict[str, dict[str, int]],
        invalidation: dict[str, Any],
        forced_layers: set[str],
        recovery: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        stage = "calibration"
        check_layer_count(stage, len(bundle.layers), self.config.budget)
        quant_artifacts = self.store.load_stage_artifacts(generation, "quantization")
        if set(quant_artifacts) != set(bundle.layer_ids):
            raise DeployError(
                "E_INCONSISTENT",
                stage,
                "quantization snapshot layer set is incomplete",
            )
        calib_macs = sum(
            layer.out_features * layer.in_features * bundle.calib_samples
            for layer in bundle.layers
        )
        if calib_macs > self.config.budget.max_quant_macs:
            from .errors import E_RESOURCE_LIMIT

            raise DeployError(
                E_RESOURCE_LIMIT,
                stage,
                f"calibration MAC budget exceeded: {calib_macs} > "
                f"{self.config.budget.max_quant_macs}",
                details={
                    "limit": "max_quant_macs",
                    "actual": calib_macs,
                    "max": self.config.budget.max_quant_macs,
                },
            )
        ledger = BudgetLedger(
            stage=stage,
            limits={
                "max_layers_per_stage": self.config.budget.max_layers_per_stage,
                "max_quant_macs": self.config.budget.max_quant_macs,
                "max_resident_bytes": self.config.budget.max_resident_bytes,
            },
            usage={
                "layers": len(bundle.layers),
                "calibration_macs": calib_macs,
                "calibration_samples": bundle.calib_samples,
            },
            strategy="offline_layerwise",
        )
        artifacts: list[dict[str, Any]] = []
        rebuilt: list[str] = []
        reused: list[str] = []
        processed: list[str] = []
        for layer in bundle.layers:
            quant_row = quant_artifacts[layer.layer_id]
            quant_payload = quant_row["payload"]
            if int(quant_payload.get("weight_revision", -1)) != layer.weight_revision:
                raise DeployError(
                    "E_INCONSISTENT",
                    stage,
                    f"quantization artifact revision {quant_payload.get('weight_revision')} "
                    f"does not match manifest revision {layer.weight_revision}",
                    layer_id=layer.layer_id,
                    field="weight_revision",
                )
            dequant = decode_array(quant_payload["dequant"])
            weight, _ = bundle.load_weight(layer)
            mismatch = self.faults.hit(stage, "calib_sample_mismatch", layer.layer_id)
            truncated = self.faults.hit(stage, "calib_truncated", layer.layer_id)
            calib, calib_digest, revision, seed = self._calibration_array(
                bundle,
                layer,
                calibration_config,
                inject_mismatch=mismatch,
                inject_truncated=truncated,
            )
            force = layer.layer_id in forced_layers or mismatch or truncated
            base = {
                "kind": "calibration",
                "layer": _field_signature(layer),
                "quant_digest": quant_row["digest"],
                "calib_digest": calib_digest,
                "calibration_revision": revision,
                "calibration_seed": seed,
                "calibration_samples": bundle.calib_samples,
            }
            cache_key = self._cache_key(
                base,
                force=force,
                generation=generation,
                stage=stage,
                layer_id=layer.layer_id,
            )
            cached = self._cached_or_none(stage=stage, cache_key=cache_key, force=force)
            if cached is not None:
                artifacts.append(
                    self._artifact(
                        layer_id=layer.layer_id,
                        cache_key=cache_key,
                        payload_text=cached["payload_text"],
                        disposition="reused",
                    )
                )
                reused.append(layer.layer_id)
            else:
                outputs = np.stack(
                    [
                        deterministic_linear(dequant, sample, np.zeros(layer.out_features))
                        for sample in calib
                    ],
                    axis=0,
                )
                error = dequant - weight
                payload = {
                    "schema": 1,
                    "layer_id": layer.layer_id,
                    "layer_index": layer.layer_index,
                    "weight_revision": layer.weight_revision,
                    "calibration_revision": revision,
                    "calibration_seed": seed,
                    "calibration_samples": int(calib.shape[0]),
                    "calibration_input_digest": calib_digest,
                    "stats": {
                        "weight_abs_min_hex": float_hex(float(np.min(np.abs(weight)))),
                        "weight_abs_max_hex": float_hex(float(np.max(np.abs(weight)))),
                        "weight_abs_mean_hex": float_hex(float(np.mean(np.abs(weight)))),
                        "quant_abs_error_max_hex": float_hex(
                            float(np.max(np.abs(error)))
                        ),
                        "quant_abs_error_mean_hex": float_hex(
                            float(np.mean(np.abs(error)))
                        ),
                        "quant_mse_hex": float_hex(float(np.mean(np.square(error)))),
                        "input_abs_max_hex": float_hex(float(np.max(np.abs(calib)))),
                        "input_mean_hex": float_hex(float(np.mean(calib))),
                        "input_std_hex": float_hex(float(np.std(calib))),
                        "output_abs_max_hex": float_hex(
                            float(np.max(np.abs(outputs)))
                        ),
                        "output_mean_hex": float_hex(float(np.mean(outputs))),
                        "output_std_hex": float_hex(float(np.std(outputs))),
                        "output_checksum": array_checksum(outputs),
                    },
                    "quant_artifact_digest": quant_row["digest"],
                }
                artifacts.append(
                    self._artifact(
                        layer_id=layer.layer_id,
                        cache_key=cache_key,
                        payload=payload,
                        disposition="recomputed",
                    )
                )
                rebuilt.append(layer.layer_id)
            processed.append(layer.layer_id)
            self._maybe_crash(stage, processed, generation)
        self._commit(
            generation=generation,
            stage=stage,
            artifacts=artifacts,
            metadata={"budget": ledger.to_dict()},
            bundle=bundle,
            manifest=manifest,
            calibration_config=calibration_config,
            invalidation=invalidation,
        )
        return rebuilt, reused

    def _stage_inference(
        self,
        bundle: LoadedBundle,
        generation: int,
        manifest: dict[str, Any],
        calibration_config: dict[str, dict[str, int]],
        invalidation: dict[str, Any],
        forced_layers: set[str],
        recovery: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        stage = "inference"
        check_layer_count(stage, len(bundle.layers), self.config.budget)
        quant_artifacts = self.store.load_stage_artifacts(generation, "quantization")
        calibration_artifacts = self.store.load_stage_artifacts(generation, "calibration")
        if set(quant_artifacts) != set(bundle.layer_ids) or set(
            calibration_artifacts
        ) != set(bundle.layer_ids):
            raise DeployError(
                "E_INCONSISTENT",
                stage,
                "quantization/calibration snapshot layer sets are incomplete",
            )
        strategy, ledger = plan_inference(bundle, self.config.budget)
        activation = bundle.load_initial_input()
        first = bundle.layers[0]
        if self.faults.hit(stage, "input_dim_mismatch", first.layer_id):
            activation = np.concatenate(
                [activation, np.zeros(1, dtype="<f8")]
            )
        if activation.ndim != 1 or activation.shape[0] != first.in_features:
            raise DeployError(
                E_INFER_DIM_MISMATCH,
                stage,
                f"inference input shape {tuple(activation.shape)} does not match "
                f"first layer input dimension {first.in_features}",
                layer_id=first.layer_id,
                field="initial_input",
                details={
                    "shape": list(activation.shape),
                    "expected": [first.in_features],
                },
                injected=self.faults.hit(stage, "input_dim_mismatch", first.layer_id),
            )
        activation = np.ascontiguousarray(activation, dtype="<f8")
        input_digest = array_digest(activation)
        tile_rows = strategy.get("tile_rows")
        artifacts: list[dict[str, Any]] = []
        rebuilt: list[str] = []
        reused: list[str] = []
        processed: list[str] = []
        activation_checksums: dict[str, str] = {}
        for layer in bundle.layers:
            quant_row = quant_artifacts[layer.layer_id]
            calib_row = calibration_artifacts[layer.layer_id]
            quant_payload = quant_row["payload"]
            if int(quant_payload.get("weight_revision", -1)) != layer.weight_revision:
                raise DeployError(
                    "E_INCONSISTENT",
                    stage,
                    f"inference refused stale quant artifact revision "
                    f"{quant_payload.get('weight_revision')} for manifest revision "
                    f"{layer.weight_revision}",
                    layer_id=layer.layer_id,
                    field="weight_revision",
                )
            if int(calib_row["payload"].get("weight_revision", -1)) != layer.weight_revision:
                raise DeployError(
                    "E_INCONSISTENT",
                    stage,
                    "calibration artifact revision does not match manifest",
                    layer_id=layer.layer_id,
                    field="weight_revision",
                )
            force = layer.layer_id in forced_layers
            base = {
                "kind": "inference",
                "layer": _field_signature(layer),
                "quant_digest": quant_row["digest"],
                "calibration_digest": calib_row["digest"],
                "input_digest": input_digest,
                "strategy": strategy.get("name"),
            }
            cache_key = self._cache_key(
                base,
                force=force,
                generation=generation,
                stage=stage,
                layer_id=layer.layer_id,
            )
            cached = self._cached_or_none(stage=stage, cache_key=cache_key, force=force)
            if cached is not None:
                payload = cached["payload"]
                artifacts.append(
                    self._artifact(
                        layer_id=layer.layer_id,
                        cache_key=cache_key,
                        payload_text=cached["payload_text"],
                        disposition="reused",
                    )
                )
                reused.append(layer.layer_id)
            else:
                weight = decode_array(quant_payload["dequant"])
                bias = decode_array(quant_payload["bias"])
                linear = deterministic_linear(
                    weight,
                    activation,
                    bias,
                    tile_rows=tile_rows,
                )
                output = np.tanh(linear)
                output = np.ascontiguousarray(output, dtype="<f8")
                output_digest = array_digest(output)
                checksum = array_checksum(output)
                activation_checksums[layer.layer_id] = checksum
                payload = {
                    "schema": 1,
                    "layer_id": layer.layer_id,
                    "layer_index": layer.layer_index,
                    "weight_revision": layer.weight_revision,
                    "input_digest": input_digest,
                    "output_digest": output_digest,
                    "activation_checksum": checksum,
                    "activation": encode_array(output),
                    "quant_artifact_digest": quant_row["digest"],
                    "calibration_artifact_digest": calib_row["digest"],
                    "strategy": strategy.get("name"),
                    "tile_rows": tile_rows,
                }
                artifacts.append(
                    self._artifact(
                        layer_id=layer.layer_id,
                        cache_key=cache_key,
                        payload=payload,
                        disposition="recomputed",
                    )
                )
                rebuilt.append(layer.layer_id)
            current_payload = json.loads(artifacts[-1]["payload"])
            input_digest = current_payload["output_digest"]
            activation = decode_array(current_payload["activation"])
            processed.append(layer.layer_id)
            self._maybe_crash(stage, processed, generation)
        final_row = artifacts[-1]
        final_payload = json.loads(final_row["payload"])
        prediction = decode_array(final_payload["activation"])
        prediction_digest = array_digest(prediction)
        prediction_checksum = array_checksum(prediction)
        if final_payload["output_digest"] != prediction_digest:
            raise DeployError(
                "E_INCONSISTENT",
                stage,
                "final prediction digest disagrees with last-layer activation",
                layer_id=bundle.layers[-1].layer_id,
            )
        activation_checksums = {
            item["layer_id"]: json.loads(item["payload"])["activation_checksum"]
            for item in artifacts
        }
        metadata_recovery = dict(recovery)
        metadata_recovery["rebuilt_layers_by_stage"] = dict(
            recovery.get("rebuilt_layers_by_stage", {})
        )
        metadata_recovery["rebuilt_layers_by_stage"][stage] = list(rebuilt)
        metadata_recovery["reused_layers_by_stage"] = dict(
            recovery.get("reused_layers_by_stage", {})
        )
        metadata_recovery["reused_layers_by_stage"][stage] = list(reused)
        self._commit(
            generation=generation,
            stage=stage,
            artifacts=artifacts,
            metadata={
                "budget": ledger.to_dict(),
                "recovery": metadata_recovery,
                "prediction_digest": prediction_digest,
                "prediction_checksum": prediction_checksum,
                "prediction": encode_array(prediction),
                "activation_checksums": activation_checksums,
                "strategy": strategy,
            },
            bundle=bundle,
            manifest=manifest,
            calibration_config=calibration_config,
            invalidation=invalidation,
        )
        return rebuilt, reused

    def _result_from_store(
        self,
        *,
        bundle: LoadedBundle,
        generation: int,
        already_complete: bool,
        recovery_override: dict[str, Any] | None,
    ) -> dict[str, Any]:
        generation_row = self.store.get_generation(generation)
        if generation_row is None:
            raise DeployError(
                "E_INCONSISTENT",
                "model_validation",
                "generation row is missing after stage completion",
            )
        snapshots = self.store.generation_snapshots(generation)
        if len(snapshots) != len(STAGES):
            raise DeployError(
                "E_INCONSISTENT",
                "inference",
                f"generation has {len(snapshots)}/4 committed stages",
            )
        inference_snapshot = snapshots[-1]
        inference_artifacts = self.store.snapshot_artifacts(inference_snapshot["id"])
        final_artifact = inference_artifacts[bundle.layers[-1].layer_id]
        final_payload = final_artifact["payload"]
        prediction_array = decode_array(final_payload["activation"])
        budget_ledgers = {
            snapshot["stage"]: snapshot["metadata"].get("budget")
            for snapshot in snapshots
        }
        recovery = (
            recovery_override
            if recovery_override is not None
            else inference_snapshot["metadata"].get("recovery")
        )
        errors = self.store.recent_errors(10)
        result = {
            "ok": True,
            "already_complete": already_complete,
            "generation": generation,
            "model_tag": bundle.model_tag,
            "bundle": bundle_summary(bundle),
            "progress_version": snapshots[-1]["version"],
            "progress_checksum": snapshots[-1]["content_checksum"],
            "current_stage": "completed",
            "prediction_digest": array_digest(prediction_array),
            "prediction_checksum": array_checksum(prediction_array),
            "prediction": encode_array(prediction_array),
            "prediction_values": [float(value) for value in prediction_array],
            "activation_checksums": {
                layer_id: artifact["payload"]["activation_checksum"]
                for layer_id, artifact in inference_artifacts.items()
            },
            "snapshots": snapshots,
            "trace": self.store.trace(generation=generation),
            "budget_ledgers": budget_ledgers,
            "invalidation": generation_row["invalidation"],
            "recovery": recovery,
            "errors": errors,
            "latest_error_code": errors[0]["code"] if errors else None,
            "unattributed_count": self.store.unattributed_count(),
            "checkpoint_chain_problems": self.store.verify_chain(),
        }
        return result

    def state(self) -> dict[str, Any]:
        bundle: LoadedBundle | None
        try:
            bundle = load_bundle(self.bundle_path)
        except Exception:
            bundle = None
        latest = self.store.latest_generation()
        trace = self.store.trace(limit=60)
        errors = self.store.recent_errors(20)
        if latest is None:
            stage_status = {
                stage: {
                    "code": stage,
                    "label": STAGE_LABELS[stage],
                    "status": "active" if index == 0 else "pending",
                    "version": None,
                    "checksum": None,
                    "completed_layers": [],
                }
                for index, stage in enumerate(STAGES)
            }
            layers: list[dict[str, Any]] = []
            if bundle is not None:
                layers = [
                    {
                        **layer.to_dict(),
                        "layer_shape": list(layer.layer_shape),
                        "status": "pending",
                        "quant": None,
                        "activation_checksum": None,
                    }
                    for layer in bundle.layers
                ]
            return {
                "model_tag": bundle.model_tag if bundle else None,
                "bundle": bundle_summary(bundle) if bundle else None,
                "state": {
                    "generation": 0,
                    "current_stage": STAGES[0],
                    "current_stage_label": STAGE_LABELS[STAGES[0]],
                    "progress_version": 0,
                    "progress_percent": 0,
                    "completed": False,
                },
                "stages": list(stage_status.values()),
                "layers": layers,
                "invalidation": None,
                "recovery": None,
                "budget": None,
                "errors": errors,
                "latest_error_code": errors[0]["code"] if errors else None,
                "unattributed_count": self.store.unattributed_count(),
                "prediction": None,
                "trace": trace,
                "generations": [],
            }
        generation = latest["generation"]
        snapshots = self.store.generation_snapshots(generation)
        by_stage = {snapshot["stage"]: snapshot for snapshot in snapshots}
        quant_rows = self.store.load_stage_artifacts(generation, "quantization")
        inference_rows = self.store.load_stage_artifacts(generation, "inference")
        current = next((stage for stage in STAGES if stage not in by_stage), None)
        stages: list[dict[str, Any]] = []
        for stage in STAGES:
            snapshot = by_stage.get(stage)
            if snapshot is not None:
                status = "completed"
            elif stage == current:
                status = "active"
            else:
                status = "pending"
            stages.append(
                {
                    "code": stage,
                    "label": STAGE_LABELS[stage],
                    "status": status,
                    "version": snapshot["version"] if snapshot else None,
                    "checksum": snapshot["content_checksum"] if snapshot else None,
                    "completed_layers": (
                        snapshot["completed_layers"] if snapshot else []
                    ),
                    "budget": snapshot["metadata"].get("budget")
                    if snapshot
                    else None,
                }
            )
        layers = []
        for layer in latest["manifest"]["layers"]:
            layer_id = layer["layer_id"]
            quant_row = quant_rows.get(layer_id)
            inference_row = inference_rows.get(layer_id)
            quant_payload = quant_row["payload"] if quant_row else None
            layers.append(
                {
                    **layer,
                    "status": (
                        "inferred"
                        if inference_row
                        else "quantized"
                        if quant_row
                        else "validated"
                        if "model_validation" in by_stage
                        else "pending"
                    ),
                    "quant": (
                        {
                            "dtype": quant_payload["quant"]["quant_dtype"],
                            "scale_hint": quant_payload["quant"]["scale_hint"],
                            "granularity": quant_payload["quant"]["granularity"],
                            "group_size": quant_payload["quant"]["group_size"],
                            "scale_count": quant_payload["quant"]["scale_count"],
                            "zero_point": quant_payload["quant"]["zero_point"],
                            "max_abs_error": float.fromhex(
                                quant_payload["quant"]["max_abs_error_hex"]
                            ),
                        }
                        if quant_payload
                        else None
                    ),
                    "activation_checksum": (
                        inference_row["payload"]["activation_checksum"]
                        if inference_row
                        else None
                    ),
                }
            )
        inference_snapshot = by_stage.get("inference")
        prediction = None
        if inference_snapshot:
            prediction = {
                "digest": inference_snapshot["metadata"].get("prediction_digest"),
                "checksum": inference_snapshot["metadata"].get(
                    "prediction_checksum"
                ),
                "values": [
                    float(value)
                    for value in decode_array(
                        inference_snapshot["metadata"]["prediction"]
                    )
                ],
            }
        completed_count = len(snapshots)
        return {
            "model_tag": latest["manifest"].get("model_tag"),
            "bundle": bundle_summary(bundle) if bundle else None,
            "state": {
                "generation": generation,
                "current_stage": current or "completed",
                "current_stage_label": STAGE_LABELS.get(
                    current or "completed", "已完成"
                ),
                "progress_version": self.store.latest_version(),
                "progress_percent": int(completed_count * 100 / len(STAGES)),
                "completed": latest["completed"],
            },
            "stages": stages,
            "layers": layers,
            "invalidation": latest["invalidation"],
            "recovery": (
                inference_snapshot["metadata"].get("recovery")
                if inference_snapshot
                else None
            ),
            "budget": (
                inference_snapshot["metadata"].get("budget")
                if inference_snapshot
                else snapshots[-1]["metadata"].get("budget")
                if snapshots
                else None
            ),
            "errors": errors,
            "latest_error_code": errors[0]["code"] if errors else None,
            "unattributed_count": self.store.unattributed_count(),
            "prediction": prediction,
            "trace": trace,
            "generations": [
                {
                    "generation": row["generation"],
                    "completed": row["completed"],
                    "invalidation": row["invalidation"],
                }
                for row in self.store.generations()
            ],
        }

    def verify_consistency(self) -> dict[str, Any]:
        from .consistency import ConsistencyChecker

        return ConsistencyChecker(self).run()

