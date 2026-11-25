"""Deterministic local sample generation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_json, write_jsonl

DEFAULT_SEED = 416


def _build_config(samples_per_batch: int) -> dict[str, Any]:
    return {
        "schema_version": "calibration-config-v1",
        "segment_parameter_version": "seg-20261126-416",
        "reference_cycle_ids": ["C00"],
        "min_sample_count": 50,
        "expected_sample_count": samples_per_batch,
        "feature_domains": {
            "age": {"dtype": "numeric", "min": 18, "max": 80, "required": True},
            "amount": {"dtype": "numeric", "min": 0.0, "max": 200000.0, "required": True},
            "utilization": {"dtype": "numeric", "min": 0.0, "max": 1.0, "required": True},
            "region": {
                "dtype": "categorical",
                "allowed": ["east", "south", "west", "north"],
                "required": True,
            },
            "channel": {
                "dtype": "categorical",
                "allowed": ["app", "web", "partner"],
                "required": True,
            },
        },
        "cost_weights": {"false_positive": 1.0, "false_negative": 4.0},
        "decision_bands": [
            {"category": "approve", "upper": 0.40},
            {"category": "manual_review", "lower": 0.40, "upper": 0.65},
            {"category": "high_risk", "lower": 0.65},
        ],
        "calibrations": [
            {
                "item": "score",
                "source_field": "score",
                "anchor_probabilities": [0.10, 0.25, 0.50, 0.75, 0.90],
                "target_anchor_values": [0.10, 0.25, 0.50, 0.75, 0.90],
                "target_quantiles": [0.10, 0.25, 0.50, 0.75, 0.90],
                "segments": [
                    {"segment_index": 0, "left_probability": 0.10, "right_probability": 0.50, "boundary": 0.50},
                    {"segment_index": 1, "left_probability": 0.50, "right_probability": 0.90, "boundary": 0.50},
                ],
                "threshold_bands": [
                    {"category": "approve", "upper": 0.40},
                    {"category": "manual_review", "lower": 0.40, "upper": 0.65},
                    {"category": "high_risk", "lower": 0.65},
                ],
                "cost_weights": {"false_positive": 1.0, "false_negative": 4.0},
            },
            {
                "item": "amount",
                "source_field": "amount",
                "anchor_probabilities": [0.10, 0.25, 0.50, 0.75, 0.90],
                "target_anchor_values": [-1.28, -0.67, 0.0, 0.67, 1.28],
                "target_quantiles": [0.10, 0.50, 0.90],
                "segments": [
                    {"segment_index": 0, "left_probability": 0.10, "right_probability": 0.50},
                    {"segment_index": 1, "left_probability": 0.50, "right_probability": 0.90},
                ],
                "cost_weights": {"false_positive": 1.0, "false_negative": 2.0},
            },
            {
                "item": "utilization",
                "source_field": "utilization",
                "anchor_probabilities": [0.10, 0.50, 0.90],
                "target_anchor_values": [0.10, 0.50, 0.90],
                "target_quantiles": [0.10, 0.50, 0.90],
                "segments": [
                    {"segment_index": 0, "left_probability": 0.10, "right_probability": 0.50},
                    {"segment_index": 1, "left_probability": 0.50, "right_probability": 0.90},
                ],
            },
        ],
        "boundary_targets": [
            {
                "target": "approve_boundary",
                "score_field": "score",
                "threshold": 0.40,
                "direction": "high_risk",
                "max_flips": 8,
                "hysteresis_margin": 0.055,
                "require_opposite_label_pair": True,
                "cost_weights": {"false_positive": 1.0, "false_negative": 3.0},
            },
            {
                "target": "review_boundary",
                "score_field": "score",
                "threshold": 0.65,
                "direction": "high_risk",
                "max_flips": 2,
                "hysteresis_margin": 0.060,
                "require_opposite_label_pair": True,
                "cost_weights": {"false_positive": 1.5, "false_negative": 4.0},
            },
        ],
        "lineage": {"downstream_by_batch": {}},
        "batch_overrides": {
            "B002": {
                "calibrations": {
                    "score": {
                        "change_note": "event-1 target quantile adjustment",
                        "target_anchor_values": [0.11, 0.26, 0.50, 0.74, 0.89],
                    }
                }
            },
            "B004": {
                "calibrations": {
                    "score": {
                        "change_note": "event-2 segment boundary refinement",
                        "segments": [
                            {"segment_index": 0, "left_probability": 0.10, "right_probability": 0.25},
                            {"segment_index": 1, "left_probability": 0.25, "right_probability": 0.75},
                            {"segment_index": 2, "left_probability": 0.75, "right_probability": 0.90},
                        ],
                    }
                }
            },
            "B003": {"feature_encoding_version": "channel-v2-staged"},
            "B006": {"feature_encoding_version": "channel-v2-staged"},
        },
    }


def build_dataset(
    *,
    seed: int = DEFAULT_SEED,
    cycles: int = 3,
    batches_per_cycle: int = 3,
    samples_per_batch: int = 144,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    rng = np.random.default_rng(seed)
    config = _build_config(samples_per_batch)
    entity_bases: list[list[float]] = []
    for slot in range(batches_per_cycle):
        bases: list[float] = []
        for entity_index in range(samples_per_batch):
            selector = entity_index % 23
            if selector == 0:
                base_score = float(0.40 + rng.normal(0.0, 0.018))
            elif selector == 1:
                base_score = float(0.65 + rng.normal(0.0, 0.018))
            else:
                base_score = float(rng.beta(2.2, 2.0))
            bases.append(base_score)
        entity_bases.append(bases)

    rows: list[dict[str, Any]] = []
    batch_index = 0
    for cycle_index in range(cycles):
        cycle_id = f"C{cycle_index:02d}"
        for slot in range(batches_per_cycle):
            batch_id = f"B{batch_index:03d}"
            batch_index += 1
            for entity_index in range(samples_per_batch):
                base_score = entity_bases[slot][entity_index]
                cycle_drift = (cycle_index - 1) * 0.012
                score = float(np.clip(base_score + cycle_drift + rng.normal(0.0, 0.018), 0.001, 0.999))
                label_probability = 1.0 / (1.0 + np.exp(-8.0 * (score - 0.52)))
                label = int(rng.random() < label_probability)
                age = int(np.clip(round(24 + 38 * score + rng.normal(0.0, 5.0)), 18, 80))
                amount = float(np.exp(8.4 + 1.25 * score + rng.normal(0.0, 0.42)))
                utilization = float(np.clip(0.08 + 0.78 * score + rng.normal(0.0, 0.08), 0.0, 1.0))
                region = str(rng.choice(["east", "south", "west", "north"], p=[0.34, 0.27, 0.23, 0.16]))
                channel = str(rng.choice(["app", "web", "partner"], p=[0.58, 0.31, 0.11]))
                rows.append(
                    {
                        "batch_id": batch_id,
                        "cycle_id": cycle_id,
                        "entity_id": f"G{slot:02d}-E{entity_index:04d}",
                        "features": {
                            "age": age,
                            "amount": round(amount, 2),
                            "utilization": round(utilization, 6),
                            "region": region,
                            "channel": channel,
                        },
                        "score": round(score, 6),
                        "label": label,
                    }
                )

    for row in rows:
        if row["batch_id"] == "B001" and row["entity_id"].endswith("E0005"):
            row["features"]["age"] = 199
        if row["batch_id"] == "B004" and row["entity_id"].endswith("E0007"):
            row["features"]["region"] = "moon_base"
        if row["batch_id"] == "B007" and row["entity_id"].endswith("E0009"):
            row["features"]["amount"] = None

    events = [
        {
            "event_seq": 1,
            "batch_id": "B002",
            "cycle_id": "C00",
            "change_dimension": "calibration.target_quantiles.score",
            "old_config_summary": {
                "segment_parameter_version": config["segment_parameter_version"],
                "target_anchor_values": [0.10, 0.25, 0.50, 0.75, 0.90],
            },
            "new_config_summary": {
                "segment_parameter_version": config["segment_parameter_version"],
                "target_anchor_values": [0.11, 0.26, 0.50, 0.74, 0.89],
                "affected_batch_ids": ["B002"],
            },
        },
        {
            "event_seq": 2,
            "batch_id": "B004",
            "cycle_id": "C01",
            "change_dimension": "segment_boundaries.score",
            "old_config_summary": {
                "segments": [
                    {"segment_index": 0, "left_probability": 0.10, "right_probability": 0.50},
                    {"segment_index": 1, "left_probability": 0.50, "right_probability": 0.90},
                ]
            },
            "new_config_summary": {
                "segments": config["batch_overrides"]["B004"]["calibrations"]["score"]["segments"],
                "affected_batch_ids": ["B004"],
            },
        },
        {
            "event_seq": 3,
            "batch_id": "B003",
            "cycle_id": "C01",
            "change_dimension": "feature_redefinition.channel",
            "old_config_summary": {"encoding": "one_hot_v1", "scope": "staged"},
            "new_config_summary": {
                "encoding": "target_aware_v2",
                "scope": "staged",
                "affected_batch_ids": ["B003", "B006"],
            },
        },
    ]
    return rows, config, events


def generate_files(
    out_dir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    cycles: int = 3,
    batches_per_cycle: int = 3,
    samples_per_batch: int = 144,
) -> dict[str, Path]:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    rows, config, events = build_dataset(
        seed=seed,
        cycles=cycles,
        batches_per_cycle=batches_per_cycle,
        samples_per_batch=samples_per_batch,
    )
    samples_path = output / "samples.jsonl"
    config_path = output / "calibration_config.json"
    baseline_config_path = output / "calibration_config.baseline.json"
    events_path = output / "change_events.jsonl"
    metadata_path = output / "generation_metadata.json"

    write_jsonl(samples_path, rows)
    atomic_write_json(config_path, config)
    baseline = dict(config)
    baseline.pop("batch_overrides", None)
    atomic_write_json(baseline_config_path, baseline)
    write_jsonl(events_path, events)
    batch_count = cycles * batches_per_cycle
    metadata = {
        "seed": seed,
        "cycles": cycles,
        "batches_per_cycle": batches_per_cycle,
        "samples_per_batch": samples_per_batch,
        "batch_count": batch_count,
        "record_count": len(rows),
        "reference_cycle_ids": config["reference_cycle_ids"],
        "targeted_change_closure": ["B002", "B003", "B004", "B006"],
        "targeted_reused_batches": ["B000", "B001", "B005", "B007", "B008"],
        "files": {
            "samples": samples_path.name,
            "config": config_path.name,
            "baseline_config": baseline_config_path.name,
            "events": events_path.name,
        },
    }
    atomic_write_json(metadata_path, metadata)
    return {
        "samples": samples_path,
        "config": config_path,
        "baseline_config": baseline_config_path,
        "events": events_path,
        "metadata": metadata_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic risk-governance inputs")
    parser.add_argument("--out-dir", default="data/generated")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--batches-per-cycle", type=int, default=3)
    parser.add_argument("--samples-per-batch", type=int, default=144)
    args = parser.parse_args(argv)
    paths = generate_files(
        args.out_dir,
        seed=args.seed,
        cycles=args.cycles,
        batches_per_cycle=args.batches_per_cycle,
        samples_per_batch=args.samples_per_batch,
    )
    for key, path in paths.items():
        print(f"{key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
