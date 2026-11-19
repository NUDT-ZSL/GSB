from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from elastic_control import cli

from .support import SAMPLES, reset_server, running_server


class CliTests(unittest.TestCase):
    def test_full_cli_outputs_are_byte_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server(
            json.loads((SAMPLES / "faults.json").read_text())
        ) as (_, url):
            root = Path(temporary)
            outputs: list[dict[str, bytes]] = []
            for attempt in range(2):
                reset_server(url)
                out_dir = root / str(attempt)
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(
                        [
                            "run",
                            "--services",
                            str(SAMPLES / "services.json"),
                            "--nodes",
                            str(SAMPLES / "nodes.json"),
                            "--trace",
                            str(SAMPLES / "trace.json"),
                            "--db",
                            str(root / f"state-{attempt}.db"),
                            "--runtime-dir",
                            str(root / f"runtime-{attempt}"),
                            "--policy",
                            str(SAMPLES / "policy.json"),
                            "--launcher-url",
                            url,
                            "--run-id",
                            "cli-repeat",
                            "--out-dir",
                            str(out_dir),
                        ]
                    )
                self.assertEqual(code, 0, stderr.getvalue())
                files = {
                    str(path.relative_to(out_dir)): path.read_bytes()
                    for path in sorted(out_dir.rglob("*.json"))
                }
                outputs.append(files)
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(len(outputs[0]), 10)


if __name__ == "__main__":
    unittest.main()

