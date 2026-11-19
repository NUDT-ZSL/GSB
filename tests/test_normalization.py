from __future__ import annotations

import unittest

from sampling_compat.config_io import read_config
from sampling_compat.errors import BusinessError, ErrorCode
from sampling_compat.normalization import normalize_params


def v2_config(
    *,
    temperature: str = "0.7",
    top_k: str = "8",
    top_p: str = "1.0",
    repetition: str = "1.0",
) -> str:
    return (
        '{"version":"v2",'
        f'"temperature":{{"value":"{temperature}","unit":"ratio"}},'
        f'"top_k":{{"value":"{top_k}","unit":"tokens"}},'
        f'"top_p":{{"value":"{top_p}","unit":"probability"}},'
        f'"repetition_penalty":{{"value":"{repetition}","unit":"multiplier"}},'
        '"vocab_version":"tok-2024-01"}'
    )


class NormalizationTests(unittest.TestCase):
    def test_v1_defaults_are_explicit(self) -> None:
        config = read_config('{"version":"v1","temperature":"0.7","top_k":8}')
        normalized = normalize_params(config, vocabulary_size=32)
        self.assertEqual(normalized.top_p, 1.0)
        self.assertEqual(normalized.repetition_penalty, 1.0)
        self.assertEqual(
            {item["field"] for item in normalized.defaulted_fields},
            {"top_p", "repetition_penalty"},
        )

    def test_top_k_clamps_to_vocabulary_size(self) -> None:
        config = read_config(v2_config(top_k="99.0e0"))
        normalized = normalize_params(config, vocabulary_size=32)
        self.assertEqual(normalized.top_k, 32)
        self.assertEqual(normalized.clamped_fields[0]["before"], 99)
        self.assertEqual(normalized.clamped_fields[0]["after"], 32)

    def test_range_errors_name_field_and_original(self) -> None:
        cases = [
            ({"temperature": "0"}, "temperature", "0"),
            ({"temperature": "-0.1"}, "temperature", "-0.1"),
            ({"top_k": "0"}, "top_k", "0"),
            ({"top_k": "2.5"}, "top_k", "2.5"),
            ({"top_p": "0"}, "top_p", "0"),
            ({"top_p": "1.5"}, "top_p", "1.5"),
            ({"repetition": "0"}, "repetition_penalty", "0"),
        ]
        for overrides, expected_field, expected_literal in cases:
            with self.subTest(field=expected_field, value=expected_literal):
                config = read_config(v2_config(**overrides))
                with self.assertRaises(BusinessError) as raised:
                    normalize_params(config, vocabulary_size=32)
                self.assertEqual(raised.exception.code, ErrorCode.PARAM_RANGE)
                self.assertEqual(raised.exception.field, expected_field)
                self.assertEqual(
                    str(raised.exception.original_value),
                    expected_literal,
                )

    def test_top_p_one_is_no_truncation_sentinel(self) -> None:
        config = read_config(v2_config(top_p="1.0"))
        normalized = normalize_params(config, vocabulary_size=32)
        self.assertEqual(normalized.top_p, 1.0)
        self.assertIn("no-truncation sentinel", normalized.range_policy["top_p"])


if __name__ == "__main__":
    unittest.main()
