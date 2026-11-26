import unittest

from kfdiff.errors import E_BLOCK_MISMATCH, KFError
from kfdiff.selfcheck import run_selfcheck
from tests.helpers import generate_all, make_studio


class SelfCheckTests(unittest.TestCase):
    def test_selfcheck_passes_on_generated_workspace(self):
        studio, _ = make_studio()
        generate_all(studio)
        report = run_selfcheck(studio, raise_on_error=False)
        self.assertTrue(report["ok"], [c for c in report["checks"] if c["status"] == "fail"])
        self.assertEqual(report["checks_failed"], 0)

    def test_selfcheck_returns_e_inconsistent_on_failure(self):
        studio, _ = make_studio()
        generate_all(studio)

        def broken(_clip_id, **_kwargs):
            raise KFError(E_BLOCK_MISMATCH, "injected drift", {"x": 1})

        studio.adjudicate_consistency = broken
        with self.assertRaises(KFError) as ctx:
            run_selfcheck(studio)
        self.assertEqual(ctx.exception.code, "E_INCONSISTENT")

    def test_selfcheck_skips_ungenerated_clip_gracefully(self):
        studio, _ = make_studio()
        report = run_selfcheck(studio, clip_ids=["clip_0001"], raise_on_error=False)
        self.assertTrue(any(c["status"] == "skipped" for c in report["checks"]))


if __name__ == "__main__":
    unittest.main()
