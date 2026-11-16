import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from config_audit.errors import CycleError, PropagateError
from config_audit.fixedpoint import Grid
from config_audit.loader import load_archives
from config_audit.propagation import build_quantized


def _load(payload):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "a.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    archives = load_archives([path])
    return archives, tmp


ARCHIVE = {
    "node_id": "n", "version": "v1",
    "items": [
        {"key": "retry.count", "value": 3, "unit": "count",
         "source": "manual"},
        {"key": "http.timeout", "value": 1000, "unit": "ms",
         "source": "manual"},
        {"key": "backoff.budget", "unit": "ms", "source": "derived",
         "formula": {"coefficients": {"retry.count": 3}, "constant": 100}},
        {"key": "effective.timeout", "unit": "ms", "source": "derived",
         "formula": {"coefficients": {"http.timeout": 1,
                                      "backoff.budget": 2},
                     "constant": 0}},
    ],
}


class PropagationTests(unittest.TestCase):
    def test_multilevel_integer_propagation(self):
        archives, tmp = _load(ARCHIVE)
        self.addCleanup(tmp.cleanup)
        grid = Grid.create(3)
        q = build_quantized(archives, grid)
        # backoff = 3*3 + 100 = 109 -> 109000 grid steps
        self.assertEqual(q[("n", "backoff.budget")].qvalue, 109000)
        # effective = 1000 + 2*109 = 1218 -> 1218000 grid steps
        self.assertEqual(q[("n", "effective.timeout")].qvalue, 1218000)
        trace = q[("n", "effective.timeout")].trace["propagation"]
        # contributions recorded per upstream, in deterministic order
        self.assertEqual([c["upstream_key"] for c in trace["contributions"]],
                         ["backoff.budget", "http.timeout"])
        # weight |coef| = 2 for backoff
        backoff = trace["contributions"][0]
        self.assertEqual(backoff["weight_abs_coef"], 2)

    def test_cycle_detected(self):
        cyclic = {
            "node_id": "c", "version": "v1",
            "items": [
                {"key": "a", "value": 1, "unit": "count",
                 "source": "manual"},
                {"key": "b", "unit": "count", "source": "derived",
                 "formula": {"coefficients": {"a": 1, "c": 1},
                             "constant": 0}},
                {"key": "c", "unit": "count", "source": "derived",
                 "formula": {"coefficients": {"b": 1}, "constant": 0}},
            ],
        }
        archives, tmp = _load(cyclic)
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(CycleError) as ctx:
            build_quantized(archives, Grid.create(2))
        self.assertEqual(ctx.exception.code, "E_CYCLE")
        self.assertEqual(ctx.exception.key, "b")
        self.assertTrue(ctx.exception.locations)

    def test_propagate_error_on_missing_upstream(self):
        # Construct an archive model directly whose derived item points at an
        # upstream key that is absent from the item map (the E_PROPAGATE
        # break-point).  The JSON loader rejects this earlier, so the model is
        # assembled here to exercise the propagation guard.
        from decimal import Decimal
        from config_audit.models import Archive, ConfigItem, Formula
        formula = Formula(coefficients={"ghost.key": Decimal(1)},
                          constant=Decimal(0), upstream=("ghost.key",))
        derived = ConfigItem(key="d", value=None, unit="ms",
                             source="derived", formula=formula,
                             bounds=(Decimal(0), Decimal(1000)),
                             location="d/formula")
        archive = Archive(node_id="n", version="v1", items={"d": derived})
        with self.assertRaises(PropagateError) as ctx:
            build_quantized([archive], Grid.create(2))
        self.assertEqual(ctx.exception.code, "E_PROPAGATE")
        self.assertEqual(ctx.exception.key, "ghost.key")

    def test_quantization_error_recorded_per_input(self):
        # Non-integer coefficient forces a coefficient quantization residual.
        payload = {
            "node_id": "n", "version": "v1",
            "items": [
                {"key": "a", "value": 10, "unit": "ms", "source": "manual"},
                {"key": "d", "unit": "ms", "source": "derived",
                 "formula": {"coefficients": {"a": 0.5}, "constant": 0}},
            ],
        }
        archives, tmp = _load(payload)
        self.addCleanup(tmp.cleanup)
        q = build_quantized(archives, Grid.create(2))
        qi = q[("n", "d")]
        coef_rec = qi.trace["coef:a"]
        # 0.5 coefficient rounds to integer 0 (half-even), residual recorded
        self.assertEqual(coef_rec.quantized, 0)
        self.assertEqual(coef_rec.up_error + coef_rec.down_error, 1)


if __name__ == "__main__":
    unittest.main()
