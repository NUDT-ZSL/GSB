from __future__ import annotations

from pathlib import Path

from .util import ensure_dir


class Workspace:
    """Filesystem layout for one local pipeline workspace."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.data_dir = self.root / "data"
        self.state_dir = self.root / "state"
        self.snapshots_dir = self.state_dir / "snapshots"
        self.cache_dir = self.state_dir / "cache"
        self.cache_objects_dir = self.cache_dir / "objects"
        self.cache_keys_dir = self.cache_dir / "keys"
        self.locks_dir = self.state_dir / "locks"
        self.exports_dir = self.root / "exports"

        self.samples_path = self.data_dir / "samples.jsonl"
        self.templates_path = self.data_dir / "templates.json"
        self.rules_path = self.data_dir / "cleaning_rules.json"
        self.quotas_path = self.data_dir / "quotas.json"
        self.pipeline_config_path = self.data_dir / "pipeline_config.json"

        self.manifest_path = self.state_dir / "manifest.json"
        self.recovery_report_path = self.state_dir / "recovery_report.json"
        self.last_error_path = self.state_dir / "last_error.json"
        self.last_edit_path = self.state_dir / "last_edit_report.json"
        self.demo_report_path = self.state_dir / "demo_report.json"
        self.graceful_stop_path = self.state_dir / "graceful_stop.json"
        self.stop_request_path = self.state_dir / "stop.request"

    def ensure_layout(self) -> None:
        for directory in (
            self.root,
            self.data_dir,
            self.state_dir,
            self.snapshots_dir,
            self.cache_objects_dir,
            self.cache_keys_dir,
            self.locks_dir,
            self.exports_dir,
        ):
            ensure_dir(directory)

    def input_paths(self) -> tuple[Path, ...]:
        return (
            self.samples_path,
            self.templates_path,
            self.rules_path,
            self.quotas_path,
            self.pipeline_config_path,
        )

    def inputs_exist(self) -> bool:
        return all(path.is_file() for path in self.input_paths())
