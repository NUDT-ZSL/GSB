import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scaler.errors import ScalerError
from scaler.parser import parse_services
from scaler.orchestrator import Orchestrator
from tests.helpers import Harness


class NoWriteOnFailureTests(unittest.TestCase):
    def test_parser_failure_creates_no_database_artifacts(self):
        harness = Harness()
        db = os.path.join(harness.root, "should_not_exist.db")
        with self.assertRaises(ScalerError):
            parse_services([
                {"name": "x", "replicas": 1, "cpu_per_replica": 0,
                 "mem_per_replica": 1, "upstream": [],
                 "failure_domain": "fd", "priority": 1}
            ])
        # pure validation never touches the working directory
        self.assertFalse(os.path.exists(db))
        self.assertEqual(glob.glob(db + "*"), [])
        harness.cleanup()

    def test_rollback_rejects_before_writing_new_package(self):
        harness = Harness()
        harness.load()
        orch = Orchestrator(harness.db_path)
        packages_before = orch.storage.connection.execute(
            "SELECT COUNT(*) AS n FROM packages"
        ).fetchone()["n"]
        with self.assertRaises(ScalerError) as ctx:
            orch.load_package(
                harness.context(), "0.0.1", 0, ["rollback-hash"]
            )
        self.assertEqual(ctx.exception.code, "E_VERSION_ROLLBACK")
        packages_after = orch.storage.connection.execute(
            "SELECT COUNT(*) AS n FROM packages"
        ).fetchone()["n"]
        self.assertEqual(packages_before, packages_after)
        orch.close()
        harness.cleanup()


if __name__ == "__main__":
    unittest.main()
