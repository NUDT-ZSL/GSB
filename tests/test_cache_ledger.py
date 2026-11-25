from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cache_ledger.cli import main
from cache_ledger.errors import CacheLedgerError
from cache_ledger.merge import build_merged_view, compute_closure
from cache_ledger.models import load_bundle
from cache_ledger.runner import execute_command
from cache_ledger.samplegen import generate_samples
from cache_ledger.store import LedgerStore


ROOT = Path(__file__).resolve().parents[1]


class CacheLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)
        self.samples = self.tmp / "samples"
        self.db = self.tmp / "state" / "ledger.sqlite3"
        generate_samples(self.samples)
        self.bundle = load_bundle(self.samples)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(self, *args: str) -> int:
        return main([*args, "--db", str(self.db), "--samples", str(self.samples)])

    def test_sample_validation_and_merge_adjudication(self) -> None:
        self.assertEqual(36, len(self.bundle.entries))
        self.assertEqual(3, len(self.bundle.tenants))
        view = build_merged_view(self.bundle, 18_000)
        policies = view["policies"]
        self.assertEqual("v1", policies["cache-0014"]["source"])
        self.assertEqual(21_000, policies["cache-0014"]["effective_at_ms"])
        self.assertEqual("v2", policies["cache-0015"]["source"])
        self.assertIn("source priority", policies["cache-0015"]["decision_basis"])
        v1_conversion = policies["cache-0014"]["candidates"][0]["time_conversion"]
        self.assertEqual(21_000, v1_conversion["absolute_ms"])
        self.assertEqual(8, v1_conversion["sub_tick_remainder_ms"])
        self.assertTrue(v1_conversion["remainder_participates_in_comparison"])

    def test_invalid_digest_is_rejected_before_load(self) -> None:
        entries = self.samples / "entries.jsonl"
        lines = entries.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["digest"] = "0" * 64
        lines[0] = json.dumps(row, sort_keys=True)
        entries.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(CacheLedgerError) as ctx:
            load_bundle(self.samples)
        self.assertEqual("E_DIGEST_MISMATCH", ctx.exception.code)

    def _run_canonical_in_process(self, db: Path, *, patch: bool = False) -> LedgerStore:
        rc = main(["load", "--db", str(db), "--samples", str(self.samples)])
        self.assertEqual(0, rc)
        store = LedgerStore(db)
        patch_path = str(self.samples / "policy_update_example.json") if patch else None
        execute_command(store, self.bundle, {
            "command": "merge", "clock_ms": 18_000, "patch_path": patch_path,
        })
        execute_command(store, self.bundle, {"command": "admit", "clock_ms": 42_000})
        execute_command(store, self.bundle, {"command": "rebalance", "clock_ms": 48_000})
        execute_command(store, self.bundle, {"command": "evict", "clock_ms": 49_000})
        return store

    def test_canonical_flow_is_deterministic_and_verifies(self) -> None:
        first = self._run_canonical_in_process(self.db)
        status_first = first.status()
        decisions_first = first.decisions()
        first.close()

        second_db = self.tmp / "second.sqlite3"
        second = self._run_canonical_in_process(second_db)
        self.assertEqual(
            json.dumps(status_first, sort_keys=True, default=str),
            json.dumps(second.status(), sort_keys=True, default=str),
        )
        self.assertEqual(
            [d["payload"] for d in decisions_first],
            [d["payload"] for d in second.decisions()],
        )
        second.close()
        self.assertEqual(0, self.cli("verify"))

    def test_clock_rollback_has_independent_code_and_no_new_decision(self) -> None:
        store = self._run_canonical_in_process(self.db)
        before = store.status()["decision_count"]
        store.close()
        rc = self.cli("admit", "--clock-ms", "1000")
        self.assertEqual(10, rc)
        store = LedgerStore(self.db)
        self.assertEqual(before, store.status()["decision_count"])
        self.assertEqual(
            0,
            store.conn.execute("SELECT COUNT(*) AS n FROM command_log WHERE status!='completed'").fetchone()["n"],
        )
        self.assertGreaterEqual(store.status()["clock_watermark_ms"], 49_000)
        store.close()

    def test_start_failure_is_distinguished_from_abandonment(self) -> None:
        rc = self.cli("load")
        self.assertEqual(0, rc)
        self.assertEqual(0, self.cli("merge"))
        self.assertEqual(0, self.cli("admit"))
        report = self.tmp / "rebalance.json"
        rc = self.cli("rebalance", "--fail-key", "cache-0012", "--report", str(report))
        self.assertEqual(0, rc)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["counts"]["failed_after_start"])
        self.assertEqual("E_INJECTED_START_FAILURE", payload["failed_after_start"][0]["error_code"])
        self.assertGreater(payload["counts"]["abandoned"], 0)
        self.assertEqual("BUDGET_EXHAUSTED", payload["abandoned"][0]["reason"])

    def test_incremental_closure_reuses_unaffected_entries(self) -> None:
        store_a = self._run_canonical_in_process(self.db)
        view_a = json.loads(store_a.get_meta("policy_pointer_fingerprint") and store_a.conn.execute(
            "SELECT view_json FROM policy_versions WHERE fingerprint=?",
            (store_a.get_meta("policy_pointer_fingerprint"),),
        ).fetchone()["view_json"])
        layers_a = {
            row["key"]: row["current_layer"]
            for row in store_a.conn.execute("SELECT key,current_layer FROM entries")
        }
        state_a = {
            row["key"]: (row["status"], row["current_layer"])
            for row in store_a.conn.execute("SELECT key,status,current_layer FROM entries")
        }
        store_a.close()

        db_b = self.tmp / "patched.sqlite3"
        store_b = self._run_canonical_in_process(db_b, patch=True)
        view_b = json.loads(store_b.conn.execute(
            "SELECT view_json FROM policy_versions WHERE fingerprint=?",
            (store_b.get_meta("policy_pointer_fingerprint"),),
        ).fetchone()["view_json"])
        closure = compute_closure(
            self.bundle,
            view_a["policies"],
            view_b,
            current_layers=layers_a,
            extra_changed_keys={"cache-0001"},
        )
        self.assertIn("cache-0001", closure["recompute_entry_keys"])
        self.assertGreater(closure["reused_count"], 0)
        state_b = {
            row["key"]: (row["status"], row["current_layer"])
            for row in store_b.conn.execute("SELECT key,status,current_layer FROM entries")
        }
        for key in closure["reused_entry_keys"]:
            self.assertEqual(state_a[key], state_b[key], f"reused entry changed: {key}")
        store_b.close()

    def test_prepared_half_commit_recovers_and_verifies(self) -> None:
        self.assertEqual(0, self.cli("load"))
        self.assertEqual(0, self.cli("merge"))
        env = dict(os.environ)
        env["CACHE_LEDGER_CRASH_AFTER_PREPARE_KEY"] = "admit:cache-0001"
        crashed = subprocess.run(
            [sys.executable, "-m", "cache_ledger", "admit", "--db", str(self.db), "--samples", str(self.samples)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(86, crashed.returncode)
        store = LedgerStore(self.db)
        self.assertEqual(1, len(store.pending_operations("prepared")))
        self.assertEqual(1, len(store.running_commands()))
        store.close()

        self.assertEqual(0, self.cli("resume"))
        self.assertEqual(0, self.cli("rebalance"))
        self.assertEqual(0, self.cli("evict"))
        self.assertEqual(0, self.cli("verify"))

    def test_parallel_tenant_admission_is_unique_and_consistent(self) -> None:
        self.assertEqual(0, self.cli("load"))
        self.assertEqual(0, self.cli("merge"))
        processes = []
        for tenant in ("alpha", "beta", "gamma"):
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "cache_ledger", "admit", "--tenant", tenant,
                 "--db", str(self.db), "--samples", str(self.samples)],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            ))
        errors = []
        for process in processes:
            _out, err = process.communicate(timeout=60)
            if process.returncode != 0:
                errors.append(err.decode("utf-8", "replace"))
        self.assertEqual([], errors)
        store = LedgerStore(self.db)
        self.assertEqual(37, store.status()["decision_count"])
        keys = [row["idempotency_key"] for row in store.conn.execute("SELECT idempotency_key FROM decisions")]
        self.assertEqual(len(keys), len(set(keys)))
        store.close()
        self.assertEqual(0, self.cli("verify"))


if __name__ == "__main__":
    unittest.main()
