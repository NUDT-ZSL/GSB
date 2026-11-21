from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lockdiag.errors import DiagnosticError
from lockdiag.generator import generate_sample
from lockdiag.util import pretty_json
from lockdiag.validation import load_and_validate


ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, value) -> None:
    path.write_text(pretty_json(value) + "\n", encoding="utf-8", newline="\n")


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name) / "sample"
        generate_sample(cls.base)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _variant(self, name: str) -> Path:
        target = Path(self.temp.name) / f"variant-{name}"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(self.base, target)
        return target

    def _expect(self, mutate, code: str) -> None:
        sample = self._variant(code)
        config = json.loads((sample / "config.json").read_text(encoding="utf-8"))
        transactions = json.loads(
            (sample / "transactions.json").read_text(encoding="utf-8")
        )
        schema = json.loads((sample / "schema.json").read_text(encoding="utf-8"))
        mutate(config, transactions, schema)
        _write(sample / "config.json", config)
        _write(sample / "transactions.json", transactions)
        _write(sample / "schema.json", schema)
        with self.assertRaises(DiagnosticError) as context:
            load_and_validate(sample)
        self.assertEqual(context.exception.code, code)

    def test_valid_sample(self) -> None:
        bundle = load_and_validate(self.base)
        self.assertEqual(len(bundle.transactions), 5)
        self.assertEqual(bundle.config.max_concurrent_transactions, 2)
        self.assertEqual(len(bundle.clock.samples), 50_000)

    def test_sample_generation_is_byte_deterministic(self) -> None:
        first = Path(self.temp.name) / "deterministic-a"
        second = Path(self.temp.name) / "deterministic-b"
        generate_sample(first)
        generate_sample(second)
        for name in (
            "sample.db",
            "schema.json",
            "transactions.json",
            "config.json",
            "clock.json",
            "manifest.json",
        ):
            self.assertEqual(
                (first / name).read_bytes(),
                (second / name).read_bytes(),
                name,
            )

    def test_missing_table(self) -> None:
        self._expect(
            lambda c, t, s: t["transactions"][0]["operations"][0].update(
                {"table": "missing_table"}
            ),
            "E_NO_TABLE",
        )

    def test_missing_column(self) -> None:
        self._expect(
            lambda c, t, s: t["transactions"][0]["operations"][0].update(
                {"column": "missing_column"}
            ),
            "E_NO_COLUMN",
        )

    def test_bad_budget(self) -> None:
        self._expect(
            lambda c, t, s: c.update({"default_lock_wait_budget_ms": 0}),
            "E_BAD_BUDGET",
        )

    def test_bad_concurrency(self) -> None:
        self._expect(
            lambda c, t, s: c.update({"max_concurrent_transactions": -1}),
            "E_BAD_CONCURRENCY",
        )

    def test_bad_scale(self) -> None:
        self._expect(lambda c, t, s: c.update({"fixed_scale": 0}), "E_BAD_SCALE")

    def test_bad_tolerance(self) -> None:
        self._expect(
            lambda c, t, s: c.update({"wait_tolerance_ms": "0"}), "E_BAD_TOLERANCE"
        )

    def test_bad_lock_mode(self) -> None:
        self._expect(
            lambda c, t, s: t["transactions"][0]["operations"][0].update(
                {"lock": "bad-mode"}
            ),
            "E_BAD_LOCK_MODE",
        )

    def test_bad_isolation(self) -> None:
        self._expect(
            lambda c, t, s: t["transactions"][0].update(
                {"isolation": "READ_UNCOMMITTED"}
            ),
            "E_BAD_ISOLATION",
        )

    def test_transaction_cycle(self) -> None:
        def mutate(config, transactions, schema):
            transactions["transactions"][0]["depends_on"] = ["T2"]
            transactions["transactions"][1]["depends_on"] = ["T1"]

        self._expect(mutate, "E_TX_CYCLE")

    def test_duplicate_table(self) -> None:
        self._expect(
            lambda c, t, s: s["tables"].append(dict(s["tables"][0])),
            "E_DUP_TABLE",
        )

    def test_duplicate_index(self) -> None:
        self._expect(
            lambda c, t, s: s["indexes"].append(dict(s["indexes"][0])),
            "E_DUP_INDEX",
        )

    def test_bad_operation_order(self) -> None:
        self._expect(
            lambda c, t, s: t["transactions"][0]["operations"][0].update(
                {"op_seq": 9}
            ),
            "E_BAD_OPERATION_ORDER",
        )

    def test_invalid_config_does_not_create_state(self) -> None:
        sample = self._variant("no-state")
        config = json.loads((sample / "config.json").read_text(encoding="utf-8"))
        config["default_lock_wait_budget_ms"] = 0
        _write(sample / "config.json", config)
        state = Path(self.temp.name) / "invalid-state"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "lockdiag",
                "run",
                "--sample",
                str(sample),
                "--state",
                str(state),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("E_BAD_BUDGET", completed.stdout)
        self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
