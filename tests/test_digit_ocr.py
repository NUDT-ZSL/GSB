from __future__ import annotations

import copy
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from digit_ocr import (
    DigitOCRService,
    DistanceMetric,
    E_CACHE_CORRUPT,
    E_CONFIDENCE_CONFLICT,
    E_INCREMENTAL_MISMATCH,
    E_INCONSISTENT,
    E_LINEAGE_STALE,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    ResourcePolicy,
    build_default_config,
    generate_samples,
    shift_prototype,
    validate_confidence_combination,
)
from digit_ocr.canonical import canonical_bytes, derive_cache_key
from digit_ocr.errors import BusinessError
from digit_ocr.pipeline import (
    decide_from_distances,
    extract_feature_values,
    feature_key_parts,
)


def node_kinds(nodes):
    counts = {}
    for node in nodes:
        kind = node.split(":", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts


class DigitOCRTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.samples = generate_samples(seed=314159, per_class=1)
        self.config = build_default_config(seed=314159)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(
        self,
        name: str = "cache",
        *,
        config=None,
        policy=None,
        samples=None,
    ):
        service = DigitOCRService(
            config or self.config,
            self.root / name,
            policy
            or ResourcePolicy(
                max_feature_items=64,
                max_feature_bytes=4 * 1024 * 1024,
                max_parallel_nodes=2,
                memory_policy="evict",
            ),
        )
        return service

    # ------------------------------------------------------------------
    # Determinism
    # ------------------------------------------------------------------
    def test_generation_and_feature_bytes_are_deterministic(self) -> None:
        repeated = generate_samples(seed=314159, per_class=1)
        self.assertEqual(len(self.samples), len(repeated))
        for left, right in zip(self.samples, repeated):
            self.assertEqual(left.image_id, right.image_id)
            self.assertEqual(left.pixels, right.pixels)
            self.assertEqual(left.image_sha256, right.image_sha256)
            self.assertEqual(left.label, right.label)
            self.assertEqual(left.uncertain, right.uncertain)
            first = extract_feature_values(left, self.config.normalization)
            second = extract_feature_values(right, self.config.normalization)
            self.assertEqual(canonical_bytes(first), canonical_bytes(second))

    def test_feature_artifacts_match_across_services(self) -> None:
        left = self.service("left")
        right = self.service("right")
        left_result = left.recognize_batch(self.samples, parallel_workers=1)
        right_result = right.recognize_batch(self.samples, parallel_workers=1)
        for sample in self.samples:
            self.assertEqual(
                left_result.feature_artifacts[sample.image_id],
                right_result.feature_artifacts[sample.image_id],
            )
            self.assertEqual(
                left_result.decision_artifacts[sample.image_id],
                right_result.decision_artifacts[sample.image_id],
            )

    # ------------------------------------------------------------------
    # Decision semantics
    # ------------------------------------------------------------------
    def test_tie_breaks_by_class_ascending(self) -> None:
        equal = {class_id: 0.25 for class_id in range(10)}
        result = decide_from_distances(equal, self.config.thresholds)
        self.assertEqual(result["predicted_class"], 0)
        tie_two = dict(equal)
        tie_two[1] = 0.1
        tie_two[4] = 0.1
        result = decide_from_distances(tie_two, self.config.thresholds)
        self.assertEqual(result["predicted_class"], 1)

    def test_rejected_high_confidence_is_conflict(self) -> None:
        with self.assertRaises(BusinessError) as raised:
            validate_confidence_combination(
                rejected=True,
                confidence="high",
                image_id="img-bad",
            )
        self.assertEqual(raised.exception.code, E_CONFIDENCE_CONFLICT)
        self.assertEqual(raised.exception.image_id, "img-bad")
        with self.assertRaises(BusinessError) as raised:
            validate_confidence_combination(
                rejected=False,
                confidence="none",
                image_id="img-bad-2",
            )
        self.assertEqual(raised.exception.code, E_CONFIDENCE_CONFLICT)

    def test_batch_labels_and_rejected_confidence_are_controlled(self) -> None:
        service = self.service()
        result = service.recognize_batch(self.samples, parallel_workers=1)
        self.assertEqual(len(result.decisions), 10)
        for decision in result.decisions:
            self.assertIn(decision["predicted_class"], range(10))
            self.assertEqual(decision["business_code"], "OK")
            if decision["rejected"]:
                self.assertNotEqual(decision["confidence"], "high")
        self.assertEqual(service.self_check(result)["business_code"], "OK")

    # ------------------------------------------------------------------
    # Cache behavior
    # ------------------------------------------------------------------
    def test_same_cache_keys_reuse_every_node(self) -> None:
        service = self.service()
        first = service.recognize_batch(self.samples, parallel_workers=1)
        second = service.recognize_batch(self.samples, parallel_workers=1)
        self.assertEqual(second.recomputed_nodes, [])
        self.assertEqual(
            set(second.reused_nodes), set(second.node_keys)
        )
        for sample in self.samples:
            self.assertEqual(
                first.decision_artifacts[sample.image_id],
                second.decision_artifacts[sample.image_id],
            )
        self.assertEqual(
            first.metrics_snapshot["version"],
            second.metrics_snapshot["version"],
        )

    def test_cache_key_parts_are_independently_verifiable(self) -> None:
        service = self.service()
        result = service.recognize_batch(self.samples, parallel_workers=1)
        decision = result.decisions[0]
        self.assertEqual(
            derive_cache_key(decision["key_parts"]), decision["cache_key"]
        )
        parts = decision["key_parts"]
        for name in (
            "image_sha256",
            "normalization_version",
            "prototype_set_checksum",
            "threshold_version",
            "distance_content_hashes",
            "feature_content_hash",
        ):
            self.assertIn(name, parts)
        feature_parts = feature_key_parts(
            self.samples[0], self.config.normalization
        )
        self.assertIn("image_sha256", feature_parts)
        self.assertIn("normalization_version", feature_parts)
        self.assertEqual(service.cache.verify_all()["business_code"], "OK")

    def test_corrupt_cache_entry_is_rejected(self) -> None:
        service = self.service()
        result = service.recognize_batch(self.samples, parallel_workers=1)
        decision = result.decisions[0]
        path = service.cache.path_for("decision", decision["cache_key"])
        path.write_bytes(b'{"bad": true}')
        with self.assertRaises(BusinessError) as raised:
            service.cache.load("decision", decision["cache_key"])
        self.assertEqual(raised.exception.code, E_CACHE_CORRUPT)

    # ------------------------------------------------------------------
    # Incremental propagation
    # ------------------------------------------------------------------
    def test_threshold_change_recomputes_only_decisions_and_metrics(self) -> None:
        service = self.service()
        initial = service.recognize_batch(self.samples, parallel_workers=1)
        old_cache_keys = {
            sample.image_id: initial.decision_artifacts[sample.image_id][
                "node_path"
            ]["decision"]
            for sample in self.samples
        }
        updated = self.config.with_thresholds(
            dataclasses.replace(
                self.config.thresholds,
                version=self.config.thresholds.version + 1,
                reject_threshold=0.25,
            )
        )
        calibrated = service.calibrate(
            self.samples, updated, parallel_workers=1
        )
        self.assertEqual(
            node_kinds(calibrated.recomputed_nodes),
            {"decision": 10, "metrics": 1},
        )
        self.assertEqual(
            node_kinds(calibrated.reused_nodes),
            {"distance": 100, "feature": 10},
        )
        self.assertTrue(calibrated.calibration["byte_identical"])
        for sample in self.samples:
            self.assertNotEqual(
                old_cache_keys[sample.image_id],
                calibrated.decision_artifacts[sample.image_id]["node_path"][
                    "decision"
                ],
            )
        self.assertEqual(service.self_check(calibrated)["business_code"], "OK")

    def test_single_prototype_change_is_targeted(self) -> None:
        service = self.service()
        initial = service.recognize_batch(self.samples, parallel_workers=1)
        updated = self.config.with_prototypes(
            shift_prototype(self.config.prototypes, 4, dx=1, dy=0)
        )
        calibrated = service.calibrate(
            self.samples, updated, parallel_workers=1
        )
        kinds = node_kinds(calibrated.recomputed_nodes)
        self.assertEqual(kinds.get("feature", 0), 0)
        self.assertEqual(kinds["distance"], 10)
        self.assertEqual(kinds["decision"], 10)
        distance_classes = {
            int(node.rsplit(":", 1)[1])
            for node in calibrated.recomputed_nodes
            if node.startswith("distance:")
        }
        self.assertEqual(distance_classes, {4})
        self.assertTrue(calibrated.calibration["byte_identical"])
        for sample in self.samples:
            self.assertEqual(
                initial.feature_artifacts[sample.image_id],
                calibrated.feature_artifacts[sample.image_id],
            )
            for class_id in range(10):
                if class_id != 4:
                    self.assertEqual(
                        initial.distance_artifacts[sample.image_id][class_id],
                        calibrated.distance_artifacts[sample.image_id][class_id],
                    )

    def test_normalization_change_recomputes_all_nodes(self) -> None:
        service = self.service()
        initial = service.recognize_batch(self.samples, parallel_workers=1)
        updated = self.config.with_normalization(
            dataclasses.replace(
                self.config.normalization,
                version=self.config.normalization.version + 1,
                input_max=254.0,
            )
        )
        calibrated = service.calibrate(
            self.samples, updated, parallel_workers=1
        )
        self.assertEqual(
            node_kinds(calibrated.recomputed_nodes),
            {"feature": 10, "distance": 100, "decision": 10, "metrics": 1},
        )
        self.assertEqual(calibrated.reused_nodes, [])
        self.assertTrue(calibrated.calibration["byte_identical"])
        for sample in self.samples:
            self.assertNotEqual(
                initial.feature_artifacts[sample.image_id]["content_hash"],
                calibrated.feature_artifacts[sample.image_id]["content_hash"],
            )

    def test_metric_change_recomputes_distances_and_decisions(self) -> None:
        service = self.service()
        service.recognize_batch(self.samples, parallel_workers=1)
        updated = self.config.with_distance_metric(
            DistanceMetric(name="manhattan", version=2)
        )
        calibrated = service.calibrate(
            self.samples, updated, parallel_workers=1
        )
        kinds = node_kinds(calibrated.recomputed_nodes)
        self.assertEqual(kinds.get("feature", 0), 0)
        self.assertEqual(kinds["distance"], 100)
        self.assertEqual(kinds["decision"], 10)
        self.assertTrue(calibrated.calibration["byte_identical"])

    def test_incremental_mismatch_reports_first_image(self) -> None:
        service = self.service()
        service.recognize_batch(self.samples, parallel_workers=1)
        updated = self.config.with_thresholds(
            dataclasses.replace(
                self.config.thresholds,
                version=self.config.thresholds.version + 1,
                reject_threshold=0.25,
            )
        )
        original = service._scratch_decisions

        def corrupted(samples, config):
            result = original(samples, config)
            image_id = sorted(result)[0]
            changed = copy.deepcopy(result)
            changed[image_id]["confidence"] = "high"
            return changed

        with mock.patch.object(service, "_scratch_decisions", side_effect=corrupted):
            with self.assertRaises(BusinessError) as raised:
                service.calibrate(self.samples, updated, parallel_workers=1)
        self.assertEqual(raised.exception.code, E_INCREMENTAL_MISMATCH)
        self.assertEqual(raised.exception.image_id, sorted(
            sample.image_id for sample in self.samples
        )[0])

    # ------------------------------------------------------------------
    # Lineage replay
    # ------------------------------------------------------------------
    def test_stale_lineage_rejected_and_archived_replay_matches(self) -> None:
        service = self.service()
        initial = service.recognize_batch(self.samples, parallel_workers=1)
        old_decision = initial.decisions[0]
        updated = self.config.with_thresholds(
            dataclasses.replace(
                self.config.thresholds,
                version=self.config.thresholds.version + 1,
                reject_threshold=0.25,
            )
        )
        calibrated = service.calibrate(
            self.samples, updated, parallel_workers=1
        )
        with self.assertRaises(BusinessError) as raised:
            service.replay_decision(old_decision)
        self.assertEqual(raised.exception.code, E_LINEAGE_STALE)
        self.assertEqual(raised.exception.image_id, old_decision["image_id"])
        self.assertIn(
            "threshold",
            raised.exception.details["stale_versions"],
        )
        archived = service.replay_decision(
            old_decision, allow_archived=True
        )
        self.assertTrue(archived["replayed"])
        self.assertTrue(archived["used_archived"])
        self.assertEqual(
            archived["predicted_class"], old_decision["predicted_class"]
        )
        self.assertEqual(archived["rejected"], old_decision["rejected"])
        self.assertEqual(archived["confidence"], old_decision["confidence"])
        current = service.replay_decision(calibrated.decisions[0])
        self.assertTrue(current["replayed"])
        self.assertFalse(current["used_archived"])

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def test_snapshot_rollback_and_corruption_are_rejected(self) -> None:
        service = self.service()
        service.recognize_batch(self.samples, parallel_workers=1)
        updated = self.config.with_thresholds(
            dataclasses.replace(
                self.config.thresholds,
                version=self.config.thresholds.version + 1,
                reject_threshold=0.25,
            )
        )
        service.calibrate(self.samples, updated, parallel_workers=1)
        current_normalization = service.snapshots.replace(
            "normalization",
            self.config.normalization.version,
            self.config.normalization.to_payload(),
            expected_checksum=self.config.normalization.checksum,
        )
        self.assertEqual(
            current_normalization.checksum, self.config.normalization.checksum
        )
        with self.assertRaises(BusinessError) as rollback:
            service.snapshots.replace(
                "thresholds", 1, self.config.thresholds.to_payload()
            )
        self.assertEqual(rollback.exception.code, E_SNAPSHOT_ROLLBACK)
        self.assertEqual(rollback.exception.snapshot_name, "thresholds")
        with self.assertRaises(BusinessError) as corrupt:
            service.snapshots.replace(
                "normalization",
                updated.normalization.version + 2,
                updated.normalization.to_payload(),
                expected_checksum="0" * 64,
            )
        self.assertEqual(corrupt.exception.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(corrupt.exception.snapshot_name, "normalization")

    def test_tampered_snapshot_file_is_corrupt(self) -> None:
        service = self.service()
        initial = service.recognize_batch(self.samples, parallel_workers=1)
        path = service.snapshots.path_for("normalization", 1)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["input_max"] = 251.0
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(BusinessError) as raised:
            service.snapshots.load("normalization", 1)
        self.assertEqual(raised.exception.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(raised.exception.snapshot_name, "normalization")

    def test_metrics_snapshots_increase_monotonically(self) -> None:
        service = self.service()
        initial = service.recognize_batch(self.samples, parallel_workers=1)
        updated = self.config.with_thresholds(
            dataclasses.replace(
                self.config.thresholds,
                version=self.config.thresholds.version + 1,
                reject_threshold=0.25,
            )
        )
        calibrated = service.calibrate(
            self.samples, updated, parallel_workers=1
        )
        self.assertEqual(initial.metrics_snapshot["version"], 1)
        self.assertEqual(calibrated.metrics_snapshot["version"], 2)
        self.assertNotEqual(
            initial.metrics_snapshot["checksum"],
            calibrated.metrics_snapshot["checksum"],
        )

    # ------------------------------------------------------------------
    # Resource limits and degradation
    # ------------------------------------------------------------------
    def test_strict_feature_residency_limit(self) -> None:
        service = self.service(
            "strict",
            policy=ResourcePolicy(
                max_feature_items=1,
                max_feature_bytes=1024 * 1024,
                max_parallel_nodes=2,
                memory_policy="error",
            ),
        )
        with self.assertRaises(BusinessError) as raised:
            service.recognize_batch(self.samples[:2], parallel_workers=1)
        self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(raised.exception.category, "feature_residency")
        self.assertIsNotNone(raised.exception.image_id)

    def test_degradation_preserves_decisions_and_covers_affected_nodes(self) -> None:
        baseline_service = self.service("baseline")
        baseline = baseline_service.recognize_batch(
            self.samples, parallel_workers=1
        )
        degraded_service = self.service(
            "degraded",
            policy=ResourcePolicy(
                max_feature_items=2,
                max_feature_bytes=1024 * 1024,
                max_parallel_nodes=2,
                memory_policy="evict",
                allow_parallel_degrade=True,
            ),
        )
        degraded = degraded_service.recognize_batch(
            self.samples, parallel_workers=8
        )
        self.assertTrue(degraded.execution["degraded"])
        categories = {
            item["category"]
            for item in degraded.execution["degradation_categories"]
        }
        self.assertIn("parallelism", categories)
        self.assertIn("feature_memory", categories)
        for sample in self.samples:
            self.assertEqual(
                degraded.decision_artifacts[sample.image_id],
                baseline.decision_artifacts[sample.image_id],
            )
        self.assertEqual(degraded.decisions, baseline.decisions)
        report = degraded.to_report()
        self.assertTrue(report["nodes"]["invariants"]["disjoint"])
        self.assertTrue(report["nodes"]["invariants"]["affected_covered"])
        self.assertEqual(
            degraded_service.self_check(degraded)["business_code"], "OK"
        )

    # ------------------------------------------------------------------
    # Spawn and self-check
    # ------------------------------------------------------------------
    def test_spawn_parallel_matches_serial(self) -> None:
        serial_service = self.service("serial")
        serial = serial_service.recognize_batch(
            self.samples, parallel_workers=1
        )
        parallel_service = self.service("parallel")
        parallel = parallel_service.recognize_batch(
            self.samples, parallel_workers=2
        )
        self.assertTrue(parallel.execution["parallel_used"])
        for sample in self.samples:
            self.assertEqual(
                serial.decision_artifacts[sample.image_id],
                parallel.decision_artifacts[sample.image_id],
            )

    def test_self_check_detects_missing_decision(self) -> None:
        service = self.service()
        result = service.recognize_batch(self.samples, parallel_workers=1)
        result.decisions.pop()
        with self.assertRaises(BusinessError) as raised:
            service.self_check(result)
        self.assertEqual(raised.exception.code, E_INCONSISTENT)
        self.assertEqual(raised.exception.category, "image_id_sets")

    def test_self_check_detects_tampered_distance(self) -> None:
        service = self.service()
        result = service.recognize_batch(self.samples, parallel_workers=1)
        image_id = self.samples[0].image_id
        result.distance_artifacts[image_id][0]["distance"] += 1.0
        with self.assertRaises(BusinessError) as raised:
            service.self_check(result)
        self.assertEqual(raised.exception.code, E_INCONSISTENT)
        self.assertEqual(raised.exception.image_id, image_id)


if __name__ == "__main__":
    unittest.main()
