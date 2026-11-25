from __future__ import annotations

import copy
import unittest

from radapt.errors import RadaptError
from radapt.parsing import parse_bundle
from radapt.sample import sample_raw


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = sample_raw()

    def _parse(self, raw=None):
        raw = raw or self.raw
        return parse_bundle(raw["table"], raw["plan"], raw["arrivals"], raw["config"])

    def test_valid_sample_parses(self) -> None:
        bundle = self._parse()
        self.assertEqual([w.window_id for w in bundle.plan.windows], ["w1", "w2", "w3"])
        self.assertEqual(bundle.config.fixed_point_scale, 1_000_000)

    def assert_code(self, mutate, expected_code: str) -> None:
        raw = copy.deepcopy(self.raw)
        mutate(raw)
        with self.assertRaises(RadaptError) as ctx:
            self._parse(raw)
        self.assertEqual(ctx.exception.code, expected_code)

    def test_duplicate_table(self) -> None:
        def mutate(raw):
            raw["table"]["tables"].append(copy.deepcopy(raw["table"]["tables"][0]))
        self.assert_code(mutate, "E_TABLE_NAME_DUPLICATE")

    def test_bad_column_type(self) -> None:
        def mutate(raw):
            raw["table"]["tables"][0]["columns"][0]["type"] = "not-a-type"
        self.assert_code(mutate, "E_COLUMN_TYPE_INVALID")

    def test_bad_window_param(self) -> None:
        def mutate(raw):
            raw["table"]["tables"][0]["window"]["size_ms"] = 0
        self.assert_code(mutate, "E_WINDOW_PARAM_INVALID")

    def test_bad_skew_ratio(self) -> None:
        for bad in ("0", "1", "-0.1", "1.2"):
            with self.subTest(bad=bad):
                self.assert_code(lambda raw, bad=bad: raw["table"]["tables"][0].__setitem__("skew_ratio", bad), "E_SKEW_RATIO_INVALID")

    def test_seq_must_increase(self) -> None:
        def mutate(raw):
            raw["arrivals"]["batches"][1]["seq"] = 1
        self.assert_code(mutate, "E_BATCH_SEQ_NOT_STRICTLY_INCREASING")

    def test_bad_clock(self) -> None:
        def mutate(raw):
            raw["arrivals"]["batches"][0]["arrival_ms"] = -1
        self.assert_code(mutate, "E_LOGIC_CLOCK_INVALID")

    def test_bad_threshold(self) -> None:
        for bad in ("0", "1", "2"):
            self.assert_code(lambda raw, bad=bad: raw["config"].__setitem__("drift_threshold", bad), "E_DRIFT_THRESHOLD_INVALID")

    def test_bad_parallel_limit(self) -> None:
        self.assert_code(lambda raw: raw["config"].__setitem__("max_parallel_tasks", 0), "E_PARALLEL_TASK_LIMIT_INVALID")

    def test_bad_logical_budget(self) -> None:
        self.assert_code(lambda raw: raw["config"].__setitem__("logical_time_budget_per_round_ms", -5), "E_LOGICAL_TIME_BUDGET_INVALID")

    def test_bad_scale(self) -> None:
        self.assert_code(lambda raw: raw["config"].__setitem__("fixed_point_scale", 0), "E_FIXED_POINT_SCALE_INVALID")

    def test_unknown_batch_window(self) -> None:
        self.assert_code(lambda raw: raw["arrivals"]["batches"][0].__setitem__("window_id", "nope"), "E_WINDOW_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
