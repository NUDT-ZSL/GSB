from __future__ import annotations

import subprocess
import sys
import unittest

from gateway.store import Store
from gateway.verify import verify_store
from tests.helpers import (
    ROOT,
    TempDirTestCaseMixin,
    config_doc,
    load_cli,
    request,
    request_doc,
    service_doc,
    write_json,
)


class ConcurrencyTests(TempDirTestCaseMixin, unittest.TestCase):
    def _spawn(self, args: list[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-m", "gateway", *args],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_parallel_processes_do_not_oversell_slots_or_tokens(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        services = service_doc(
            replicas=[{"replica_id": "r1", "slots": 3, "compute_units": 1}]
        )
        requests = request_doc([request(index) for index in range(1, 9)])
        loaded = load_cli(
            db,
            services,
            requests,
            config_doc(capacity=100, rate=100, max_inflight=100),
        )
        self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)

        processes = [
            self._spawn(
                [
                    "dispatch",
                    "--db",
                    str(db),
                    "--clock-ms",
                    "1",
                    "--round-id",
                    f"parallel-{index}",
                    "--max-launches",
                    "1",
                    "--budget-ms",
                    "100",
                ]
            )
            for index in range(8)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(
                process.returncode, 0, f"stdout={stdout}\nstderr={stderr}"
            )

        state, version, _ = Store(db).read_state()
        self.assertEqual(state["clock_watermark_ms"], 1)
        self.assertEqual(state["counters"]["dispatches"], 3)
        self.assertEqual(state["global_token"]["total_spent_fixed"], 3000)
        self.assertEqual(state["services"]["svc"]["token"]["total_spent_fixed"], 3000)
        self.assertEqual(state["global_token"]["balance_fixed"], 97000)
        inflight = [
            request_state
            for request_state in state["requests"].values()
            if request_state["status"] == "inflight"
        ]
        self.assertEqual(len(inflight), 3)
        for request_state in inflight:
            self.assertEqual(request_state["generation"], 1)
            self.assertEqual(len(request_state["attempts"]), 1)
            self.assertEqual(request_state["attempts"][0]["replica_id"], "r1")
        self.assertTrue(verify_store(Store(db))["verified"])
        self.assertGreater(version, 1)

    def test_duplicate_round_id_across_processes_dispatches_once(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        loaded = load_cli(
            db,
            service_doc(),
            request_doc([request(1)]),
            config_doc(capacity=100, rate=100),
        )
        self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)
        processes = [
            self._spawn(
                [
                    "dispatch",
                    "--db",
                    str(db),
                    "--clock-ms",
                    "1",
                    "--round-id",
                    "same-round",
                    "--max-launches",
                    "1",
                ]
            )
            for _ in range(8)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(
                process.returncode, 0, f"stdout={stdout}\nstderr={stderr}"
            )
        state, version, _ = Store(db).read_state()
        self.assertEqual(version, 2)
        self.assertEqual(state["counters"]["dispatches"], 1)
        self.assertEqual(state["global_token"]["total_spent_fixed"], 1000)
        self.assertEqual(state["requests"]["1"]["generation"], 1)
        self.assertTrue(verify_store(Store(db))["verified"])

    def test_concurrent_readers_observe_only_committed_pointer(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        loaded = load_cli(
            db,
            service_doc(),
            request_doc([request(1)]),
            config_doc(capacity=100, rate=100),
        )
        self.assertEqual(loaded.returncode, 0)
        writer = self._spawn(
            [
                "dispatch",
                "--db",
                str(db),
                "--clock-ms",
                "1",
                "--round-id",
                "write-race",
            ]
        )
        readers = [
            self._spawn(["status", "--db", str(db)]) for _ in range(6)
        ]
        for reader in readers:
            stdout, stderr = reader.communicate(timeout=60)
            self.assertEqual(reader.returncode, 0, stderr)
            self.assertIn('"ok":true', stdout)
        stdout, stderr = writer.communicate(timeout=60)
        self.assertEqual(writer.returncode, 0, stderr)
        state, _, _ = Store(db).read_state()
        self.assertEqual(state["counters"]["dispatches"], 1)

    def test_parallel_processes_do_not_oversell_global_tokens(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        services = service_doc(
            replicas=[{"replica_id": "r1", "slots": 8, "compute_units": 1}]
        )
        requests = request_doc([request(index) for index in range(1, 9)])
        loaded = load_cli(
            db,
            services,
            requests,
            config_doc(
                capacity=3,
                rate=1,
                max_inflight=100,
            ),
        )
        self.assertEqual(loaded.returncode, 0)
        processes = [
            self._spawn(
                [
                    "dispatch",
                    "--db",
                    str(db),
                    "--clock-ms",
                    "1",
                    "--round-id",
                    f"token-race-{index}",
                    "--max-launches",
                    "1",
                ]
            )
            for index in range(8)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(
                process.returncode, 0, f"stdout={stdout}\nstderr={stderr}"
            )
        state, _, _ = Store(db).read_state()
        self.assertEqual(state["counters"]["dispatches"], 3)
        self.assertEqual(state["global_token"]["total_spent_fixed"], 3000)
        self.assertEqual(state["global_token"]["balance_fixed"], 0)
        self.assertEqual(
            sum(
                1
                for request_state in state["requests"].values()
                if request_state["status"] == "inflight"
            ),
            3,
        )
        self.assertTrue(verify_store(Store(db))["verified"])

    def test_concurrent_duplicate_failure_writes_one_tombstone(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        loaded = load_cli(
            db,
            service_doc(),
            request_doc([request(1)]),
            config_doc(max_generation=1, capacity=100, rate=100),
        )
        self.assertEqual(loaded.returncode, 0)
        dispatched = self._spawn(
            [
                "dispatch",
                "--db",
                str(db),
                "--clock-ms",
                "1",
                "--round-id",
                "before-failures",
            ]
        )
        stdout, stderr = dispatched.communicate(timeout=60)
        self.assertEqual(dispatched.returncode, 0, stdout + stderr)
        outcomes = write_json(
            temp / "failure.json",
            [
                {
                    "request_id": 1,
                    "generation": 1,
                    "replica_id": "r1",
                    "outcome": "failure",
                }
            ],
        )
        processes = [
            self._spawn(
                [
                    "dispatch",
                    "--db",
                    str(db),
                    "--clock-ms",
                    "2",
                    "--round-id",
                    f"failure-race-{index}",
                    "--outcomes",
                    str(outcomes),
                    "--no-dispatch",
                ]
            )
            for index in range(8)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(
                process.returncode, 0, f"stdout={stdout}\nstderr={stderr}"
            )
        state, _, _ = Store(db).read_state()
        self.assertEqual(state["counters"]["failures"], 1)
        self.assertEqual(state["counters"]["dead_letters"], 1)
        self.assertEqual(len(state["tombstones"]), 1)
        self.assertEqual(
            state["requests"]["1"]["dead_letter"]["trigger"], "failure"
        )
        self.assertTrue(verify_store(Store(db))["verified"])

    def test_concurrent_verify_uses_one_consistent_snapshot(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        loaded = load_cli(
            db,
            service_doc(),
            request_doc([request(1)]),
            config_doc(capacity=100, rate=100),
        )
        self.assertEqual(loaded.returncode, 0)
        plan = write_json(
            temp / "plan.json",
            {
                "schema_version": 1,
                "steps": [
                    {
                        "round_id": f"race-step-{index}",
                        "clock_ms": index,
                        "dispatch": False,
                    }
                    for index in range(1, 31)
                ],
            },
        )
        writer = self._spawn(
            ["dispatch", "--db", str(db), "--plan", str(plan)]
        )
        verifiers = [
            self._spawn(["verify", "--db", str(db)]) for _ in range(10)
        ]
        for verifier in verifiers:
            stdout, stderr = verifier.communicate(timeout=60)
            self.assertEqual(
                verifier.returncode, 0, f"stdout={stdout}\nstderr={stderr}"
            )
            self.assertIn('"verified":true', stdout)
        stdout, stderr = writer.communicate(timeout=60)
        self.assertEqual(writer.returncode, 0, stdout + stderr)
        self.assertTrue(verify_store(Store(db))["verified"])


if __name__ == "__main__":
    unittest.main()
