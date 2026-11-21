import argparse
import unittest

from drift_monitor.cli import cmd_migrate
from drift_monitor.errors import DriftError


class CliTests(unittest.TestCase):
    def test_unknown_migration_target(self):
        args = argparse.Namespace(
            config="samples/monitor_v1.cfg",
            target="v7",
            policy="freeze",
        )
        with self.assertRaises(DriftError) as ctx:
            cmd_migrate(args)
        self.assertEqual(ctx.exception.code, "E_VERSION_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
