import unittest

from ocr_pipeline.errors import ConfigError
from ocr_pipeline.templates import CHARSETS, build_library


class ContractTests(unittest.TestCase):
    def test_unknown_charset_raises_config_error(self):
        with self.assertRaises(ConfigError) as caught:
            build_library("does-not-exist", 1)
        self.assertEqual(caught.exception.code, "E_CONFIG")

    def test_charset_v2_is_a_shrink_of_v1(self):
        self.assertLess(len(CHARSETS["cs-v2"]), len(CHARSETS["cs-v1"]))
        self.assertTrue(set(CHARSETS["cs-v2"]).issubset(set(CHARSETS["cs-v1"])))


if __name__ == "__main__":
    unittest.main()
