from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kvledger.engine import (
    advance_next_token,
    apply_merge,
    claim_next_attempt,
    create_round,
    finalize_claimed_attempt,
    load_inputs,
    plan_pressure_candidates,
    run_eviction_round,
)
from kvledger.errors import KvError
from kvledger.ledger import encode_frame, write_frame_at
from kvledger.recovery import recover_workspace
from kvledger.reports import verify_workspace
from kvledger.seed import LOGICAL_CLOCK_MS, generate_samples
from kvledger.storage import Database
from kvledger.util import deterministic_payload


CLOCK = LOGICAL_CLOCK_MS + 600_000


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kvledger-test-"))
        generate_samples(self.tmp / "samples")
        self.samples = self.tmp / "samples"
        self.requests = self.samples / "requests.json"
        self.config = self.samples / "memory_config.json"
        self.policy = self.samples / "policy_package"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self, name: str = "ws") -> Database:
        return Database(self.tmp / name)

    def _load_merge(self, db: Database) -> None:
        load_inputs(db, self.requests, self.config, LOGICAL_CLOCK_MS)
        apply_merge(db, self.policy, LOGICAL_CLOCK_MS)

    def _run_to_completion(self, db: Database, prefix: str = "R") -> None:
        for index in range(1, 15):
            run_eviction_round(db, f"{prefix}{index}", CLOCK + index)
        verdict = verify_workspace(db)
        self.assertTrue(verdict["ok"], verdict["checks"])

    def test_duplicate_seq_returns_independent_code_without_state(self) -> None:
        bad = self.tmp / "bad-requests.json"
        payload = json.loads(self.requests.read_text())
        payload["requests"][1]["seq_id"] = payload["requests"][0]["seq_id"]
        bad.write_text(json.dumps(payload))
        with self._db("bad") as db:
            with self.assertRaises(KvError) as ctx:
                load_inputs(db, bad, self.config, CLOCK)
            self.assertEqual(ctx.exception.code, "SEQ_ID_DUPLICATE")
            self.assertIsNone(db.conn.execute("SELECT 1 FROM config").fetchone())

    def test_capacity_config_error_code(self) -> None:
        bad_config = self.tmp / "bad-config.json"
        payload = json.loads(self.config.read_text())
        payload["total_memory_bytes"] = 4
        payload["layer_capacity_ratios"] = {
            "L0": "0.25",
            "L1": "0.25",
            "L2": "0.25",
            "L3": "0.25",
        }
        bad_config.write_text(json.dumps(payload))
        with self._db("bad-cap") as db:
            with self.assertRaises(KvError) as ctx:
                load_inputs(db, self.requests, bad_config, CLOCK)
            self.assertEqual(ctx.exception.code, "MEMORY_CAPACITY_INVALID")

    def test_shard_hash_mismatch_code_and_no_pointer(self) -> None:
        bad_package = self.tmp / "bad-policy"
        shutil.copytree(self.policy, bad_package)
        manifest_path = bad_package / "v2" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["shards"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self._db("bad-policy-db") as db:
            with self.assertRaises(KvError) as ctx:
                apply_merge(db, bad_package, CLOCK)
            self.assertEqual(ctx.exception.code, "V2_SHARD_HASH_MISMATCH")
            self.assertIsNone(db.get_meta("active_policy_version"))

    def test_full_chain_digest_and_recovery_match(self) -> None:
        with self._db("normal") as normal:
            self._load_merge(normal)
            self._run_to_completion(normal, "N")
            normal_verify = verify_workspace(normal)
            normal_digest = normal_verify["ledger_digest"]
            self.assertEqual(normal_verify["committed_evictions"], 320)

        with self._db("crash") as crash:
            self._load_merge(crash)
            create_round(crash, "CRASH", CLOCK)
            seq_rows = crash.conn.execute("SELECT seq_id FROM requests ORDER BY priority, seq_id").fetchall()
            for row in seq_rows:
                seq_id = row["seq_id"]
                while advance_next_token(crash, seq_id, CLOCK) is not None:
                    plan_pressure_candidates(crash, "CRASH", CLOCK)

            first = claim_next_attempt(crash, "CRASH", "unit-test", CLOCK)
            self.assertIsNotNone(first)
            nvme_path, lock_path = crash.paths()
            offset = int(first["file_offset"])
            fd = os.open(str(nvme_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.pwrite(fd, b"BADMAGIC-partial", offset)
            finally:
                os.close(fd)

            second = claim_next_attempt(crash, "CRASH", "unit-test", CLOCK)
            self.assertIsNotNone(second)
            header = json.loads(second["header_json"])
            payload = deterministic_payload(second["entry_id"], int(second["actual_bytes"]))
            frame = encode_frame(header, payload)
            write_frame_at(second["file_path"], lock_path, int(second["file_offset"]), frame)

            recovery = recover_workspace(crash, CLOCK + 1)
            self.assertEqual(recovery["recovery_verdict"], "replayed_unfinished")
            self.assertEqual(len(recovery["repaired_writing"]), 2)
            actions = {item["uid"]: item["action"] for item in recovery["repaired_writing"]}
            self.assertTrue(any(action.startswith("rewrote_") for action in actions.values()))
            self.assertIn("verified_complete_frame", actions.values())

            self._run_to_completion(crash, "R")
            crash_verify = verify_workspace(crash)
            self.assertTrue(crash_verify["ok"])
            self.assertEqual(crash_verify["ledger_digest"], normal_digest)

    def test_duplicate_finalize_is_idempotent(self) -> None:
        with self._db("idem") as db:
            self._load_merge(db)
            run_eviction_round(db, "IDEM", CLOCK)
            row = db.conn.execute("SELECT * FROM eviction_attempts WHERE status='committed' ORDER BY rank_in_round LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            before = db.conn.execute("SELECT COUNT(*) n FROM idempotent_ledger").fetchone()["n"]
            record = finalize_claimed_attempt(db, dict(row), CLOCK + 2)
            self.assertTrue(record["eviction_uid"])
            after = db.conn.execute("SELECT COUNT(*) n FROM idempotent_ledger").fetchone()["n"]
            self.assertEqual(before, after)
            verdict = verify_workspace(db)
            for check in verdict["checks"]:
                if check["name"] in {"request_byte_and_fixed_identity", "global_layer_ledger_matches_entries", "committed_frames_digest_valid"}:
                    self.assertTrue(check["ok"], check)


if __name__ == "__main__":
    unittest.main()
