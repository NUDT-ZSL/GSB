"""Atomic state, phase snapshots, product artifacts, and resume validation."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from .errors import BusinessError, E_CONFIG_MISMATCH, E_IO, E_INPUT_INVALID
from .util import (
    atomic_write_json,
    atomic_write_npy,
    load_json,
    sha256_json,
    tensor_fingerprint,
    tensor_summary,
)


FORMAT_VERSION = 1
CURRENT = "current.json"


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.current_path = self.root / CURRENT
        self.products_dir = self.root / "products"
        self.phase_dir = self.root / "snapshots"
        self.config_path = self.root / "config.json"
        self.input_descriptor_path = self.root / "input_descriptor.json"
        self.input_array_path = self.root / "input.npy"
        self.lock_path = self.root / ".lock"

    @property
    def initialized(self) -> bool:
        return self.current_path.is_file()

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusinessError(E_IO, "state directory is locked by another process", stage="state") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(
        self,
        *,
        graph_raw: dict[str, Any],
        config_raw: dict[str, Any],
        input_descriptor_raw: dict[str, Any],
        input_array: np.ndarray,
        graph_version: int = 0,
    ) -> dict[str, Any]:
        if self.initialized:
            raise BusinessError(E_IO, "state directory is already initialized", stage="init")
        self.root.mkdir(parents=True, exist_ok=True)
        self.products_dir.mkdir(parents=True, exist_ok=True)
        self.phase_dir.mkdir(parents=True, exist_ok=True)
        graph_fingerprint = sha256_json(graph_raw)
        config_hash = sha256_json(config_raw)
        descriptor_hash = sha256_json(input_descriptor_raw)
        input_fp = tensor_fingerprint(input_array)
        atomic_write_json(self.config_path, config_raw)
        atomic_write_json(self.input_descriptor_path, input_descriptor_raw)
        atomic_write_npy(self.input_array_path, input_array)
        snapshot: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "snapshot_version": FORMAT_VERSION,
            "run_epoch": 1,
            "phase_seq": 0,
            "stage": "initialized",
            "status": "initialized",
            "graph_version": int(graph_version),
            "graph": graph_raw,
            "graph_fingerprint": graph_fingerprint,
            "config_hash": config_hash,
            "input_descriptor_hash": descriptor_hash,
            "input_fingerprint": input_fp,
            "input_summary": tensor_summary(input_array),
            "topology": [],
            "completed_nodes": [],
            "node_products": {},
            "failures": [],
            "injections_hit": [],
            "recalculated_nodes": [],
            "reused_nodes": [],
            "invalidated_nodes": [],
            "invalidation_chains": {},
            "removed_nodes": [],
            "execution_trace": [],
            "final_outputs": {},
            "phase_history": [],
        }
        self.save(snapshot)
        return snapshot

    def load(self) -> dict[str, Any]:
        if not self.initialized:
            raise BusinessError(E_IO, f"state is not initialized: {self.root}", stage="state")
        try:
            snapshot = load_json(self.current_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(E_IO, f"cannot load state snapshot: {exc}", stage="state") from exc
        if not isinstance(snapshot, dict) or snapshot.get("format_version") != FORMAT_VERSION:
            raise BusinessError(E_IO, "unsupported or malformed state snapshot", stage="state")
        return snapshot

    def save(self, snapshot: dict[str, Any]) -> Path:
        snapshot["phase_seq"] = int(snapshot.get("phase_seq", 0)) + 1
        snapshot["snapshot_version"] = FORMAT_VERSION
        topology_index = snapshot.get("topology_index", {}) if isinstance(snapshot.get("topology_index"), dict) else {}
        snapshot["completed_nodes"] = sorted(
            snapshot.get("completed_nodes", []),
            key=lambda item: (topology_index.get(item, 10**9), item),
        )
        version = int(snapshot.get("graph_version", 0))
        stage = str(snapshot.get("stage", "unknown")).replace("/", "_")
        archive = self.phase_dir / f"v{version:06d}" / f"{int(snapshot['phase_seq']):06d}_{stage}.json"
        history = snapshot.setdefault("phase_history", [])
        relative_archive = str(archive.relative_to(self.root))
        if not history or history[-1] != relative_archive:
            history.append(relative_archive)
        atomic_write_json(self.current_path, snapshot)
        atomic_write_json(archive, snapshot)
        return archive

    def load_config(self) -> dict[str, Any]:
        try:
            return load_json(self.config_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(E_IO, f"cannot load state config: {exc}", stage="state") from exc

    def load_input_descriptor(self) -> dict[str, Any]:
        try:
            return load_json(self.input_descriptor_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(E_INPUT_INVALID, f"cannot load stored input descriptor: {exc}", stage="state") from exc

    def load_input_array(self, expected_fingerprint: str | None = None) -> np.ndarray:
        try:
            array = np.load(self.input_array_path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise BusinessError(E_INPUT_INVALID, f"cannot load stored input tensor: {exc}", stage="state") from exc
        array = np.ascontiguousarray(array, dtype=np.float64)
        if expected_fingerprint is not None and tensor_fingerprint(array) != expected_fingerprint:
            raise BusinessError(E_INPUT_INVALID, "stored input tensor fingerprint mismatch", stage="state")
        return array

    def replace_input_array(self, array: np.ndarray, descriptor_raw: dict[str, Any]) -> dict[str, Any]:
        array = np.ascontiguousarray(array, dtype=np.float64)
        atomic_write_json(self.input_descriptor_path, descriptor_raw)
        atomic_write_npy(self.input_array_path, array)
        return {
            "input_fingerprint": tensor_fingerprint(array),
            "input_summary": tensor_summary(array),
            "input_descriptor_hash": sha256_json(descriptor_raw),
        }

    def product_path(self, cache_key: str) -> Path:
        return self.products_dir / f"{cache_key}.npy"

    def persist_product(self, cache_key: str, array: np.ndarray) -> Path:
        path = self.product_path(cache_key)
        atomic_write_npy(path, array)
        return path

    def load_product(self, record: dict[str, Any]) -> np.ndarray | None:
        cache_key = record.get("cache_key")
        expected = record.get("tensor_fingerprint")
        if not isinstance(cache_key, str) or not isinstance(expected, str):
            return None
        path = self.product_path(cache_key)
        if not path.is_file():
            return None
        try:
            array = np.load(path, allow_pickle=False)
        except (OSError, ValueError):
            return None
        array = np.ascontiguousarray(array, dtype=np.float64)
        if tensor_fingerprint(array) != expected:
            return None
        return array

    def verify_resume(
        self,
        snapshot: dict[str, Any],
        *,
        config_raw: dict[str, Any] | None = None,
        graph_raw: dict[str, Any] | None = None,
        graph_version: int | None = None,
    ) -> None:
        if config_raw is not None and sha256_json(config_raw) != snapshot.get("config_hash"):
            raise BusinessError(
                E_CONFIG_MISMATCH,
                "configuration hash does not match the snapshot",
                stage="resume",
                details={"snapshot_config_hash": snapshot.get("config_hash"), "current_config_hash": sha256_json(config_raw)},
            )
        if graph_raw is not None and sha256_json(graph_raw) != snapshot.get("graph_fingerprint"):
            raise BusinessError(
                E_CONFIG_MISMATCH,
                "graph content fingerprint does not match the snapshot",
                stage="resume",
                details={
                    "snapshot_graph_fingerprint": snapshot.get("graph_fingerprint"),
                    "current_graph_fingerprint": sha256_json(graph_raw),
                },
            )
        if graph_version is not None and int(graph_version) != int(snapshot.get("graph_version", -1)):
            raise BusinessError(
                E_CONFIG_MISMATCH,
                "graph version does not match the snapshot",
                stage="resume",
                details={"snapshot_graph_version": snapshot.get("graph_version"), "requested": int(graph_version)},
            )

    def copy_to(self, destination: Path) -> None:
        shutil.copytree(self.root, destination, dirs_exist_ok=False)
