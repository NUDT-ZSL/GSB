from __future__ import annotations

import tempfile
from pathlib import Path

from quant_diag.config import EngineConfig
from quant_diag.engine import DiagnosticEngine
from quant_diag.sample_data import write_default_samples


class Workspace:
    def __init__(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.manifest, self.calibration = write_default_samples(self.root)

    def engine(
        self,
        runtime: str,
        *,
        config: EngineConfig | None = None,
        manifest: Path | None = None,
        calibration: Path | None = None,
        force_kill_after: tuple[str, int] | None = None,
    ) -> DiagnosticEngine:
        return DiagnosticEngine(
            manifest_path=manifest or self.manifest,
            calibration_path=calibration or self.calibration,
            runtime_dir=self.root / runtime,
            config=config,
            force_kill_after=force_kill_after,
        )

    def cleanup(self) -> None:
        self._temp.cleanup()
