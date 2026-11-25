"""Filesystem layout for a workbench root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def open(cls, root: str | os.PathLike[str] | None = None) -> "Paths":
        base = Path(root or os.environ.get("PROXY_DIAG_ROOT") or Path.cwd()).resolve()
        paths = cls(root=base)
        paths.ensure()
        return paths

    @property
    def samples(self) -> Path:
        return self.root / "samples"

    @property
    def current_package(self) -> Path:
        return self.samples / "current_package.json"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def report_dir(self) -> Path:
        return self.root / "reports"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "workbench.sqlite3"

    @property
    def global_lock_path(self) -> Path:
        return self.state_dir / "workbench.lock"

    @property
    def session_lock_dir(self) -> Path:
        return self.state_dir / "session-locks"

    @property
    def unified_view_path(self) -> Path:
        return self.report_dir / "unified_view.json"

    @property
    def status_path(self) -> Path:
        return self.report_dir / "status.json"

    @property
    def diagnostic_report_path(self) -> Path:
        return self.report_dir / "diagnostic_report.json"

    def ensure(self) -> None:
        self.samples.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.session_lock_dir.mkdir(parents=True, exist_ok=True)
