from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from replication_lab.cli import main as cli_main
from replication_lab.service import LabSession

try:
    from tests.helpers import write_legacy_segments
except ModuleNotFoundError:  # support `unittest discover -s tests` without -t
    from helpers import write_legacy_segments


class CliSnapshotTests(unittest.TestCase):
    def test_cli_restore_and_verify_do_not_auto_upgrade_restored_legacy_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_legacy_segments(root / "logs")
            with LabSession.open(root, auto_upgrade=False) as session:
                session.replay(replay_rate=10, concurrency=2, reset=True)
                snapshot_id = session.save_snapshot()["snapshot_id"]
            # Upgrade and thereby make the current disk view differ from the snapshot.
            with LabSession.open(root, auto_upgrade=True) as session:
                self.assertEqual(session.version_set(), [3])

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                restore_code = cli_main(
                    [
                        "snapshot-restore",
                        "--root",
                        str(root),
                        "--json",
                        snapshot_id,
                    ]
                )
                verify_code = cli_main(
                    [
                        "snapshot-verify",
                        "--root",
                        str(root),
                        "--json",
                        snapshot_id,
                    ]
                )
            self.assertEqual(restore_code, 0)
            self.assertEqual(verify_code, 0)
            with LabSession.open(root, auto_upgrade=False) as restored:
                self.assertEqual(restored.version_set(), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
