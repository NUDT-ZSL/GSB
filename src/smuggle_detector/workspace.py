"""Workspace layout and normalized input persistence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import NORMALIZED_SCHEMA_VERSION
from .errors import InputError
from .io_utils import atomic_write_json, read_json
from .models import SampleInput
from .parsing import compute_input_digest


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    @property
    def normalized_path(self) -> Path:
        return self.root / "normalized_input.json"

    @property
    def checkpoint_db(self) -> Path:
        return self.root / "checkpoint.sqlite3"

    @property
    def checkpoint_snapshot(self) -> Path:
        return self.root / "checkpoint_snapshot.json"

    def report_path(self, configured: str) -> Path:
        path = Path(configured)
        if path.is_absolute():
            return path
        return self.root / path

    def save_normalized(self, sample: SampleInput, input_digest: str) -> None:
        doc = {
            "schema_version": NORMALIZED_SCHEMA_VERSION,
            "input_digest": input_digest,
            "sample": sample.to_doc(),
        }
        atomic_write_json(self.normalized_path, doc)

    def load_normalized(self) -> tuple[SampleInput, str]:
        if not self.normalized_path.is_file():
            raise InputError(
                f"normalized input not found at {self.normalized_path}; run import first"
            )
        try:
            doc = read_json(self.normalized_path)
        except Exception as exc:
            raise InputError(f"cannot read normalized input: {exc}") from exc
        if doc.get("schema_version") != NORMALIZED_SCHEMA_VERSION:
            raise InputError("normalized input schema version mismatch; run import")
        try:
            sample = SampleInput.from_doc(doc["sample"])
        except Exception as exc:
            raise InputError(f"invalid normalized input: {exc}") from exc
        recomputed = compute_input_digest(sample)
        if recomputed != doc.get("input_digest"):
            raise InputError("normalized input digest mismatch; run import")
        return sample, doc["input_digest"]

