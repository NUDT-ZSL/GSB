from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fusion_diag.generate import generate_workspace


class SampleWorkspace:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="fusion-diag-tests-"))
        self.samples = self.root / "samples"
        generate_workspace(self.samples)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, relative: str) -> str:
        return str(self.samples / relative)

