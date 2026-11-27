import unittest

from subpixel.generator import SEED, generate_project
from subpixel.models import summary
from subpixel.validation import validate_curve, validate_project


class ValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = validate_project(generate_project(SEED))

    def test_deterministic_output(self):
        again = validate_project(generate_project(SEED))
        self.assertEqual(
            [c.curve_id for c in again.curves],
            [c.curve_id for c in self.project.curves],
        )
        self.assertEqual(
            [c.radius_px for c in again.curves],
            [c.radius_px for c in self.project.curves],
        )

    def test_expected_inconsistent_curves(self):
        info = summary(self.project)
        self.assertEqual(info["invalid_curve_count"], 10)
        codes = {issue.code for issue in self.project.issues}
        self.assertEqual(
            codes,
            {"E_START_OUTSIDE", "E_TANGENT_NORM",
             "E_LENGTH_MISMATCH", "E_POINT_OUTSIDE"},
        )

    def test_invalid_curves_excluded_from_rendering(self):
        invalid = {issue.curve_id for issue in self.project.issues}
        rendered = {curve.curve_id for curve in self.project.valid_curves()}
        self.assertTrue(invalid.isdisjoint(rendered))

    def test_clean_curve_passes(self):
        curve = next(c for c in self.project.curves if c.curve_id == "FB-0001")
        self.assertEqual(validate_curve(curve, self.project), [])


if __name__ == "__main__":
    unittest.main()
