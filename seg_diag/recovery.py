"""Batch checkpoints and restart recovery after forced termination."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .codes import CODES
from .errors import DiagnosticError
from .hashing import canonical_bytes, content_key
from .ledger import assemble_ledger, compute_pixel_row, iter_pixel_coordinates, pixel_id, region_id
from .models import UnifiedConfig
from .normalization import normalize_v1, normalize_v2


CHECKPOINT_NAME = "checkpoint.json"


class ForcedTermination(RuntimeError):
    """Raised by test injection to emulate SIGKILL at a batch boundary."""


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def list_regions(height: int, width: int, region_size: int) -> list[str]:
    rows = (height + region_size - 1) // region_size
    cols = (width + region_size - 1) // region_size
    return [f"R:{ry}:{rx}" for ry in range(rows) for rx in range(cols)]


class RecoveryRunner:
    def __init__(
        self,
        state_dir: str | Path,
        arrays: dict[str, np.ndarray],
        v1_config: UnifiedConfig,
        v2_config: UnifiedConfig,
        *,
        seed: int,
        region_size: int = 4,
        threshold_strategy: str = "strict",
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_dir = self.state_dir / "ledgers"
        self.ledger_dir.mkdir(exist_ok=True)
        self.checkpoint_path = self.state_dir / CHECKPOINT_NAME
        self.arrays = arrays
        self.v1_config = v1_config
        self.v2_config = v2_config
        self.seed = seed
        self.region_size = region_size
        self.threshold_strategy = threshold_strategy
        self.height, self.width = arrays["gt_instance"].shape
        self.all_regions = list_regions(self.height, self.width, region_size)
        self._coordinates_by_region: dict[str, list[tuple[int, int]]] = {
            region: [] for region in self.all_regions
        }
        for y, x in iter_pixel_coordinates(self.height, self.width):
            self._coordinates_by_region[region_id(y, x, region_size)].append((y, x))

    def load_checkpoint(self) -> dict[str, Any] | None:
        if not self.checkpoint_path.exists():
            return None
        try:
            checkpoint = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DiagnosticError(
                CODES.CHECKPOINT_INVALID, "checkpoint is not valid JSON", {"path": str(self.checkpoint_path)}
            ) from exc
        for field in ["completed_regions", "ledger_version", "ledger_checksum"]:
            if field not in checkpoint:
                raise DiagnosticError(CODES.CHECKPOINT_INVALID, f"checkpoint lacks {field}")
        ledger_path = Path(checkpoint["ledger_path"])
        if not ledger_path.is_absolute():
            ledger_path = self.state_dir / ledger_path
        if not ledger_path.exists():
            raise DiagnosticError(
                CODES.CHECKPOINT_INVALID, "checkpoint ledger is missing", {"path": str(ledger_path)}
            )
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if ledger.get("checksum") != checkpoint["ledger_checksum"]:
            raise DiagnosticError(
                CODES.INCONSISTENT,
                "checkpoint ledger checksum mismatch",
                {"checkpoint": checkpoint["ledger_checksum"], "ledger": ledger.get("checksum")},
            )
        checkpoint["_ledger"] = ledger
        checkpoint["_ledger_absolute_path"] = str(ledger_path)
        return checkpoint

    def _write_checkpoint(
        self, completed: set[str], version: int, ledger: dict[str, Any]
    ) -> dict[str, Any]:
        ledger_bytes = canonical_bytes(ledger) + b"\n"
        ledger_path = self.ledger_dir / f"{ledger['checksum']}.json"
        atomic_write_bytes(ledger_path, ledger_bytes)
        relative_ledger_path = ledger_path.relative_to(self.state_dir)
        checkpoint = {
            "format": "seg-diag-checkpoint/v1",
            "completed_regions": sorted(completed),
            "ledger_version": version,
            "ledger_checksum": ledger["checksum"],
            "ledger_byte_checksum": ledger["byte_checksum"],
            "ledger_path": str(relative_ledger_path),
            "region_size": self.region_size,
            "all_regions": self.all_regions,
        }
        atomic_write_bytes(self.checkpoint_path, canonical_bytes(checkpoint) + b"\n")
        return checkpoint

    def run(
        self,
        *,
        batch_size: int = 2,
        fail_in_batch_index: int | None = None,
        termination_callback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        checkpoint = self.load_checkpoint()
        if checkpoint is None:
            completed: set[str] = set()
            existing_rows: dict[str, dict[str, Any]] = {}
            version = 0
        else:
            completed = set(checkpoint["completed_regions"])
            existing_rows = {row["row_id"]: row for row in checkpoint["_ledger"]["rows"]}
            version = int(checkpoint["ledger_version"])

        reused_committed_regions = sorted(completed)
        pending_regions = [item for item in self.all_regions if item not in completed]
        v1_normalized = normalize_v1(self.arrays["v1_instance"], self.v1_config)
        v2_normalized = normalize_v2(
            self.arrays["v2_category"],
            self.arrays["v2_instance"],
            self.arrays["v2_confidence"],
            self.v2_config,
            strategy=self.threshold_strategy,
        )

        computed_regions: list[str] = []
        committed_batches: list[dict[str, Any]] = []
        self.last_computed_regions = computed_regions
        for batch_index, start in enumerate(range(0, len(pending_regions), batch_size)):
            batch_regions = pending_regions[start : start + batch_size]
            pending_rows: dict[str, dict[str, Any]] = {}
            for region in batch_regions:
                for y, x in self._coordinates_by_region[region]:
                    row = compute_pixel_row(
                        y,
                        x,
                        self.arrays,
                        self.v1_config,
                        self.v2_config,
                        v1_normalized=v1_normalized,
                        v2_normalized=v2_normalized,
                        threshold_strategy=self.threshold_strategy,
                        region_size=self.region_size,
                    )
                    pending_rows[row["row_id"]] = row
                computed_regions.append(region)

            if fail_in_batch_index == batch_index:
                if termination_callback is not None:
                    termination_callback()
                raise ForcedTermination(
                    "emulated forced termination before batch checkpoint",
                    {"batch_index": batch_index, "regions": batch_regions},
                )

            merged_rows = dict(existing_rows)
            merged_rows.update(pending_rows)
            version += 1
            ledger = assemble_ledger(
                merged_rows,
                arrays=self.arrays,
                seed=self.seed,
                v1_checksum=self.v1_config.checksum,
                v2_checksum=self.v2_config.checksum,
                threshold_strategy=self.threshold_strategy,
                region_size=self.region_size,
                ledger_version=version,
            )
            checkpoint = self._write_checkpoint(completed | set(batch_regions), version, ledger)
            completed.update(batch_regions)
            existing_rows = merged_rows
            committed_batches.append(
                {
                    "batch_index": batch_index,
                    "regions": batch_regions,
                    "ledger_version": version,
                    "ledger_checksum": ledger["checksum"],
                }
            )

        final_checkpoint = self.load_checkpoint()
        assert final_checkpoint is not None
        return {
            "business_code": CODES.OK,
            "state_dir": str(self.state_dir),
            "computed_regions": computed_regions,
            "reused_committed_regions": reused_committed_regions,
            "committed_batches": committed_batches,
            "checkpoint": {k: v for k, v in final_checkpoint.items() if not k.startswith("_")},
            "ledger": final_checkpoint["_ledger"],
            "ledger_path": final_checkpoint["_ledger_absolute_path"],
        }


def simulate_hard_kill(state_dir: str | Path) -> None:
    """Hard-exit hook used by the optional crash subprocess."""
    atomic_write_bytes(Path(state_dir) / "kill_marker.json", canonical_bytes({"killed": True}))
    os._exit(137)
