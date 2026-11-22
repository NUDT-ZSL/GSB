from __future__ import annotations

import json
import unittest

from quantdiag.calibration import estimate_calibration
from quantdiag.errors import (
    DiagnosticError,
    E_FORMAT_CORRUPT,
    E_VERSION_UNSUPPORTED,
)
from quantdiag.formats import (
    LayerQuant,
    ParamSet,
    downgrade_v3_to_v1,
    parse_quant_params,
    serialize_quant_params,
    upgrade_v1_to_v2,
    upgrade_v2_to_v3,
)
from quantdiag.model import LAYER_ORDER, NetworkWeights, generate_image_batch


class FormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.weights = NetworkWeights()
        calibration_images, _ = generate_image_batch(8, seed=101)
        cls.calibration = estimate_calibration(cls.weights, calibration_images)
        cls.v1 = ParamSet(
            "v1",
            tuple(
                LayerQuant(name, cls.weights.base_scale(name), 0)
                for name in LAYER_ORDER
            ),
        )
        cls.v1_text = serialize_quant_params(cls.v1, version="v1")
        cls.v2 = upgrade_v1_to_v2(cls.v1).params
        cls.v2_text = serialize_quant_params(cls.v2, version="v2")
        cls.v3 = upgrade_v2_to_v3(cls.v2, cls.calibration).params
        cls.v3_text = serialize_quant_params(
            cls.v3, calibration=cls.calibration
        )

    def test_three_formats_parse_and_dispatch_by_field_set(self) -> None:
        v1 = parse_quant_params(self.v1_text)
        v2 = parse_quant_params(self.v2_text)
        v3 = parse_quant_params(self.v3_text)
        self.assertEqual(v1.params.format_version, "v1")
        self.assertEqual(v2.params.format_version, "v2")
        self.assertEqual(v3.params.format_version, "v3")
        self.assertEqual(len(v1.params.layers), 3)
        self.assertFalse(v1.params.layer("conv1").per_channel)
        self.assertEqual(v2.params.layer("fc").calibration_samples, 0)
        self.assertIsNotNone(v3.calibration)
        self.assertEqual(v3.calibration.version, self.calibration.version)

        no_header_v1 = self.v1_text.splitlines()[1:]
        parsed = parse_quant_params("\n".join(no_header_v1) + "\n")
        self.assertEqual(parsed.params.format_version, "v1")

        no_version_v2 = self.v2_text.splitlines()[1:]
        parsed = parse_quant_params("\n".join(no_version_v2) + "\n")
        self.assertEqual(parsed.params.format_version, "v2")

    def test_roundtrip_values_are_exact(self) -> None:
        parsed_v1 = parse_quant_params(self.v1_text).params
        parsed_v2 = parse_quant_params(self.v2_text).params
        parsed_v3 = parse_quant_params(self.v3_text)
        for name in LAYER_ORDER:
            self.assertEqual(
                parsed_v1.layer(name).weight_scale,
                self.v1.layer(name).weight_scale,
            )
            self.assertEqual(
                parsed_v2.layer(name).channel_scales,
                self.v2.layer(name).channel_scales,
            )
            self.assertEqual(
                parsed_v3.params.layer(name).weight_scale,
                self.v3.layer(name).weight_scale,
            )

    def test_unknown_version_code(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            parse_quant_params(
                "version=v99\nlayer,scale,zero_point\nconv1,0.1,0\n"
            )
        self.assertEqual(context.exception.code, E_VERSION_UNSUPPORTED)
        self.assertEqual(context.exception.format_version, "v99")
        self.assertEqual(context.exception.field, "version")

        payload = json.loads(self.v3_text)
        payload["format_version"] = "v99"
        with self.assertRaises(DiagnosticError) as context:
            parse_quant_params(json.dumps(payload))
        self.assertEqual(context.exception.code, E_VERSION_UNSUPPORTED)
        self.assertEqual(context.exception.format_version, "v99")

    def test_truncated_header_and_malformed_json(self) -> None:
        for text in ("vers", "version", "version=v"):
            with self.assertRaises(DiagnosticError) as context:
                parse_quant_params(text)
            self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)
            self.assertEqual(context.exception.field, "version")
        with self.assertRaises(DiagnosticError) as context:
            parse_quant_params('{"format_version":"v3","layers":')
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(context.exception.field, "format_version")

    def test_row_field_count_and_unparseable_number(self) -> None:
        lines = self.v2_text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("conv2,"):
                lines[index] = ",".join(line.split(",")[:-1])
                break
        bad_count = "\n".join(lines) + "\n"
        with self.assertRaises(DiagnosticError) as context:
            parse_quant_params(bad_count)
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)
        self.assertIn(context.exception.field, {"calibration_samples", "<extra>"})

        v1_lines = self.v1_text.splitlines()
        v1_lines[2] = "conv1,not-a-number,0"
        bad_number = "\n".join(v1_lines) + "\n"
        with self.assertRaises(DiagnosticError) as context:
            parse_quant_params(bad_number)
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(context.exception.field, "scale")
        self.assertEqual(context.exception.format_version, "v1")

    def test_missing_required_field_and_empty_table(self) -> None:
        with self.assertRaises(DiagnosticError) as context:
            parse_quant_params(
                "version=v2\n"
                "fields=layer,weight_scale,zero_point,per_channel,channel_scales\n"
            )
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(context.exception.field, "calibration_samples")

        with self.assertRaises(DiagnosticError) as context:
            parse_quant_params("version=v1\nlayer,scale,zero_point\n")
        self.assertEqual(context.exception.code, E_FORMAT_CORRUPT)
        self.assertEqual(context.exception.field, "layer")

    def test_upgrade_and_downgrade_audit_trails(self) -> None:
        first = upgrade_v1_to_v2(self.v1)
        self.assertGreaterEqual(len(first.fills), 12)
        self.assertTrue(all(item.source for item in first.fills))
        second = upgrade_v2_to_v3(first.params, self.calibration)
        self.assertTrue(
            any(item.field == "dynamic_range_mode" for item in second.fills)
        )

        nonuniform = ParamSet(
            "v3",
            tuple(
                LayerQuant(
                    name,
                    self.weights.base_scale(name),
                    0,
                    per_channel=True,
                    channel_scales=tuple(
                        self.weights.base_scale(name) * (1 + 0.2 * index)
                        for index in range(self.weights.weight(name).shape[0])
                    ),
                    calibration_samples=8,
                    dynamic_range_mode="per_activation",
                )
                for name in LAYER_ORDER
            ),
        )
        downgraded = downgrade_v3_to_v1(
            nonuniform, channel_flatten_rule="mean"
        )
        self.assertTrue(downgraded.semantic_loss)
        self.assertTrue(
            any(
                item.field == "channel_scales" and item.semantic_loss
                for item in downgraded.drops
            )
        )
        self.assertTrue(
            any(
                item.field == "dynamic_range_mode" and item.semantic_loss
                for item in downgraded.drops
            )
        )
        self.assertFalse(downgraded.params.layer("conv1").per_channel)

        uniform_params = ParamSet(
            "v3",
            tuple(
                LayerQuant(
                    name,
                    self.weights.base_scale(name),
                    0,
                    per_channel=True,
                    channel_scales=tuple(
                        self.weights.base_scale(name)
                        for _ in range(self.weights.weight(name).shape[0])
                    ),
                    calibration_samples=8,
                    dynamic_range_mode="none",
                )
                for name in LAYER_ORDER
            ),
        )
        uniform = downgrade_v3_to_v1(uniform_params)
        channel_drops = [
            item for item in uniform.drops if item.field == "channel_scales"
        ]
        self.assertTrue(channel_drops)
        self.assertFalse(any(item.semantic_loss for item in channel_drops))


if __name__ == "__main__":
    unittest.main()

