from __future__ import annotations

import sqlite3
import sys
import unittest

import numpy


class EnvironmentTests(unittest.TestCase):
    def test_required_runtime_is_available(self) -> None:
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))
        self.assertTrue(sqlite3.sqlite_version)
        self.assertTrue(numpy.__version__)


if __name__ == "__main__":
    unittest.main()
