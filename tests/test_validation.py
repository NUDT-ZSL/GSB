import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scaler.errors import ScalerError
from scaler.parser import (
    parse_config,
    parse_load,
    parse_nodes,
    parse_services,
    parse_v1,
    parse_v2_shard,
    read_v2_metadata,
    validate_shard_order,
)


SERVICE = {
    "name": "svc",
    "replicas": 2,
    "cpu_per_replica": 1,
    "mem_per_replica": 1,
    "upstream": [],
    "failure_domain": "fd",
    "priority": 1,
}


class ValidationTests(unittest.TestCase):
    def test_duplicate_service(self):
        with self.assertRaises(ScalerError) as ctx:
            parse_services([dict(SERVICE), dict(SERVICE)])
        self.assertEqual(ctx.exception.code, "E_BAD_SAMPLE")

    def test_self_dependency_rejected(self):
        row = dict(SERVICE, upstream=["svc"])
        with self.assertRaises(ScalerError) as ctx:
            parse_services([row])
        self.assertEqual(ctx.exception.code, "E_BAD_SAMPLE")

    def test_missing_upstream_rejected(self):
        row = dict(SERVICE, upstream=["ghost"])
        with self.assertRaises(ScalerError) as ctx:
            parse_services([row])
        self.assertEqual(ctx.exception.code, "E_BAD_SAMPLE")

    def test_cycle_rejected(self):
        a = dict(SERVICE, name="a", upstream=["b"])
        b = dict(SERVICE, name="b", upstream=["a"])
        with self.assertRaises(ScalerError) as ctx:
            parse_services([a, b])
        self.assertEqual(ctx.exception.code, "E_BAD_SAMPLE")

    def test_non_positive_resources_rejected(self):
        row = dict(SERVICE, cpu_per_replica=0)
        with self.assertRaises(ScalerError):
            parse_services([row])

    def test_v1_ratios_must_be_increasing_and_end_at_one(self):
        good = [{"relative_seconds": 0, "ratio": "0.5"},
                {"relative_seconds": 1, "ratio": "1"}]
        self.assertEqual(len(parse_v1([{"service": "svc", "gears": good}])), 1)
        missing_baseline = [{"relative_seconds": 0, "ratio": "0.5"},
                            {"relative_seconds": 1, "ratio": "0.9"}]
        with self.assertRaises(ScalerError):
            parse_v1([{"service": "svc", "gears": missing_baseline}])
        bad_order = [{"relative_seconds": 0, "ratio": "0.8"},
                     {"relative_seconds": 1, "ratio": "0.5"},
                     {"relative_seconds": 2, "ratio": "1"}]
        with self.assertRaises(ScalerError):
            parse_v1([{"service": "svc", "gears": bad_order}])
        expansion = [
            {"relative_seconds": 0, "ratio": "0.5"},
            {"relative_seconds": 1, "ratio": "1"},
            {"relative_seconds": 2, "ratio": "1.5"},
        ]
        self.assertEqual(
            parse_v1([{"service": "svc", "gears": expansion}])[0].gears[-1].ratio,
            "1.5",
        )
        with self.assertRaises(ScalerError):
            parse_v1([{"service": "svc",
                       "gears": [{"relative_seconds": -1, "ratio": "1"}]}])

    def test_node_free_cannot_exceed_total(self):
        with self.assertRaises(ScalerError):
            parse_nodes([{
                "name": "n", "cpu_total": 4, "mem_total": 4,
                "cpu_free": 5, "mem_free": 4, "topology_domain": "fd",
            }])

    def test_v2_metadata_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "meta.db")
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE shard_meta(shard_id TEXT PRIMARY KEY, "
                "generation INTEGER, shard_hash TEXT)"
            )
            connection.execute(
                "INSERT INTO shard_meta VALUES('s1', 1, 'hash-from-db')"
            )
            connection.commit()
            connection.close()
            payload = {
                "shard_id": "s1",
                "generation": 1,
                "declared_clock_ms": 100,
                "shard_hash": "different",
                "services": ["svc"],
                "policies": [],
                "tombstones": [],
            }
            with self.assertRaises(ScalerError) as ctx:
                parse_v2_shard(payload, read_v2_metadata(db))
            self.assertEqual(ctx.exception.code, "E_BAD_SHARD")

    def test_v2_generation_monotonic_across_shards(self):
        from scaler.models import V2Shard

        first = V2Shard("s1", 2, "h1", 100, (), (), ())
        second = V2Shard("s2", 1, "h2", 100, (), (), ())
        with self.assertRaises(ScalerError) as ctx:
            validate_shard_order([first, second])
        self.assertEqual(ctx.exception.code, "E_BAD_SHARD")

    def test_load_range_and_config(self):
        with self.assertRaises(ScalerError):
            parse_load([{"service": "s", "at_ms": 0, "load": "1.2"}])
        config = parse_config({"fixed_scale": 100})
        self.assertEqual(config["fixed_scale"], 100)
        with self.assertRaises(ScalerError):
            parse_config({"fd_concurrency": 0})


if __name__ == "__main__":
    unittest.main()
