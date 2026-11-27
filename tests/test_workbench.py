from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from spectral_workbench.cli import run_workbench
from spectral_workbench.config import load_config, validate_config
from spectral_workbench.formats import load_all_versions
from spectral_workbench.generator import generate_and_write
from spectral_workbench.reference import verify_training_records
from spectral_workbench.reporting import build_cross_version_report
from spectral_workbench.routing import GatingNetwork, RouterConfig, route_tokens
from spectral_workbench.training import initialize_model


ROOT = Path(__file__).resolve().parents[1]


def write_config(directory: Path, overrides: dict) -> Path:
    base = load_config(ROOT / "configs" / "default.json")
    merged = copy.deepcopy(base)

    def merge(target: dict, source: dict) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    merge(merged, overrides)
    merged.pop("_validated_token_length", None)
    validate_config(merged)
    path = directory / "config.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            {key: value for key, value in merged.items() if not key.startswith("_")},
            handle,
            indent=2,
        )
    return path


class FormatNormalizationTests(unittest.TestCase):
    def test_three_versions_share_index_and_masked_tokens(self) -> None:
        config = load_config(ROOT / "configs" / "default.json")
        config["generator"]["sample_count"] = 10
        config["generator"]["token_length"] = 16
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "data"
            generate_and_write(1234, config, data_root)
            datasets = load_all_versions(data_root, config)
            values_a, masks_a = datasets["A"].stacked()
            for version in ("B", "C"):
                values, masks = datasets[version].stacked()
                self.assertTrue(np.array_equal(masks, masks_a))
                self.assertTrue(np.allclose(values, values_a, atol=0.0, rtol=0.0))

            with (data_root / "manifest.json").open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            sample = next(
                item for item in manifest["samples"] if item["missing_token_indices"]
            )
            missing_index = sample["missing_token_indices"][0]
            rows: dict[str, dict[int, dict[str, str]]] = {}
            for version in ("A", "B", "C"):
                path = data_root / sample["files"][version]["path"]
                with path.open("r", encoding="utf-8", newline="") as handle:
                    rows[version] = {
                        int(row["token_index"]): row for row in csv.DictReader(handle)
                    }
            self.assertTrue(
                rows["A"][missing_index]["value"].lower() == "nan"
            )
            self.assertLess(float(rows["B"][missing_index]["value"]), 0.0)
            self.assertEqual(rows["C"][missing_index]["value"], "")

    def test_cross_version_audit_lists_value_mismatch_reason(self) -> None:
        config = load_config(ROOT / "configs" / "default.json")
        config["generator"]["sample_count"] = 8
        config["generator"]["token_length"] = 12
        config["model"]["token_feature_dim"] = 12
        config["evaluation"]["batch_size"] = 4
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            generate_and_write(4321, config, data_root)
            datasets = load_all_versions(data_root, config)
            valid_index = int(np.flatnonzero(datasets["B"].samples[0].mask)[0])
            datasets["B"].samples[0].token_values[valid_index] += 0.25
            model = initialize_model(
                4321, config, int(config["model"]["token_feature_dim"])
            )
            report = build_cross_version_report(
                model,
                datasets,
                config,
                output_dir=root / "reports",
            )
            self.assertFalse(report["consistent"])
            categories = {
                category
                for difference in report["differences"]
                for category in difference["reason_categories"]
            }
            self.assertIn("normalization_value_mismatch", categories)


class CapacityRoutingTests(unittest.TestCase):
    def test_reroute_and_drop_respect_capacity_and_ties(self) -> None:
        tokens = np.asarray(
            [
                [0.1, 0.2, 0.3],
                [0.2, 0.1, 0.4],
                [0.0, 0.5, 0.1],
                [0.4, 0.3, 0.2],
                [0.7, 0.1, 0.0],
                [0.2, 0.6, 0.2],
            ],
            dtype=np.float64,
        )
        sample_ids = np.arange(tokens.shape[0], dtype=np.int64)
        gate = GatingNetwork(
            np.zeros((tokens.shape[1], 4), dtype=np.float64),
            np.zeros(4, dtype=np.float64),
        )
        reroute_config = RouterConfig(
            num_experts=4,
            top_k=2,
            temperature=1.0,
            capacity_factor=0.5,
            overflow_strategy="reroute",
            near_tie_margin=0.1,
        )
        rerouted = route_tokens(gate, tokens, sample_ids, reroute_config)
        self.assertTrue(np.all(rerouted.token_counts <= rerouted.capacity_per_expert))
        self.assertEqual(int(rerouted.token_counts.sum()), 4)
        self.assertGreater(rerouted.overflow_events, 0)
        self.assertGreater(rerouted.rerouted_slots, 0)
        self.assertTrue(np.all(rerouted.near_tie_mask))

        drop_config = RouterConfig(
            num_experts=4,
            top_k=2,
            temperature=1.0,
            capacity_factor=0.5,
            overflow_strategy="drop",
            near_tie_margin=0.1,
        )
        dropped = route_tokens(gate, tokens, sample_ids, drop_config)
        self.assertTrue(np.all(dropped.token_counts <= dropped.capacity_per_expert))
        self.assertEqual(int(dropped.rerouted_slots), 0)
        self.assertGreater(int(dropped.dropped_slots), 0)
        self.assertGreater(dropped.capacity_loss, 0.0)


