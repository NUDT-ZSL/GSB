import json
import os
import sys
import tempfile
import unittest
import contextlib
import io

from blocksnap.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.image = os.path.join(self.tmp, "v.bin")
        self.store = os.path.join(self.tmp, "store")

    def _run(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["--image", self.image, "--store", self.store, *argv])

    def test_full_cli_flow(self):
        self.assertEqual(
            self._run("create-volume", "--block-size", "4096",
                      "--block-count", "8"), 0)
        self.assertEqual(self._run("init-store"), 0)
        self.assertEqual(self._run("snapshot", "--name", "s0"), 0)
        self.assertEqual(self._run("inject", "--block-index", "1",
                                   "--hex", "deadbeef"), 0)
        self.assertEqual(self._run("snapshot", "--name", "s1"), 0)
        self.assertEqual(self._run("audit"), 0)
        self.assertEqual(self._run("rollback", "--snapshot", "s0"), 0)
        self.assertEqual(self._run("logs", "--code", "E_INTEGRITY"), 0)

    def test_business_error_exit_code(self):
        self._run("create-volume", "--block-size", "4096", "--block-count", "4")
        self._run("init-store")
        rc = self._run("inject", "--block-index", "99", "--hex", "ab")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
