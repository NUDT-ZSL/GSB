from __future__ import annotations

import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from relief_tiles.gray import generate_full_gray
from tests.helpers import config, config_dict, run_cli, start_cli, write_config


def _block_files(state: Path) -> list[Path]:
    return sorted(state.glob("versions/*/blocks/*.npz"))


def _wait_for_block(state: Path, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _block_files(state):
            return
        time.sleep(0.03)
    raise AssertionError("no block file appeared before timeout")


class SignalRecoveryTests(unittest.TestCase):
    def _prepare(self, root: Path):
        value = config_dict(
            width=96,
            height=64,
            rows=4,
            cols=4,
            concurrency=2,
            budget_blocks=3,
        )
        config_path = root / "config.json"
        write_config(config_path, value)
        cfg = config(width=96, height=64, rows=4, cols=4, concurrency=2, budget_blocks=3)
        reference_path = root / "reference.npy"
        np.save(reference_path, generate_full_gray(cfg))
        return cfg, config_path, reference_path

    def test_graceful_signal_exits_at_boundary_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg, config_path, reference_path = self._prepare(root)
            state = root / "state"
            output = root / "out"
            process = start_cli(
                "render",
                "--config",
                str(config_path),
                "--state",
                str(state),
                "--output",
                str(output),
                "--block-delay-ms",
                "120",
            )
            try:
                _wait_for_block(state)
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            self.assertEqual(process.returncode, 75, stderr)
            payload = json.loads(stderr)
            self.assertEqual(payload["code"], "E_INTERRUPTED")
            marker = json.loads((state / "interrupted.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["version"], cfg.version)
            self.assertGreater(len(marker["missing_blocks"]), 0)

            resumed = run_cli(
                "render",
                "--config",
                str(config_path),
                "--state",
                str(state),
                "--output",
                str(output),
                "--reference",
                str(reference_path),
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(report["reused_blocks"], 1)
            self.assertEqual(report["max_abs_diff_vs_full_reference"], 0)
            self.assertTrue(
                np.array_equal(
                    np.load(output / "mosaic.npy"),
                    np.load(reference_path),
                )
            )
            self.assertFalse((state / "interrupted.json").exists())

    @unittest.skipIf(os.name == "nt", "SIGKILL fixture is Unix-specific")
    def test_forced_kill_restarts_from_valid_persisted_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg, config_path, reference_path = self._prepare(root)
            state = root / "state"
            output = root / "out"
            process = start_cli(
                "render",
                "--config",
                str(config_path),
                "--state",
                str(state),
                "--output",
                str(output),
                "--block-delay-ms",
                "180",
            )
            try:
                _wait_for_block(state)
                process.kill()
                process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            valid_files = _block_files(state)
            self.assertGreaterEqual(len(valid_files), 1)

            resumed = run_cli(
                "render",
                "--config",
                str(config_path),
                "--state",
                str(state),
                "--output",
                str(output),
                "--reference",
                str(reference_path),
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(report["reused_blocks"], 1)
            self.assertEqual(report["max_abs_diff_vs_full_reference"], 0)
            self.assertTrue(
                np.array_equal(np.load(output / "mosaic.npy"), np.load(reference_path))
            )


if __name__ == "__main__":
    unittest.main()
