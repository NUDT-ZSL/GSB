from __future__ import annotations

import unittest

from quantlab.config import WorkbenchConfig
from quantlab.deterministic import generate_manifest
from quantlab.errors import BusinessError, E_MANIFEST_INVALID
from quantlab.manifest import manifest_bytes, parse_manifest_text


class ManifestTests(unittest.TestCase):
    def test_broken_shape_chain_is_rejected(self) -> None:
        entries = generate_manifest(WorkbenchConfig())
        entries[2]["layer_shape"] = [99, 99]
        with self.assertRaises(BusinessError) as captured:
            parse_manifest_text(manifest_bytes(entries).decode("utf-8"))
        error = captured.exception
        self.assertEqual(error.code, E_MANIFEST_INVALID)
        self.assertEqual(error.stage, "model_validation")
        self.assertEqual(error.layer_id, "layer_02")
        self.assertEqual(error.layer_index, 2)

    def test_malformed_json_line_is_rejected(self) -> None:
        with self.assertRaises(BusinessError) as captured:
            parse_manifest_text('{"layer_id": "broken"\n')
        self.assertEqual(captured.exception.code, E_MANIFEST_INVALID)
        self.assertEqual(captured.exception.stage, "model_validation")


if __name__ == "__main__":
    unittest.main()
