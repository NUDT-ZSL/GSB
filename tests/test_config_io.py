from __future__ import annotations

import unittest

from sampling_compat.config_io import (
    read_config,
    read_config_file,
    serialize_v2,
    serialize_v3,
)
from sampling_compat.errors import BusinessError, ErrorCode
from tests.helpers import SAMPLES


class ConfigReaderTests(unittest.TestCase):
    def test_v1_preserves_temperature_literal(self) -> None:
        config = read_config(
            '{"version":"v1","temperature":" 7.00e-1 ","top_k":8}'
        )
        self.assertEqual(config.version, "v1")
        self.assertEqual(config.temperature, 0.7)
        self.assertEqual(config.temperature_literal, " 7.00e-1 ")
        self.assertEqual(config.top_k, 8)
        self.assertIsNone(config.top_p)
        self.assertIsNone(config.repetition_penalty)

    def test_v2_unit_objects(self) -> None:
        text = r'''
        {
          "version": "v2",
          "temperature": {"value": "7.0e-1", "unit": "ratio"},
          "top_k": {"value": "8.0e0", "unit": "tokens"},
          "top_p": {"value": "8.5e-1", "unit": "probability"},
          "repetition_penalty": {"value": "1.1e0", "unit": "multiplier"},
          "vocab_version": "tok-2024-01"
        }
        '''
        config = read_config(text)
        self.assertEqual(config.version, "v2")
        self.assertEqual(config.temperature, 0.7)
        self.assertEqual(config.top_k, 8.0)
        self.assertEqual(config.top_p, 0.85)
        self.assertEqual(config.repetition_penalty, 1.1)
        self.assertEqual(config.units["top_k"], "tokens")
        self.assertEqual(config.literal_values["top_p"], "8.5e-1")

    def test_v3_line_format_scientific_notation(self) -> None:
        text = (
            "!sampling-config v3\n"
            "temperature = 1.25e+0 ratio\n"
            "top_k = 1.2e1 tokens\n"
            "top_p = 8.50e-1 probability\n"
            "repetition_penalty = 1.10e0 multiplier\n"
            "vocab_version = tok-2025-02\n"
        )
        config = read_config(text)
        self.assertEqual(config.version, "v3")
        self.assertEqual(config.top_k, 12.0)
        self.assertEqual(config.vocab_version, "tok-2025-02")
        self.assertEqual(config.source_format, "line-kv")

    def test_v2_and_v3_roundtrip(self) -> None:
        config = read_config(
            "!sampling-config v3\n"
            "temperature = 7.0e-1 ratio\n"
            "top_k = 8 tokens\n"
            "top_p = 1.0 probability\n"
            "repetition_penalty = 1.0 multiplier\n"
            "vocab_version = tok-2024-01\n"
        )
        for output in (serialize_v2(config), serialize_v3(config)):
            roundtrip = read_config(output)
            self.assertEqual(roundtrip.temperature, config.temperature)
            self.assertEqual(roundtrip.top_k, config.top_k)
            self.assertEqual(roundtrip.top_p, config.top_p)
            self.assertEqual(roundtrip.vocab_version, config.vocab_version)

    def test_unknown_version(self) -> None:
        path = SAMPLES / "errors" / "configs" / "unknown_version_v3.txt"
        with self.assertRaises(BusinessError) as raised:
            read_config_file(path)
        self.assertEqual(raised.exception.code, ErrorCode.VERSION_UNSUPPORTED)
        self.assertEqual(raised.exception.field, "version")
        self.assertEqual(raised.exception.version, "v9")

    def test_truncated_v3_header(self) -> None:
        path = SAMPLES / "errors" / "configs" / "truncated_header_v3.txt"
        with self.assertRaises(BusinessError) as raised:
            read_config_file(path)
        self.assertEqual(raised.exception.code, ErrorCode.FORMAT_CORRUPT)
        self.assertEqual(raised.exception.field, "header")

    def test_missing_v2_field_names_field(self) -> None:
        path = SAMPLES / "errors" / "configs" / "missing_top_p_v2.json"
        with self.assertRaises(BusinessError) as raised:
            read_config_file(path)
        self.assertEqual(raised.exception.code, ErrorCode.FORMAT_CORRUPT)
        self.assertEqual(raised.exception.field, "top_p")

    def test_missing_version_header(self) -> None:
        path = SAMPLES / "errors" / "configs" / "truncated_header_v2.json"
        with self.assertRaises(BusinessError) as raised:
            read_config_file(path)
        self.assertEqual(raised.exception.code, ErrorCode.FORMAT_CORRUPT)
        self.assertEqual(raised.exception.field, "version")

    def test_unparseable_temperature_preserves_literal(self) -> None:
        path = SAMPLES / "errors" / "configs" / "bad_numeric_temperature_v1.json"
        with self.assertRaises(BusinessError) as raised:
            read_config_file(path)
        self.assertEqual(raised.exception.code, ErrorCode.FORMAT_CORRUPT)
        self.assertEqual(raised.exception.field, "temperature")
        self.assertEqual(raised.exception.original_value, "warm-ish")

    def test_v1_field_set_rejects_v2_field(self) -> None:
        text = (
            '{"version":"v1","temperature":"0.7","top_k":8,"top_p":0.9}'
        )
        with self.assertRaises(BusinessError) as raised:
            read_config(text)
        self.assertEqual(raised.exception.code, ErrorCode.FORMAT_CORRUPT)
        self.assertEqual(raised.exception.field, "top_p")

    def test_v2_unit_mismatch(self) -> None:
        text = (
            '{"version":"v2",'
            '"temperature":{"value":"0.7","unit":"kelvin"},'
            '"top_k":{"value":8,"unit":"tokens"},'
            '"top_p":{"value":"1.0","unit":"probability"},'
            '"repetition_penalty":{"value":"1.0","unit":"multiplier"},'
            '"vocab_version":"tok-2024-01"}'
        )
        with self.assertRaises(BusinessError) as raised:
            read_config(text)
        self.assertEqual(raised.exception.code, ErrorCode.FORMAT_CORRUPT)
        self.assertEqual(raised.exception.field, "temperature.unit")


if __name__ == "__main__":
    unittest.main()
