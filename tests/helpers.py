from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from corpus_dedup_workbench.config import WorkbenchConfig
from corpus_dedup_workbench.service import DedupService

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "manifest.jsonl"
CONFIG = ROOT / "config" / "dedup_config.json"


def sample_mapping() -> dict[str, Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def sample_config(**overrides: Any) -> WorkbenchConfig:
    mapping = sample_mapping()
    mapping.update(overrides)
    return WorkbenchConfig.from_mapping(mapping)


def make_service(
    directory: str | Path,
    **overrides: Any,
) -> DedupService:
    return DedupService(
        DATA,
        sample_config(**overrides),
        cache_root=Path(directory) / "cache",
    )


class TempDirMixin:
    _temp: tempfile.TemporaryDirectory[str]

    def make_temp(self) -> Path:
        self._temp = tempfile.TemporaryDirectory()
        return Path(self._temp.name)

    def cleanup(self) -> None:
        temp = getattr(self, "_temp", None)
        if temp is not None:
            temp.cleanup()

