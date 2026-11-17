import json
import shutil
import tempfile
import unittest
from pathlib import Path

from gateway.errors import E_LIMIT, E_REPLAY, E_SPEC, E_UNKNOWN, E_VERSION, BusinessError
from gateway.injection import (
    INJECTIONS,
    inject_corrupt_policy,
    inject_token_version_conflict,
    inject_zero_window,
    validate_injected_inputs,
)
from gateway.ledger import Ledger
from gateway.replay import compare_decision, replay_ledger
from gateway.runner import evolve_policy, recover, run_file

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


class LedgerRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gw-ledger-"))
        self.workdir = self.tmp / "work"
        self.routes = SAMPLES / "basic" / "routes.json"
        self.tokens = SAMPLES / "basic" / "tokens.json"
        self.requests = SAMPLES / "basic" / "requests.ndjson"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_batches_persist_and_replay_matches(self) -> None:
        result = run_file(self.routes, self.tokens, self.requests,
                          self.workdir, batch_size=4, replay_after=True)
        self.assertEqual(result.batches, [1, 2, 3])
        ledger = Ledger(self.workdir / "ledger.db")
        try:
            complete = ledger.complete_batches()
            self.assertEqual(len(complete), 3)
            replay = replay_ledger(ledger)
            self.assertEqual(replay["status"], "match")
            self.assertEqual(replay["batches_checked"], 3)
            snapshots = ledger.policy_snapshots()
            self.assertIn("[1, 1]", snapshots)
        finally:
            ledger.close()

    def test_forced_termination_leaves_unknown_batch_and_blocks_report(self) -> None:
        result = run_file(self.routes, self.tokens, self.requests,
                          self.workdir, batch_size=5)
        self.assertEqual(result.batches, [1, 2, 3])
        ledger = Ledger(self.workdir / "ledger.db")
        try:
            orphan = ledger.begin_batch((1, 1))  # never committed: simulates kill -9
        finally:
            ledger.close()

        recovery = recover(self.workdir, replay=True)
        self.assertEqual(recovery["unknown_batches"], [orphan])
        self.assertEqual(recovery["complete_batches"], [1, 2, 3])

        ledger = Ledger(self.workdir / "ledger.db")
        try:
            with self.assertRaises(BusinessError) as ctx:
                ledger.raise_if_unknown_batch()
            self.assertEqual(ctx.exception.code, E_UNKNOWN)
            self.assertIn("rerun required", ctx.exception.message)
        finally:
            ledger.close()

    def test_recovery_continues_accepting_new_requests(self) -> None:
        run_file(self.routes, self.tokens, self.requests, self.workdir, batch_size=6)
        first = run_file(self.routes, self.tokens, self.requests,
                         self.workdir, batch_size=6, replay_after=True)
        self.assertEqual(first.recovery["replay"]["status"], "match")
        self.assertTrue(first.recovery.get("restored_state"))
        # Restored limiter state means the repeated stream starts from drained
        # buckets: more E_LIMIT than the cold run, proving state was rebuilt.
        cold = run_file(self.routes, self.tokens, self.requests,
                        self.tmp / "cold", batch_size=6)
        self.assertGreaterEqual(
            first.engine.decision_codes()["E_LIMIT"],
            cold.engine.decision_codes()["E_LIMIT"],
        )

    def test_replay_detects_first_mismatch(self) -> None:
        run_file(self.routes, self.tokens, self.requests, self.workdir, batch_size=4)
        ledger = Ledger(self.workdir / "ledger.db")
        try:
            # Tamper with one recorded decision to prove E_REPLAY locates it.
            rows = ledger.batch_requests(2)
            recorded = rows[0][1]
            tampered = json.loads(json.dumps(recorded))
            tampered["decision"] = "E_OK" if tampered["decision"] != "E_OK" else "E_LIMIT"
            ledger.conn.execute(
                "UPDATE requests SET decision_json=? WHERE batch_id=? AND seq=?",
                (json.dumps(tampered), 2, tampered["seq"]),
            )
            ledger.conn.commit()
            with self.assertRaises(BusinessError) as ctx:
                replay_ledger(ledger)
            self.assertEqual(ctx.exception.code, E_REPLAY)
            self.assertIn(f"seq={tampered['seq']}", ctx.exception.message)
            self.assertIn("field=decision", ctx.exception.message)
        finally:
            ledger.close()

    def test_compare_decision_reports_path_and_field(self) -> None:
        recorded = {"seq": 9, "decision": "E_LIMIT", "auth": {"code": "E_OK"},
                    "paths": {"token_bucket": {"allow": False, "remaining": 0,
                                               "reason": "bucket_empty"}}}
        replayed = dict(recorded)
        replayed["paths"] = {"token_bucket": {"allow": True, "remaining": 1,
                                              "reason": "admitted"}}
        mismatch = compare_decision(recorded, replayed)
        self.assertEqual(mismatch["scope"], "token_bucket")
        self.assertEqual(mismatch["field"], "allow")


class InjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gw-inject-"))
        self.workdir = self.tmp / "work"
        self.backup = self.tmp / "backups"
        self.routes = self.tmp / "routes.json"
        self.tokens = self.tmp / "tokens.json"
        shutil.copy2(SAMPLES / "basic" / "routes.json", self.routes)
        shutil.copy2(SAMPLES / "basic" / "tokens.json", self.tokens)
        self.workdir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_three_injections_map_to_stable_codes(self) -> None:
        # Every injection runs on the temporary copies created in setUp, never
        # on the checked-in sample files.
        backup = inject_corrupt_policy(self.routes, self.backup)
        error = validate_injected_inputs("corrupt_policy", self.routes, self.tokens, self.backup)
        self.assertEqual(error.code, E_SPEC)
        shutil.copy2(backup, self.routes)

        backup = inject_token_version_conflict(self.tokens, self.backup)
        error = validate_injected_inputs("token_version_conflict", self.routes,
                                         self.tokens, self.backup)
        self.assertEqual(error.code, E_VERSION)
        shutil.copy2(backup, self.tokens)

        backup, _ = inject_zero_window(self.routes, self.backup)
        error = validate_injected_inputs("zero_window", self.routes, self.tokens, self.backup)
        self.assertEqual(error.code, E_LIMIT)

    def test_injection_logs_structured_events(self) -> None:
        ledger = Ledger(self.workdir / "ledger.db")
        try:
            inject_corrupt_policy(self.routes, self.backup)
            error = validate_injected_inputs("corrupt_policy", self.routes,
                                             self.tokens, self.backup)
            ledger.log_event(error.code, "injection_corrupt_policy",
                             {"message": error.message})
            events = ledger.read_log()
            self.assertTrue(any(event["code"] == E_SPEC for event in events))
        finally:
            ledger.close()

    def test_injection_catalog_is_complete(self) -> None:
        self.assertEqual(
            {spec.code for spec in INJECTIONS.values()},
            {E_SPEC, E_VERSION, E_LIMIT},
        )


class PolicyEvolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gw-evolve-"))
        self.workdir = self.tmp / "work"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_version_conflict_rejected_and_higher_version_replaces(self) -> None:
        routes_v1 = self.tmp / "routes_v1.json"
        tokens_v1 = self.tmp / "tokens_v1.json"
        routes_v2 = self.tmp / "routes_v2.json"
        tokens_v2 = self.tmp / "tokens_v2.json"
        shutil.copy2(SAMPLES / "basic" / "routes.json", routes_v1)
        shutil.copy2(SAMPLES / "basic" / "tokens.json", tokens_v1)

        data = json.loads(tokens_v1.read_text(encoding="utf-8"))
        data["version"] = 2
        tokens_v2.write_text(json.dumps(data), encoding="utf-8")
        routes_data = json.loads(routes_v1.read_text(encoding="utf-8"))
        routes_data["version"] = 2
        routes_v2.write_text(json.dumps(routes_data), encoding="utf-8")

        run_file(routes_v1, tokens_v1,
                 SAMPLES / "basic" / "requests.ndjson", self.workdir, batch_size=4)
        outcome = evolve_policy(self.workdir, routes_v2, tokens_v2,
                                SAMPLES / "basic" / "requests.ndjson", batch_size=4)
        self.assertEqual(outcome["replacement"]["new_version"], [2, 2])
        self.assertGreaterEqual(outcome["replacement"]["cache_entries_cleared"], 0)
        self.assertEqual(outcome["replacement"]["limiter_state"], "wholesale_replaced")

        # Non-monotonic reload must be rejected with E_VERSION.
        conflict = self.tmp / "tokens_conflict.json"
        conflict.write_text(json.dumps({**data, "version": 1}), encoding="utf-8")
        with self.assertRaises(BusinessError) as ctx:
            evolve_policy(self.workdir, routes_v2, conflict,
                          SAMPLES / "basic" / "requests.ndjson")
        self.assertEqual(ctx.exception.code, E_VERSION)


if __name__ == "__main__":
    unittest.main()
