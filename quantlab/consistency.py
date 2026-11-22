from __future__ import annotations

from typing import Any

import numpy as np

from .config import WorkbenchConfig
from .deterministic import generate_weights, initial_activation
from .inference import run_inference
from .quantization import QuantizedLayer, verify_dequantized_weights
from .serialization import array_checksum, decode_array
from .snapshots import SnapshotStore


def _check(name: str, passed: bool, detail: Any = None) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def run_consistency(
    config: WorkbenchConfig,
    store: SnapshotStore,
    entries: list[dict[str, Any]],
    *,
    invalidation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    stage_snapshots = {
        stage: store.stage_snapshot(stage)
        for stage in ("model_validation", "quantization", "calibration", "inference")
    }
    expected_ids = [str(entry["layer_id"]) for entry in entries]
    layer_sets: dict[str, list[str]] = {}
    validation = stage_snapshots["model_validation"]
    if validation:
        layer_sets["model_validation"] = sorted(validation.payload.get("layers", {}))
    else:
        layer_sets["model_validation"] = []
    quant = stage_snapshots["quantization"]
    if quant:
        layer_sets["quantization"] = sorted(
            record["layer_id"] for record in quant.payload.get("records", [])
        )
    else:
        layer_sets["quantization"] = []
    calibration = stage_snapshots["calibration"]
    if calibration:
        layer_sets["calibration"] = sorted(calibration.payload.get("stats", {}))
    else:
        layer_sets["calibration"] = []
    inference = stage_snapshots["inference"]
    if inference:
        layer_sets["inference"] = sorted(inference.payload.get("activations", {}))
    else:
        layer_sets["inference"] = []
    expected_sorted = sorted(expected_ids)
    set_matches = all(layer_set == expected_sorted for layer_set in layer_sets.values())
    checks.append(
        _check(
            "four_stage_layer_sets_identical",
            set_matches,
            {"expected": expected_sorted, "actual": layer_sets},
        )
    )

    quant_verifications: list[dict[str, Any]] = []
    if quant:
        try:
            records = [QuantizedLayer.from_payload(item) for item in quant.payload["records"]]
            for entry, record in zip(entries, records, strict=True):
                weights = generate_weights(config, entry)
                quant_verifications.append(verify_dequantized_weights(record, weights))
        except Exception as exc:
            quant_verifications.append({"error": f"{type(exc).__name__}: {exc}"})
    quant_ok = bool(quant_verifications) and all(
        item.get("codes_match")
        and item.get("dequantized_from_codes_match")
        and item.get("independent_recompute_match")
        for item in quant_verifications
    )
    checks.append(
        _check(
            "quant_params_and_dequantized_weights_reconcile",
            quant_ok,
            quant_verifications,
        )
    )

    if quant and inference:
        try:
            records = {
                item["layer_id"]: QuantizedLayer.from_payload(item)
                for item in quant.payload["records"]
            }
            initial_payload = inference.payload["initial_input"]
            initial = decode_array(initial_payload)
            recomputed = run_inference(
                entries,
                records,
                initial,
                max_block_elements=config.max_block_elements,
            )
            checksum_checks = []
            for entry in entries:
                layer_id = str(entry["layer_id"])
                committed = inference.payload["activations"][layer_id]["checksum"]
                actual = array_checksum(recomputed["activations"][layer_id])
                checksum_checks.append(
                    {
                        "layer_id": layer_id,
                        "committed": committed,
                        "recomputed": actual,
                        "match": committed == actual,
                    }
                )
            committed_prediction = decode_array(inference.payload["final_prediction"])
            recomputed_prediction_match = (
                array_checksum(committed_prediction)
                == array_checksum(recomputed["final_prediction"])
            )
            last_layer_id = str(entries[-1]["layer_id"])
            stored_last_activation = decode_array(
                inference.payload["activations"][last_layer_id]["array"]
            )
            last_activation_prediction = np.ascontiguousarray(
                np.mean(stored_last_activation, axis=0)
            )
            last_activation_match = (
                array_checksum(committed_prediction)
                == array_checksum(last_activation_prediction)
            )
            checks.append(
                _check(
                    "prediction_independently_recomputable",
                    all(item["match"] for item in checksum_checks)
                    and recomputed_prediction_match
                    and last_activation_match,
                    {
                        "layers": checksum_checks,
                        "recomputed_prediction_match": recomputed_prediction_match,
                        "last_activation_prediction_match": last_activation_match,
                    },
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "prediction_independently_recomputable",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
    else:
        checks.append(
            _check(
                "prediction_independently_recomputable",
                False,
                "缺少量化或推理快照",
            )
        )

    if invalidation is not None:
        recomputed = set(invalidation.get("recomputed_layers", []))
        reused = set(invalidation.get("reused_layers", []))
        affected = set(invalidation.get("affected_layers", []))
        disjoint = recomputed.isdisjoint(reused)
        covers = affected.issubset(recomputed | reused)
        checks.append(
            _check(
                "invalidation_sets_disjoint_and_cover_affected",
                disjoint and covers,
                {
                    "disjoint": disjoint,
                    "covers_affected": covers,
                    "recomputed": sorted(recomputed),
                    "reused": sorted(reused),
                    "affected": sorted(affected),
                },
            )
        )

    progress = store.progress_view()
    versions = [entry["version"] for entry in progress["entries"]]
    completed_unique = all(
        len(set(entry["completed_layers"])) == len(entry["completed_layers"])
        for entry in progress["entries"]
    )
    progress_ok = versions == sorted(set(versions)) and completed_unique
    checks.append(
        _check(
            "snapshot_versions_monotonic_and_completed_sets_unique",
            progress_ok,
            {"versions": versions},
        )
    )
    return {
        "ok": all(item["passed"] for item in checks),
        "checks": checks,
        "layer_sets": layer_sets,
        "progress_version_count": len(versions),
        "latest_version": versions[-1] if versions else None,
    }
