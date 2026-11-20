from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from leasecoord.engine import Engine
from leasecoord.errors import BusinessError

from .helpers import load_sample, sample_documents


class ValidationTests(unittest.TestCase):
    def test_invalid_entries_do_not_create_persistent_state(self) -> None:
        mutations = [
            ("E_DUP_NODE", lambda nodes, cfg: nodes["nodes"].append(dict(nodes["nodes"][0]))),
            ("E_BAD_PRIORITY", lambda nodes, cfg: nodes["nodes"][0].__setitem__("priority", -1)),
            (
                "E_BAD_LEASE",
                lambda nodes, cfg: nodes["nodes"][0].__setitem__("lease_ms", 200),
            ),
            (
                "E_BAD_HEARTBEAT",
                lambda nodes, cfg: nodes["nodes"][0].__setitem__("heartbeat_ms", 0),
            ),
            (
                "E_BAD_SESSION",
                lambda nodes, cfg: nodes["nodes"][0].__setitem__("session_epoch", -1),
            ),
            (
                "E_BAD_DATA_VERSION",
                lambda nodes, cfg: nodes["nodes"][0].__setitem__("data_version", -1),
            ),
            (
                "E_BAD_SAFETY_MARGIN",
                lambda nodes, cfg: cfg.__setitem__("safety_margin_ratio", 1),
            ),
            (
                "E_BAD_QUORUM",
                lambda nodes, cfg: cfg.__setitem__("quorum", 4),
            ),
            (
                "E_BAD_MAX_LEASES",
                lambda nodes, cfg: cfg.__setitem__("max_leases", 0),
            ),
        ]
        for expected, mutate in mutations:
            with self.subTest(expected=expected), TemporaryDirectory() as temp:
                nodes, config = sample_documents()
                mutate(nodes, config)
                workspace = Path(temp) / "workspace"
                with self.assertRaises(BusinessError) as caught:
                    Engine(workspace).load(nodes, config, clock="0", reset=True)
                self.assertEqual(caught.exception.code, expected)
                self.assertFalse(workspace.exists())
                self.assertFalse((workspace / "coord.db").exists())

    def test_bad_quorum_above_lease_cap(self) -> None:
        with TemporaryDirectory() as temp:
            nodes, config = sample_documents()
            config["max_leases"] = 2
            config["quorum"] = 3
            with self.assertRaises(BusinessError) as caught:
                Engine(Path(temp) / "ws").load(nodes, config, clock="0")
            self.assertEqual(caught.exception.code, "E_BAD_QUORUM")

    def test_session_epoch_must_be_monotonic_across_loads(self) -> None:
        with TemporaryDirectory() as temp:
            workspace = Path(temp) / "ws"
            engine, nodes, config = load_sample(workspace)
            before = engine.inspect_database()
            nodes["nodes"][0]["session_epoch"] = 0
            with self.assertRaises(BusinessError) as caught:
                engine.load(nodes, config, clock="0", reset=True)
            self.assertEqual(caught.exception.code, "E_SESSION_REGRESSION")
            self.assertEqual(engine.inspect_database(), before)

    def test_bad_scale_and_rounding_are_explicit(self) -> None:
        for field, value, code in (
            ("scale", 12345, "E_BAD_SCALE"),
            ("rounding", "half_up", "E_BAD_ROUNDING"),
        ):
            with self.subTest(field=field), TemporaryDirectory() as temp:
                nodes, config = sample_documents()
                config[field] = value
                with self.assertRaises(BusinessError) as caught:
                    Engine(Path(temp) / "ws").load(nodes, config, clock="0")
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
