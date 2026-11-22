from __future__ import annotations

import unittest

from quantdiag.calibration import estimate_calibration
from quantdiag.formats import (
    LayerQuant,
    ParamSet,
    parse_quant_params,
    serialize_quant_params,
    upgrade_v1_to_v2,
)
from quantdiag.inference import DeterministicEngine, LayerCache
from quantdiag.model import LAYER_ORDER, NetworkWeights, generate_image_batch
from quantdiag.snapshots import build_snapshot
from quantdiag.utils import array_bytes


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.weights = NetworkWeights()
        cls.images, _ = generate_image_batch(6, seed=501)
        calibration_images, _ = generate_image_batch(12, seed=502)
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
        cls.v2_params = upgrade_v1_to_v2(parsed).params
        cls.snapshot = build_snapshot(
            cls.v2_params,
            cls.calibration,
            version=1,
            model_fingerprint=cls.weights.fingerprint(),
        )
        cls.engine = DeterministicEngine(cls.weights)

    def test_byte_reproducibility_with_cold_and_warm_cache(self) -> None:
        cache = LayerCache()
        first = self.engine.infer(self.snapshot, self.images, cache=cache)
        second = self.engine.infer(self.snapshot, self.images, cache=cache)
        fresh = DeterministicEngine(self.weights).infer(
            self.snapshot, self.images, cache=LayerCache()
        )
        self.assertEqual(first.recomputed_layers, LAYER_ORDER)
        self.assertEqual(second.reused_layers, LAYER_ORDER)
        self.assertEqual(first.scores_checksum, second.scores_checksum)
        self.assertEqual(first.scores_checksum, fresh.scores_checksum)
        self.assertEqual(
            array_bytes(first.scores_array), array_bytes(fresh.scores_array)
        )
        self.assertEqual(first.result_checksum, fresh.result_checksum)

    def test_cache_key_composition_and_hit_count(self) -> None:
        cache = LayerCache()
        self.engine.infer(self.snapshot, self.images, cache=cache)
        self.engine.infer(self.snapshot, self.images, cache=cache)
        info = cache.explain("conv2")
        self.assertIsNotNone(info)
        components = info["components"]
        for field in (
            "image_hash",
            "input_hash",
            "upstream_hash",
            "upstream_key",
            "quant_version",
            "calibration_version",
            "per_channel",
            "dynamic_range_mode",
        ):
            self.assertIn(field, components)
        self.assertTrue(info["key"].startswith("lk-"))
        self.assertEqual(info["product_hash"], info["activation_checksum"])
        self.assertGreaterEqual(info["hit_count"], 1)

    def _warm_cache(self) -> LayerCache:
        cache = LayerCache()
        self.engine.infer(self.snapshot, self.images, cache=cache)
        return cache

    def test_scale_change_recomputes_layer_and_suffix(self) -> None:
        cache = self._warm_cache()
        changed_params = ParamSet(
            self.v2_params.format_version,
            tuple(
                (
                    LayerQuant(
                        item.name,
                        item.weight_scale * 1.1,
                        item.zero_point,
                        per_channel=False,
                    )
                    if item.name == "conv2"
                    else item
                )
                for item in self.v2_params.layers
            ),
        )
        changed = build_snapshot(
            changed_params,
            self.calibration,
            version=2,
            model_fingerprint=self.weights.fingerprint(),
        )
        analysis = self.engine.analyze_change(self.snapshot, changed)
        self.assertEqual(analysis.affected_layers, ("conv2", "fc"))
        self.assertEqual(analysis.reusable_layers, ("conv1",))
        self.assertEqual({event.kind for event in analysis.events}, {"scale"})
        result = self.engine.infer(changed, self.images, cache=cache)
        self.assertEqual(result.reused_layers, ("conv1",))
        self.assertEqual(result.recomputed_layers, ("conv2", "fc"))

    def test_calibration_sample_change_recomputes_suffix_even_if_range_equal(self) -> None:
        cache = self._warm_cache()
        calibration = estimate_calibration(
            self.weights,
            generate_image_batch(12, seed=502)[0],
            samples_by_layer={"conv1": 12, "conv2": 6, "fc": 12},
        )
        changed = build_snapshot(
            self.v2_params,
            calibration,
            version=2,
            model_fingerprint=self.weights.fingerprint(),
        )
        analysis = self.engine.analyze_change(self.snapshot, changed)
        self.assertEqual(analysis.affected_layers, ("conv2", "fc"))
        self.assertIn(
            "calibration_samples",
            {event.kind for event in analysis.events},
        )
        result = self.engine.infer(changed, self.images, cache=cache)
        self.assertEqual(result.reused_layers, ("conv1",))
        self.assertEqual(result.recomputed_layers, ("conv2", "fc"))

    def test_dynamic_mode_switch_recomputes_suffix(self) -> None:
        cache = self._warm_cache()
        changed_params = ParamSet(
            "v3",
            tuple(
                (
                    LayerQuant(
                        item.name,
                        item.weight_scale,
                        item.zero_point,
                        per_channel=item.per_channel,
                        channel_scales=item.channel_scales,
                        calibration_samples=item.calibration_samples,
                        dynamic_range_mode="per_activation",
                    )
                    if item.name == "conv2"
                    else item
                )
                for item in self.v2_params.layers
            ),
        )
        changed = build_snapshot(
            changed_params,
            self.calibration,
            version=2,
            model_fingerprint=self.weights.fingerprint(),
        )
        analysis = self.engine.analyze_change(self.snapshot, changed)
        self.assertEqual(analysis.affected_layers, ("conv2", "fc"))
        self.assertEqual(
            {event.kind for event in analysis.events},
            {"dynamic_range_mode"},
        )
        result = self.engine.infer(changed, self.images, cache=cache)
        self.assertEqual(result.reused_layers, ("conv1",))
        self.assertEqual(result.recomputed_layers, ("conv2", "fc"))

    def test_changed_input_never_reuses_cache(self) -> None:
        cache = LayerCache()
        baseline = self.engine.infer(self.snapshot, self.images, cache=cache)
        original_hash = baseline.batch_hash
        other_images, _ = generate_image_batch(6, seed=999)
        result = self.engine.infer(self.snapshot, other_images, cache=cache)
        self.assertEqual(result.recomputed_layers, LAYER_ORDER)
        self.assertEqual(result.reused_layers, ())
        self.assertNotEqual(
            result.batch_hash,
            original_hash,
        )

    def test_tampered_checksum_is_rejected(self) -> None:
        from dataclasses import replace
        from quantdiag.errors import DiagnosticError, E_SNAPSHOT_CORRUPT

        bad = replace(self.snapshot, checksum="snap-sha256:bad")
        with self.assertRaises(DiagnosticError) as context:
            self.engine.infer(bad, self.images, cache=LayerCache())
        self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)


if __name__ == "__main__":
    unittest.main()

