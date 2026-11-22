from __future__ import annotations

import threading
import unittest
from dataclasses import replace

from quantdiag.calibration import estimate_calibration
from quantdiag.errors import (
    DiagnosticError,
    E_LINEAGE_BROKEN,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_STALE_RESULT,
)
from quantdiag.formats import LayerQuant, ParamSet, parse_quant_params, serialize_quant_params, upgrade_v1_to_v2
from quantdiag.inference import (
    DeterministicEngine,
    LayerCache,
    TraceRecord,
    verify_trace,
)
from quantdiag.model import LAYER_ORDER, NetworkWeights, generate_image_batch
from quantdiag.snapshots import SnapshotStore, build_snapshot


class LineageSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.weights = NetworkWeights()
        cls.images, _ = generate_image_batch(4, seed=701)
        calibration_images, _ = generate_image_batch(8, seed=702)
        cls.calibration = estimate_calibration(
            cls.weights, calibration_images
        )
        v1_params = ParamSet(
            "v1",
            tuple(
                LayerQuant(name, cls.weights.base_scale(name), 0)
                for name in LAYER_ORDER
            ),
        )
        parsed = parse_quant_params(
            serialize_quant_params(v1_params, version="v1")
        ).params
        cls.params = upgrade_v1_to_v2(parsed).params
        cls.snapshot = build_snapshot(
            cls.params,
            cls.calibration,
            version=2,
            model_fingerprint=cls.weights.fingerprint(),
        )
        cls.engine = DeterministicEngine(cls.weights)
        cls.cache = LayerCache()
        cls.result = cls.engine.infer(
            cls.snapshot, cls.images, cache=cls.cache
        )
        cls.record = cls.engine.make_trace(cls.result, cls.images)

    def _changed_snapshot(self):
        changed_params = ParamSet(
            "v2",
            tuple(
                (
                    LayerQuant(
                        item.name,
                        item.weight_scale * 1.08,
                        item.zero_point,
                    )
                    if item.name == "conv2"
                    else item
                )
                for item in self.params.layers
            ),
        )
        return build_snapshot(
            changed_params,
            self.calibration,
            version=3,
            model_fingerprint=self.weights.fingerprint(),
        )

    def test_replay_reproduces_checksums_and_scores(self) -> None:
        restored = TraceRecord.from_dict(self.record.to_dict())
        self.assertEqual(restored.layer_outputs, self.record.layer_outputs)
        replay = verify_trace(
            self.engine, self.snapshot, restored, cache=self.cache
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["scores_checksum"], self.record.scores_checksum)
        self.assertEqual(
            replay["layer_checksums"],
            [layer.activation_checksum for layer in self.record.layers],
        )

    def test_stale_result_names_layer_and_versions(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            verify_trace(
                self.engine,
                self._changed_snapshot(),
                self.record,
                cache=self.cache,
            )
        error = context.exception
        self.assertEqual(error.code, E_STALE_RESULT)
        self.assertEqual(error.layer, "conv2")
        self.assertIsNotNone(error.source_version)
        self.assertIsNotNone(error.current_version)
        self.assertIn("quant_version", error.details["dependency"])

        store = SnapshotStore(self.snapshot)
        store.commit(self._changed_snapshot())
        with self.assertRaises(DiagnosticError) as store_context:
            verify_trace(self.engine, store, self.record, cache=self.cache)
        self.assertEqual(store_context.exception.code, E_STALE_RESULT)

    def test_missing_and_tampered_trace_are_broken(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            verify_trace(self.engine, self.snapshot, "trace-missing")
        self.assertEqual(context.exception.code, E_LINEAGE_BROKEN)
        self.assertIsNone(context.exception.details["first_deviation_layer"])

        tampered = replace(
            self.record,
            scores=tuple(
                tuple(value + 0.125 for value in row)
                for row in self.record.scores
            ),
        )
        with self.assertRaises(DiagnosticError) as context:
            verify_trace(
                self.engine, self.snapshot, tampered, cache=self.cache
            )
        error = context.exception
        self.assertEqual(error.code, E_LINEAGE_BROKEN)
        self.assertEqual(error.details["first_deviation_layer"], "fc")
        self.assertAlmostEqual(error.details["deviation"], 0.125, places=12)

    def test_snapshot_rollback_corruption_and_whole_replacement(self) -> None:
        older = build_snapshot(
            self.params,
            self.calibration,
            version=1,
            model_fingerprint=self.weights.fingerprint(),
        )
        newer = build_snapshot(
            self.params,
            self.calibration,
            version=3,
            model_fingerprint=self.weights.fingerprint(),
        )
        store = SnapshotStore(newer)
        with self.assertRaises(DiagnosticError) as context:
            store.commit(older)
        self.assertEqual(context.exception.code, E_SNAPSHOT_ROLLBACK)

        corrupt = replace(
            build_snapshot(
                self.params,
                self.calibration,
                version=4,
                model_fingerprint=self.weights.fingerprint(),
            ),
            checksum="snap-sha256:corrupt",
        )
        with self.assertRaises(DiagnosticError) as context:
            store.commit(corrupt)
        self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)

        same_version = build_snapshot(
            self._changed_snapshot().params,
            self.calibration,
            version=3,
            model_fingerprint=self.weights.fingerprint(),
        )
        with self.assertRaises(DiagnosticError) as context:
            store.commit(same_version)
        self.assertEqual(context.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_non_blocking_stage_busy_and_atomic_replace(self) -> None:
        current = self.snapshot
        replacement = build_snapshot(
            self.params,
            self.calibration,
            version=4,
            model_fingerprint=self.weights.fingerprint(),
        )
        store = SnapshotStore(current)
        started = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with store.hold_stage():
                started.set()
                release.wait(timeout=3)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(started.wait(timeout=2))
        with self.assertRaises(DiagnosticError) as context:
            store.commit(replacement)
        self.assertEqual(context.exception.code, E_STAGE_BUSY)
        release.set()
        thread.join(timeout=3)
        committed = store.commit(replacement)
        self.assertIs(committed, replacement)
        self.assertIs(store.current, replacement)
        self.assertEqual(current.checksum, current.compute_checksum())
        self.assertEqual(store.history, (current, replacement))


if __name__ == "__main__":
    unittest.main()

