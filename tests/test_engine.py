from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cdn_engine.engine import Engine
from cdn_engine.errors import EngineError
from cdn_engine.parser import parse_sample, read_json
from cdn_engine.seed import EPOCH_MS, generate_sample, write_json
from cdn_engine.storage import Store
from cdn_engine.verification import run_verification


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample = self.root / "sample"
        generate_sample(self.sample, port=free_port(), offline_port=free_port())
        self.store = Store(self.root / "state.db")
        self.engine = Engine(self.store)
        self.engine.load_sample(self.sample, EPOCH_MS)
        self.engine.merge(EPOCH_MS)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_merge_and_idempotent_observation_submission(self) -> None:
        result = self.engine.merge(EPOCH_MS)
        self.assertEqual(result["summary"]["rule_count"], 8)
        shared = next(rule for rule in result["rules"] if rule["name"] == "shared-config")
        self.assertEqual(shared["source_version"], "v2")
        self.assertEqual(shared["decision_basis"]["tie_break_applied"], "equal_declared_clock;source_format_priority_desc")

        submission = self.engine.import_observation_file(self.sample / "observations" / "probe-stream.json")
        self.assertEqual(submission["submitted"], 7)
        self.assertEqual(submission["inserted"], 0)
        self.assertEqual(submission["duplicate"], 7)

    def test_probe_without_origin_is_deterministic(self) -> None:
        first = self.engine.probe(EPOCH_MS + 120_000, origin_enabled=False)
        self.assertEqual(first["processed_new"], 7)
        self.assertEqual(first["closed_windows"], 42)
        second = self.engine.probe(EPOCH_MS + 120_000, origin_enabled=False)
        self.assertEqual(second["processed_new"], 0)
        self.assertEqual(second["skipped_duplicates"], 7)

    def test_patch_closure_marks_chain(self) -> None:
        self.engine.probe(EPOCH_MS + 60_000, origin_enabled=False)
        patch = self.engine.apply_patch_file(
            self.sample / "patches" / "patch-origin-catalog.json",
            EPOCH_MS + 125_000,
        )
        closure = patch["closure"]
        self.assertEqual(
            closure["affected_rules"],
            ["catalog-list", "catalog-page", "origin-catalog"],
        )
        page = next(item for item in closure["entries"] if item["rule_name"] == "catalog-page")
        self.assertEqual(
            page["dependency_path"],
            ["origin-catalog", "catalog-list", "catalog-page"],
        )
        self.assertIn("api-auth", closure["reusable_rules"])

    def test_inflight_window_keeps_starting_rule_version(self) -> None:
        self.engine.probe(EPOCH_MS + 10_000, origin_enabled=False)
        package = self.engine.load_package(
            self.sample / "packages" / "rules-package-3",
            EPOCH_MS + 20_000,
        )
        self.assertEqual(package["decision"], "full_replace")
        active_view_id = self.store.active_view_id()
        self.assertGreater(active_view_id, 1)

        self.engine.probe(EPOCH_MS + 60_000, origin_enabled=False)
        row = self.store.conn.execute(
            "SELECT result_json FROM conclusions WHERE node_name='edge-a' "
            "AND rule_name='catalog-page' AND start_ms=?",
            (EPOCH_MS,),
        ).fetchone()
        conclusion = json.loads(row["result_json"])
        self.assertEqual(conclusion["rule_version"]["view_id"], 1)

        row = self.store.conn.execute(
            "SELECT result_json FROM conclusions WHERE node_name='edge-b' "
            "AND rule_name='catalog-page' AND start_ms=?",
            (EPOCH_MS,),
        ).fetchone()
        conclusion = json.loads(row["result_json"])
        self.assertEqual(conclusion["rule_version"]["view_id"], active_view_id)

    def test_invalid_inputs_have_business_error_codes(self) -> None:
        duplicate = self.root / "duplicate"
        shutil.copytree(self.sample, duplicate)
        v1_path = duplicate / "v1" / "rules.json"
        v1 = read_json(v1_path)
        v1["rules"].append(dict(v1["rules"][0]))
        write_json(v1_path, v1)
        with self.assertRaises(EngineError) as ctx:
            parse_sample(duplicate)
        self.assertEqual(ctx.exception.code, "E_DUPLICATE_RULE_NAME")

        bad_hash = self.root / "bad-hash"
        shutil.copytree(self.sample, bad_hash)
        shard = bad_hash / "v2" / "shards" / "shard-00-core.json"
        payload = read_json(shard)
        payload["tampered"] = True
        write_json(shard, payload)
        with self.assertRaises(EngineError) as ctx:
            parse_sample(bad_hash)
        self.assertEqual(ctx.exception.code, "E_SHARD_HASH_MISMATCH")

    def test_additional_validation_error_codes(self) -> None:
        def copy_sample(name: str) -> Path:
            target = self.root / name
            shutil.copytree(self.sample, target)
            return target

        bad_seconds = copy_sample("bad-seconds")
        v1_path = bad_seconds / "v1" / "rules.json"
        v1 = read_json(v1_path)
        v1["rules"][0]["relative_effective_seconds"] = -1
        write_json(v1_path, v1)
        with self.assertRaises(EngineError) as ctx:
            parse_sample(bad_seconds)
        self.assertEqual(ctx.exception.code, "E_INVALID_EFFECTIVE_SECONDS")

        dangling = copy_sample("dangling")
        v1_path = dangling / "v1" / "rules.json"
        v1 = read_json(v1_path)
        v1["forward_dependencies"]["ghost-source"] = "origin-auth"
        write_json(v1_path, v1)
        with self.assertRaises(EngineError) as ctx:
            parse_sample(dangling)
        self.assertEqual(ctx.exception.code, "E_DANGLING_DEPENDENCY")

        bad_tombstone = copy_sample("bad-tombstone")
        connection = sqlite3.connect(bad_tombstone / "v2" / "metadata.sqlite")
        connection.execute(
            "INSERT INTO tombstones(rule_name, reason, logical_clock_ms) VALUES('ghost', 'test', 0)"
        )
        connection.commit()
        connection.close()
        with self.assertRaises(EngineError) as ctx:
            parse_sample(bad_tombstone)
        self.assertEqual(ctx.exception.code, "E_INVALID_TOMBSTONE")

        bad_sequence = copy_sample("bad-sequence")
        obs_path = bad_sequence / "observations" / "probe-stream.json"
        stream = read_json(obs_path)
        stream["observations"][1]["seq"] = 1
        write_json(obs_path, stream)
        with self.assertRaises(EngineError) as ctx:
            parse_sample(bad_sequence)
        self.assertEqual(ctx.exception.code, "E_OBS_SEQUENCE_NOT_MONOTONIC")

        bad_digest = copy_sample("bad-digest")
        obs_path = bad_digest / "observations" / "probe-stream.json"
        stream = read_json(obs_path)
        stream["observations"][0]["content_digest"] = None
        write_json(obs_path, stream)
        with self.assertRaises(EngineError) as ctx:
            parse_sample(bad_digest)
        self.assertEqual(ctx.exception.code, "E_OBS_DIGEST_RESULT_MISMATCH")


class CliVerificationTests(unittest.TestCase):
    def test_full_verification_command_logic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_verification(
                Path(temp_dir) / "verify",
                requested_port=free_port(),
                offline_port=free_port(),
            )
            if not result["ok"]:
                self.fail(json.dumps(result, ensure_ascii=False, indent=2))
            self.assertGreaterEqual(result["check_count"], 20)


if __name__ == "__main__":
    unittest.main()
