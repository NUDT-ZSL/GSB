from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from sft_pipeline.engine import PipelineEngine
from sft_pipeline.generator import generate_dataset_files
from sft_pipeline.workspace import Workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="sft-pipeline-test-")
        self.temp_root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def make_workspace(self, name: str = "workspace") -> Workspace:
        workspace = Workspace(self.temp_root / name)
        generate_dataset_files(workspace, force=True)
        PipelineEngine(workspace).store.ensure_manifest()
        return workspace

    def read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, path: Path, value: Any) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_pipeline(self, workspace: Workspace) -> dict[str, Any]:
        return PipelineEngine(workspace).run().to_dict()

    def export_bytes(self, workspace: Workspace, run: dict[str, Any]) -> bytes:
        return (workspace.root / run["final_export"]["path"]).read_bytes()

    def worker_command(self, workspace: Workspace, *extra: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "sft_pipeline",
            "worker",
            "--root",
            str(workspace.root),
            *extra,
        ]

    def worker_env(self) -> dict[str, str]:
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(PROJECT_ROOT)
            if not existing
            else str(PROJECT_ROOT) + os.pathsep + existing
        )
        return environment
