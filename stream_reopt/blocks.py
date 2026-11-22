"""Atomic local block/shard persistence for generated and joined batches."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from .data import TableBatch
from .join import ExecutionResult
from .models import BatchSpec


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    cleaned = cleaned.strip("._") or "batch"
    if cleaned != value:
        cleaned = f"{cleaned}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"
    return cleaned[:120]


class BlockStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _atomic_savez(self, path: Path, arrays: Mapping[str, np.ndarray]) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("wb") as fh:
            np.savez_compressed(fh, **{name: value for name, value in arrays.items()})
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def write_batch(
        self,
        batch: BatchSpec,
        table_batches: Mapping[str, TableBatch],
        result: ExecutionResult,
    ) -> str:
        relative = self.root / _safe_component(batch.batch_id)
        directory = relative
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if (
                existing
                and existing.get("output_digest") == result.output_digest
                and existing.get("input_digest") == result.input_digest
                and existing.get("shape_version") == result.shape.version
            ):
                referenced = list(existing.get("table_files", {}).values()) + [
                    existing.get("joined_file")
                ]
                if all(name and (directory / name).is_file() for name in referenced):
                    return relative.as_posix()
        files: dict[str, str] = {}
        for table_name, table_batch in sorted(table_batches.items()):
            filename = f"{_safe_component(table_name)}.npz"
            arrays = {
                f"{column}": table_batch.columns[column]
                for column in sorted(table_batch.columns)
            }
            self._atomic_savez(directory / filename, arrays)
            files[table_name] = filename
        joined_name = "joined.npz"
        columns = {
            hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]: values
            for name, values in result.columns.items()
        }
        self._atomic_savez(directory / joined_name, columns)
        manifest = {
            "batch_id": batch.batch_id,
            "arrival_seq": batch.arrival_seq,
            "shape_version": result.shape.version,
            "shape_digest": result.shape.content_digest,
            "input_digest": result.input_digest,
            "output_digest": result.output_digest,
            "output_rows": result.output_rows,
            "table_files": files,
            "joined_file": joined_name,
            "column_digests": {
                hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]: name
                for name in sorted(result.columns)
            },
            "complete": True,
        }
        tmp_manifest = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
        tmp_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_manifest, manifest_path)
        return relative.as_posix()

    def verify_manifest(self, relative_path: str, expected_output_digest: str) -> bool:
        manifest = Path(relative_path) / "manifest.json"
        if not manifest.exists():
            return False
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not data.get("complete") or data.get("output_digest") != expected_output_digest:
            return False
        directory = Path(relative_path)
        referenced = list(data.get("table_files", {}).values()) + [data.get("joined_file")]
        return all(name and (directory / name).is_file() for name in referenced)

