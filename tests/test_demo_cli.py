from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from infer_workbench.canonical import canonical_json
from infer_workbench.cli import main
from infer_workbench.demo import run_demo


ROOT = Path(__file__).resolve().parent.parent


def _make_writable_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass


class DemoAndCliTests(unittest.TestCase):
    def test_demo_reports_are_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = run_demo(project_root=ROOT, out_dir=Path(first_dir) / "demo", clean=True)
            second = run_demo(project_root=ROOT, out_dir=Path(second_dir) / "demo", clean=True)
            first_reports = {
                name: json.loads((Path(first_dir) / "demo" / "reports" / f"{name}.json").read_text(encoding="utf-8"))
                for name in (
                    "01_float32",
                    "02_fixed_q3_interrupted",
                    "03_fixed_q2_replaced",
                    "04_restore_replay_continue",
                )
            }
            second_reports = {
                name: json.loads((Path(second_dir) / "demo" / "reports" / f"{name}.json").read_text(encoding="utf-8"))
                for name in first_reports
            }
            _make_writable_tree(Path(first_dir) / "demo")
            _make_writable_tree(Path(second_dir) / "demo")
        self.assertEqual(first["summary"], second["summary"])
        for name in first_reports:
            self.assertEqual(canonical_json(first_reports[name]), canonical_json(second_reports[name]))

    def test_cli_invalid_config_returns_e_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "bad.json"
            config_path.write_text(
                json.dumps(
                    {
                        "batch_size": 0,
                        "sort_strategy": "sample_id",
                        "precision_mode": "fixed",
                        "quant_scale": 1000,
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "run",
                        "--config",
                        str(config_path),
                        "--data-dir",
                        str(ROOT / "data"),
                        "--snapshot-dir",
                        temporary,
                        "--report",
                        str(Path(temporary) / "report.json"),
                    ]
                )
        self.assertEqual(code, 2)
        self.assertIn("E_CONFIG", output.getvalue())

    def test_cli_rejects_tampered_data_with_e_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            data_dir.mkdir()
            for name in ("dataset.json", "model.json", "generation.json"):
                source = ROOT / "data" / name
                (data_dir / name).write_bytes(source.read_bytes())
            dataset_path = data_dir / "dataset.json"
            payload = json.loads(dataset_path.read_text(encoding="utf-8"))
            payload["samples"][0]["input"][0] = 999.0
            dataset_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "run",
                        "--config",
                        str(ROOT / "configs" / "fixed_q3_b3.json"),
                        "--data-dir",
                        str(data_dir),
                        "--snapshot-dir",
                        str(Path(temporary) / "snapshots"),
                        "--report",
                        str(Path(temporary) / "report.json"),
                    ]
                )
        self.assertEqual(code, 3)
        self.assertIn("E_DATA", output.getvalue())

    def test_tkinter_workbench_constructs_when_tk_is_available(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter is not installed in this Python")
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no graphical display for Tk")
        try:
            from infer_workbench.ui import WorkbenchApp

            with tempfile.TemporaryDirectory() as temporary:
                app = WorkbenchApp(root, data_dir=ROOT / "data", snapshot_dir=Path(temporary))
                root.update_idletasks()
                self.assertEqual(app.batch_tree["columns"][0], "batch")
                self.assertEqual(app.snapshot_tree["columns"][0], "file")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
