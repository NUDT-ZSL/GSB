from __future__ import annotations

import unittest

from sampling_compat.config_io import read_config
from sampling_compat.errors import BusinessError, ErrorCode
from sampling_compat.migration import downgrade_v2_to_v1, upgrade_v1_to_v2


class MigrationTests(unittest.TestCase):
    def test_upgrade_records_and_roundtrips(self) -> None:
        source = read_config(
            '{"version":"v1","temperature":"7.0e-1","top_k":8}'
        )
        result = upgrade_v1_to_v2(
            source,
            default_vocab_version="tok-2024-01",
        )
        self.assertFalse(result.lossy)
        self.assertEqual(
            [item["field"] for item in result.added_fields],
            ["top_p", "repetition_penalty", "vocab_version"],
        )
        target = read_config(result.output_text)
        self.assertEqual(target.version, "v2")
        self.assertEqual(target.top_p, 1.0)
        self.assertEqual(target.repetition_penalty, 1.0)
        self.assertEqual(target.vocab_version, "tok-2024-01")
        self.assertEqual(target.temperature_literal, "7.0e-1")

    def test_downgrade_records_drops_quantization_and_losses(self) -> None:
        source = read_config(
            '{"version":"v2",'
            '"temperature":{"value":"1.2500004","unit":"ratio"},'
            '"top_k":{"value":12,"unit":"tokens"},'
            '"top_p":{"value":"0.85","unit":"probability"},'
            '"repetition_penalty":{"value":"1.1","unit":"multiplier"},'
            '"vocab_version":"tok-2025-02"}'
        )
        result = downgrade_v2_to_v1(source)
        self.assertTrue(result.lossy)
        target = read_config(result.output_text)
        self.assertEqual(target.version, "v1")
        self.assertIsNone(target.top_p)
        self.assertIsNone(target.repetition_penalty)
        self.assertEqual(target.temperature_literal, "1.250000")
        self.assertEqual(
            [item["field"] for item in result.dropped_fields],
            ["top_p", "repetition_penalty", "vocab_version", "field_units"],
        )
        self.assertEqual(
            [item["field"] for item in result.losses],
            ["top_p", "repetition_penalty", "temperature"],
        )

    def test_temperature_quantization_is_half_up(self) -> None:
        source = read_config(
            '{"version":"v2",'
            '"temperature":{"value":"1.2345675","unit":"ratio"},'
            '"top_k":{"value":8,"unit":"tokens"},'
            '"top_p":{"value":"1.0","unit":"probability"},'
            '"repetition_penalty":{"value":"1.0","unit":"multiplier"},'
            '"vocab_version":"tok-2024-01"}'
        )
        result = downgrade_v2_to_v1(source)
        self.assertEqual(result.quantized_fields[0]["after"], "1.234568")
        self.assertTrue(result.quantized_fields[0]["changed"])
        self.assertEqual(result.losses[-1]["field"], "temperature")

    def test_downgrade_default_values_have_no_sampling_loss(self) -> None:
        source = read_config(
            '{"version":"v2",'
            '"temperature":{"value":"0.7","unit":"ratio"},'
            '"top_k":{"value":8,"unit":"tokens"},'
            '"top_p":{"value":"1.0","unit":"probability"},'
            '"repetition_penalty":{"value":"1.0","unit":"multiplier"},'
            '"vocab_version":"tok-2024-01"}'
        )
        result = downgrade_v2_to_v1(source)
        self.assertFalse(result.lossy)
        self.assertTrue(all(not item["semantic_loss"] for item in result.dropped_fields))

    def test_wrong_source_versions_are_rejected(self) -> None:
        v2 = read_config(
            '{"version":"v2",'
            '"temperature":{"value":"0.7","unit":"ratio"},'
            '"top_k":{"value":8,"unit":"tokens"},'
            '"top_p":{"value":"1.0","unit":"probability"},'
            '"repetition_penalty":{"value":"1.0","unit":"multiplier"},'
            '"vocab_version":"tok-2024-01"}'
        )
        with self.assertRaises(BusinessError) as raised:
            upgrade_v1_to_v2(v2, default_vocab_version="tok-2024-01")
        self.assertEqual(raised.exception.code, ErrorCode.VERSION_UNSUPPORTED)
        v1 = read_config('{"version":"v1","temperature":"0.7","top_k":8}')
        with self.assertRaises(BusinessError) as raised:
            downgrade_v2_to_v1(v1)
        self.assertEqual(raised.exception.code, ErrorCode.VERSION_UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
