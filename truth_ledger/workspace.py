from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .hashing import read_json, write_json_atomic
from .model import CSRMatrix


class CaseWorkspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.manifest_path = self.root / "case_manifest.json"
        self.initial_csr_path = self.root / "initial_csr.json"
        self.rhs_dense_path = self.root / "rhs_dense_b.npy"
        self.rhs_sparse_path = self.root / "rhs_sparse.json"
        self.events_dir = self.root / "events"
        self.initial_ledgers_dir = self.root / "initial_ledgers"
        self.cache_dir = self.root / "cache"
        self.state_dir = self.root / "state"
        self.snapshots_dir = self.state_dir / "snapshots"
        self.work_dir = self.state_dir / "work"
        self.latest_snapshot_path = self.state_dir / "latest.json"
        self.committed_dir = self.root / "committed"
        self.committed_csr_path = self.committed_dir / "csr.json"
        self.lineage_path = self.committed_dir / "lineage.json"
        self.committed_ledgers_dir = self.committed_dir / "ledgers"
        self.reports_dir = self.committed_dir / "reports"
        self.adjudication_path = self.reports_dir / "adjudication.json"
        self.self_check_path = self.reports_dir / "self_check.json"
        self.stage_records_path = self.root / "stage_records.jsonl"

    def ensure_dirs(self) -> "CaseWorkspace":
        for path in (
            self.root,
            self.events_dir,
            self.initial_ledgers_dir,
            self.cache_dir,
            self.snapshots_dir,
            self.work_dir,
            self.committed_dir,
            self.committed_ledgers_dir,
            self.reports_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def ledger_path(self, version: int, *, committed: bool = True) -> Path:
        directory = self.committed_ledgers_dir if committed else self.initial_ledgers_dir
        if version == 1:
            return directory / "v1_ledger.json"
        if version == 2:
            return directory / "v2"
        if version == 3:
            return directory / "v3_ledger.json"
        raise ValueError(version)

    def save_initial_csr(self, matrix: CSRMatrix) -> None:
        write_json_atomic(self.initial_csr_path, matrix.to_json_dict())

    def load_initial_csr(self) -> CSRMatrix:
        return CSRMatrix.from_json_dict(read_json(self.initial_csr_path))

    def save_rhs(self, dense_b: np.ndarray, sparse_b: CSRMatrix) -> None:
        np.save(self.rhs_dense_path, np.asarray(dense_b, dtype=np.float64))
        write_json_atomic(self.rhs_sparse_path, sparse_b.to_json_dict())

    def load_rhs(self) -> tuple[np.ndarray, CSRMatrix]:
        if not self.rhs_dense_path.exists():
            alternate = self.rhs_dense_path.with_name(self.rhs_dense_path.name + ".npy")
            dense_path = alternate if alternate.exists() else self.rhs_dense_path
        else:
            dense_path = self.rhs_dense_path
        dense_b = np.load(dense_path)
        sparse_b = CSRMatrix.from_json_dict(read_json(self.rhs_sparse_path))
        return dense_b, sparse_b

    def save_lineage(self, interval_versions: dict[str, int], last_write_version: int) -> None:
        write_json_atomic(
            self.lineage_path,
            {
                "interval_versions": interval_versions,
                "last_write_version": int(last_write_version),
            },
        )

    def load_lineage(self) -> dict[str, Any]:
        return read_json(self.lineage_path)

    def load_committed_csr(self) -> CSRMatrix:
        return CSRMatrix.from_json_dict(read_json(self.committed_csr_path))

    def save_committed_csr(self, matrix: CSRMatrix) -> None:
        write_json_atomic(self.committed_csr_path, matrix.to_json_dict())


def initial_lineage(matrix: CSRMatrix) -> dict[str, int]:
    return {
        matrix.interval_id(start, end): 0
        for start, end in matrix.interval_bounds()
    }
