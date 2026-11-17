from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from replica_ledger.samples import generate_samples


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SampleWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tempdir = tempfile.TemporaryDirectory(prefix="replica-ledger-tests-")
        cls.workspace = Path(cls._tempdir.name) / "samples"
        generate_samples(cls.workspace, force=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tempdir.cleanup()

    def copy_scenario(self, name: str) -> Path:
        destination = Path(self._tempdir.name) / f"scenario-{name}-{self._testMethodName}"
        shutil.copytree(self.workspace / "scenarios" / name, destination)
        return destination

    @staticmethod
    def read_json(path: str | Path) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def write_json(path: str | Path, value: Any) -> None:
        Path(path).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
