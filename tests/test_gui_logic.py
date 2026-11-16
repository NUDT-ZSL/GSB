from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joinlab.catalog import load_catalog
from joinlab.errors import JoinLabError
from joinlab.gui import RefreshGate, parse_float, parse_order_bias
from joinlab.session import ExperimentSession


class GuiLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog(ROOT / "samples" / "catalog.json")

    def test_refresh_gate_rejects_old_serial_and_old_version(self) -> None:
        session = ExperimentSession(self.catalog)
        gate = RefreshGate()
        old = gate.issue(session.versions)
        new = gate.issue(session.versions)
        self.assertFalse(gate.accepts(old, session))
        self.assertTrue(gate.accepts(new, session))
        session.update_cost_params({"scan_row_cost": 1.5})
        self.assertFalse(gate.accepts(new, session))
        latest = gate.issue(session.versions)
        self.assertTrue(gate.accepts(latest, session))

    def test_parameter_text_parsing(self) -> None:
        self.assertEqual(parse_float("1.25", "x"), 1.25)
        self.assertEqual(parse_order_bias("orders=2.5, customers=-1"), {"orders": 2.5, "customers": -1.0})
        with self.assertRaises(JoinLabError):
            parse_float("nope", "x")
        with self.assertRaises(JoinLabError):
            parse_order_bias("orders:2")


if __name__ == "__main__":
    unittest.main()
