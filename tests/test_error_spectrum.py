import unittest

from xrel.config import default_config
from xrel.data_gen import generate_bridge, generate_documents, generate_queries
from xrel.error_spectrum import StageError, build_spectrum
from xrel.errors import ErrorSpectrumInvalid
from xrel.numerics import make_backend
from xrel.pipeline import XrelPipeline
from xrel.snapshot import attach_checksum
from xrel.scoring import DocScore
import tempfile
from pathlib import Path


class SpectrumValidationTests(unittest.TestCase):
    def test_diverging_hit_sets_raise_spectrum_error(self):
        reference = {"d1": DocScore("d1", "en", 1.0)}
        observed = {"d2": DocScore("d2", "en", 1.0)}
        with self.assertRaises(ErrorSpectrumInvalid) as ctx:
            build_spectrum(
                "float32", reference, observed, make_backend("float32")
            )
        self.assertEqual(ctx.exception.code, "E_ERR_SPECTRUM_INVALID")

    def test_invalid_interval_order_is_rejected(self):
        from xrel.error_spectrum import _validate_intervals

        good = StageError("s", 1.0, 0.1, 0.0, 2.0, 1.0, 1.0, 1.0)
        bad = StageError("s", 1.0, 0.1, 2.0, 0.0, 1.0, 1.0, 1.0)
        _validate_intervals("d1", [good])
        with self.assertRaises(ErrorSpectrumInvalid):
            _validate_intervals("d1", [bad])

    def test_pipeline_spectrum_is_valid_for_all_tiers(self):
        bridge = attach_checksum(generate_bridge(7, 1))
        documents = generate_documents(7)
        queries = generate_queries(7)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = XrelPipeline(
                Path(tmp) / "snap", Path(tmp) / "res", default_config("float64")
            )
            pipeline.load_inputs(documents, queries, bridge)
            for precision in ("float32", "fixed"):
                record = pipeline.query_by_id("q-000", precision)
                spectrum = record.result["error_spectrum"]
                self.assertTrue(spectrum["dominant_stage"])
                for stages in spectrum["per_doc"].values():
                    self.assertEqual(len(stages), 4)


if __name__ == "__main__":
    unittest.main()
