from __future__ import annotations

import json

from replica_ledger.analysis import analyze_local
from replica_ledger.anomalies import (
    corrupt_shard_digest,
    parse_target,
    truncate_block_file,
)
from replica_ledger.errors import BlockError, E_BLOCK, E_GRID, E_INTEGRITY, GridError, IntegrityError
from replica_ledger.fixed import validate_scale
from replica_ledger.spec import load_replica_group
from replica_ledger.util import JsonlEventLog

from tests.common import SampleWorkspace


class AnomalyTests(SampleWorkspace):
    def test_truncation_reports_e_block_with_paths_and_log(self) -> None:
        root = self.copy_scenario("single")
        spec = load_replica_group(root / "spec.json")
        log_path = root / "block.jsonl"
        logger = JsonlEventLog(log_path)
        truncate_block_file(spec, parse_target("n4:B-SINGLE"), logger=logger)
        with self.assertRaises(BlockError) as context:
            analyze_local(spec, 8, logger=logger)
        error = context.exception
        self.assertEqual(error.code, E_BLOCK)
        self.assertEqual(error.node_id, "n4")
        self.assertEqual(error.block_id, "B-SINGLE")
        self.assertIn("byte_size", error.field_path)
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        errors = [row for row in records if row["level"] == "ERROR"]
        self.assertTrue(errors)
        for row in errors:
            self.assertIn("block_id", row)
            self.assertIn("node_id", row)
            self.assertIn("field_path", row)
            self.assertEqual(row["code"], E_BLOCK)

    def test_digest_corruption_reports_e_integrity(self) -> None:
        root = self.copy_scenario("single")
        spec_path = root / "spec.json"
        spec = load_replica_group(spec_path)
        log_path = root / "integrity.jsonl"
        logger = JsonlEventLog(log_path)
        corrupt_shard_digest(
            spec_path, spec, parse_target("n1:B-SINGLE"), logger=logger
        )
        corrupted = load_replica_group(spec_path)
        with self.assertRaises(IntegrityError) as context:
            analyze_local(corrupted, 8, logger=logger)
        error = context.exception
        self.assertEqual(error.code, E_INTEGRITY)
        self.assertEqual(error.node_id, "n1")
        self.assertEqual(error.block_id, "B-SINGLE")
        self.assertIn("shards[0].digest", error.field_path)
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["code"] == E_INTEGRITY and row["level"] == "ERROR" for row in records))

    def test_invalid_grid_reports_e_grid(self) -> None:
        with self.assertRaises(GridError) as context:
            validate_scale(0)
        self.assertEqual(context.exception.code, E_GRID)
        self.assertEqual(context.exception.field_path, "session.scale")