class EndToEndTests(unittest.TestCase):
    def _small_config(self, directory: Path) -> Path:
        return write_config(
            directory,
            {
                "generator": {
                    "token_length": 12,
                    "sample_count": 12,
                    "missing_probability": 0.5,
                    "max_missing_blocks": 1,
                },
                "model": {
                    "num_experts": 3,
                    "top_k": 2,
                    "token_feature_dim": 12,
                    "batch_size": 4,
                    "epochs": 2,
                    "capacity_factor": 0.9,
                },
                "evaluation": {"batch_size": 4},
            },
        )

    def test_pipeline_reference_cross_version_and_determinism(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._small_config(root)
            first = run_workbench(
                seed=77,
                config_path=config_path,
                output_dir=root / "first",
                quiet=True,
            )
            second = run_workbench(
                seed=77,
                config_path=config_path,
                output_dir=root / "second",
                quiet=True,
            )
            self.assertTrue(first["reference_verified"])
            self.assertTrue(first["cross_version_consistent"])
            self.assertTrue((root / "first" / "snapshot" / "model_snapshot.npz").exists())
            first_records = (root / "first" / "training" / "step_records.jsonl").read_text()
            second_records = (
                root / "second" / "training" / "step_records.jsonl"
            ).read_text()
            self.assertEqual(first_records, second_records)
            first_norm = (
                root / "first" / "normalization" / "normalization_log.jsonl"
            ).read_text()
            second_norm = (
                root / "second" / "normalization" / "normalization_log.jsonl"
            ).read_text()
            self.assertEqual(first_norm, second_norm)

    def test_tampered_assignment_reports_first_mismatch_basis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = self._small_config(root)
            run_workbench(
                seed=88,
                config_path=config_path,
                output_dir=root / "run",
                quiet=True,
            )
            source = root / "run" / "training" / "step_arrays.npz"
            with np.load(source, allow_pickle=False) as arrays:
                payload = {key: arrays[key].copy() for key in arrays.files}
            assignments = payload["assignments"]
            valid = np.argwhere(payload["masks"][0].astype(bool))[0]
            expert = int(np.argmax(assignments[0, valid[0], valid[1]]))
            assignments[0, valid[0], valid[1], expert] = 0
            tampered = root / "tampered.npz"
            np.savez_compressed(tampered, **payload)
            config = load_config(config_path)
            report = verify_training_records(tampered, config)
            self.assertFalse(report["verified"])
            self.assertEqual(report["first_mismatch_step"], 1)
            self.assertIn(
                "assignment_matrix",
                report["first_mismatch"]["mismatch_types"],
            )
            self.assertGreater(
                len(report["first_mismatch"]["routing_basis"]["token_level"]),
                0,
            )

    def test_temperature_collapse_adjustment_and_rare_concentration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = ROOT / "configs" / "collapse_temperature.json"
            summary = run_workbench(
                seed=20261128,
                config_path=config_path,
                output_dir=root / "collapse",
                quiet=True,
            )
            self.assertGreater(summary["alert_count"], 0)
            self.assertGreater(summary["adjustment_count"], 0)
            self.assertGreater(summary["rare_concentration_warning_count"], 0)
            adjustments = json.loads(
                (root / "collapse" / "training" / "adjustments.json").read_text()
            )
            event = adjustments[0]
            self.assertGreater(
                event["temperature_after"], event["temperature_before"]
            )
            self.assertIn("assignment_matrix", event["before"])
            self.assertIn("assignment_matrix", event["after"])
            self.assertIn("balance_loss", event["before"]["losses"])
            report = json.loads(
                (
                    root
                    / "collapse"
                    / "reports"
                    / "specialization_report.json"
                ).read_text()
            )
            warning = report["rare_concentration_warnings"][0]
            self.assertGreater(warning["max_primary_expert_share"], 0.8)
            self.assertTrue(warning["rare_material_group"])

    def test_reinitialize_gate_strategy_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = run_workbench(
                seed=20261128,
                config_path=ROOT / "configs" / "collapse_reinit.json",
                output_dir=root / "reinit",
                quiet=True,
            )
            self.assertGreater(summary["adjustment_count"], 0)
            adjustments = json.loads(
                (root / "reinit" / "training" / "adjustments.json").read_text()
            )
            self.assertTrue(any(item["gate_reinitialized"] for item in adjustments))
            self.assertTrue(
                all(
                    np.asarray(item["after"]["assignment_matrix"]).ndim == 3
                    for item in adjustments
                )
            )


if __name__ == "__main__":
    unittest.main()
