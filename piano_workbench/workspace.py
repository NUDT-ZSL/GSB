"""On-disk workspace: state tree, undo/redo pointers and content cache.

Layout::

    <root>/
        manifest.json          committed state (versioned, atomic replace)
        manifest.json.tmp      staging file (never read as a manifest)
        exit.json              last-shutdown marker
        cache/<sha256>.wav     content-addressed int16 PCM audio
        orphan/<sha256>.wav    caches found unreferenced after a hard kill

Durability rule: content caches are fully written and fsynced *before* the
manifest that references them is atomically replaced, so a crash at any point
leaves either the previous good state or the new complete state. Caches left
behind by an interrupted commit are reclaimed as orphans on the next open.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .errors import WorkbenchError, E_CACHE_HASH, E_IO, E_VERSION_CONFLICT
from .nodes import node_from_dict, node_to_dict
from .pcm import hash_pcm
from .constants import FORMAT_VERSION, SAMPLE_RATE

MANIFEST_NAME = "manifest.json"
EXIT_NAME = "exit.json"
CACHE_DIR = "cache"
ORPHAN_DIR = "orphan"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _atomic_write(path: Path, text: str) -> None:
    """Write text via a sibling temp file + fsync + atomic os.replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise WorkbenchError(E_IO, f"failed writing {path.name}: {exc}") from exc


class Workspace:
    """Owns the manifest document and the content-addressed cache."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.cache_dir = self.root / CACHE_DIR
        self.orphan_dir = self.root / ORPHAN_DIR
        self.manifest_path = self.root / MANIFEST_NAME
        self.exit_path = self.root / EXIT_NAME
        self.nodes: dict[str, dict] = {}
        self.root_id: str | None = None
        self.current_id: str | None = None
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self.sample_rate: int = SAMPLE_RATE
        self.sample_count: int = 0
        self.sequence: int = 0
        self.created_at: str = _utc_now()
        self.updated_at: str = self.created_at
        self.last_recovery: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # Creation / opening
    # ------------------------------------------------------------------ #
    @classmethod
    def create(
        cls,
        root: str | Path,
        root_node: dict,
        pcm: np.ndarray,
        sample_rate: int = SAMPLE_RATE,
    ) -> "Workspace":
        ws = cls(root)
        ws.root.mkdir(parents=True, exist_ok=True)
        if ws.manifest_path.exists():
            raise WorkbenchError(
                E_IO, f"workspace already exists: {ws.manifest_path}", {"root": str(ws.root)}
            )
        ws.sample_rate = int(sample_rate)
        ws.sample_count = int(pcm.size)
        ws.root_id = root_node["id"]
        ws.current_id = root_node["id"]
        content_hash = ws._write_cache(pcm)
        stored = dict(root_node)
        stored["cache"] = {"sha256": content_hash, "samples": int(pcm.size)}
        ws.nodes[stored["id"]] = stored
        ws.exit_path.unlink(missing_ok=True)
        ws._write_manifest(committed=True)
        return ws

    @classmethod
    def open(cls, root: str | Path, expected_version: int = FORMAT_VERSION) -> "Workspace":
        ws = cls(root)
        if not ws.manifest_path.exists():
            raise WorkbenchError(
                E_IO, "workspace manifest not found", {"root": str(ws.root)}
            )
        try:
            raw = json.loads(ws.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                E_IO, "manifest is unreadable; workspace left untouched", {"error": str(exc)}
            ) from exc

        version = int(raw.get("format_version", -1))
        if version != int(expected_version):
            raise WorkbenchError(
                E_VERSION_CONFLICT,
                f"manifest format_version {version} != supported {expected_version}",
                {"stored": version, "supported": expected_version},
            )

        ws.nodes = {}
        for item in raw["nodes"]:
            node = node_from_dict(item)
            ws.nodes[node["id"]] = node
        ws.root_id = raw["root_id"]
        ws.current_id = raw["current_id"]
        ws.undo_stack = list(raw.get("undo_stack", []))
        ws.redo_stack = list(raw.get("redo_stack", []))
        ws.sample_rate = int(raw["sample_rate"])
        ws.sample_count = int(raw["sample_count"])
        ws.sequence = int(raw.get("sequence", 0))
        ws.created_at = raw.get("created_at", ws.created_at)
        ws.updated_at = raw.get("updated_at", ws.updated_at)

        ws._validate_references()
        recovery = ws._recover_orphans()
        ws._verify_caches(recovery)
        ws.last_recovery = recovery
        # Consume the previous shutdown marker: a new session has begun.
        previous_exit = ws._read_exit_marker()
        if previous_exit is not None:
            recovery["previous_exit"] = previous_exit
        ws.exit_path.unlink(missing_ok=True)
        return ws

    # ------------------------------------------------------------------ #
    # Cache management
    # ------------------------------------------------------------------ #
    def cache_path(self, content_hash: str) -> Path:
        return self.cache_dir / f"{content_hash}.wav"

    def _write_cache(self, pcm: np.ndarray) -> str:
        """Write a cache WAV; returns the content hash. Idempotent."""

        pcm = np.asarray(pcm, dtype="<i2")
        content_hash = hash_pcm(pcm)
        target = self.cache_path(content_hash)
        if target.exists():
            return content_hash
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="pending-", suffix=".wav", dir=self.cache_dir)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                sf.write(handle, pcm, self.sample_rate, subtype="PCM_16", format="WAV")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise WorkbenchError(E_IO, f"failed writing cache: {exc}") from exc
        return content_hash

    def read_cache(self, content_hash: str) -> np.ndarray:
        target = self.cache_path(content_hash)
        if not target.exists():
            raise WorkbenchError(
                E_CACHE_HASH, "cache file missing", {"sha256": content_hash}
            )
        try:
            data, sr = sf.read(str(target), dtype="int16", always_2d=False)
        except Exception as exc:  # soundfile raises RuntimeError on bad WAVs
            raise WorkbenchError(
                E_CACHE_HASH, "cache file unreadable", {"sha256": content_hash}
            ) from exc
        pcm = np.asarray(data, dtype="<i2")
        actual = hash_pcm(pcm)
        if actual != content_hash:
            raise WorkbenchError(
                E_CACHE_HASH,
                "cache content hash does not match its name",
                {"expected": content_hash, "actual": actual},
            )
        if sr != self.sample_rate:
            raise WorkbenchError(
                E_CACHE_HASH,
                "cache sample rate differs from manifest",
                {"expected": self.sample_rate, "actual": int(sr)},
            )
        return pcm

    def node_pcm(self, node_id: str) -> np.ndarray:
        node = self.nodes.get(node_id)
        if node is None:
            raise WorkbenchError(E_CACHE_HASH, "unknown node", {"node_id": node_id})
        descriptor = node.get("cache")
        if not descriptor:
            raise WorkbenchError(
                E_CACHE_HASH, "node has no committed cache", {"node_id": node_id}
            )
        return self.read_cache(descriptor["sha256"])

    def _verify_caches(self, recovery: dict) -> None:
        bad: list[dict] = []
        for node in self.nodes.values():
            descriptor = node.get("cache")
            if not descriptor:
                bad.append({"node_id": node["id"], "reason": "missing_descriptor"})
                continue
            target = self.cache_path(descriptor["sha256"])
            if not target.exists():
                bad.append(
                    {"node_id": node["id"], "reason": "missing_file",
                     "sha256": descriptor["sha256"]}
                )
                continue
            try:
                pcm = self.read_cache(descriptor["sha256"])
            except WorkbenchError as exc:
                bad.append({"node_id": node["id"], "reason": exc.code, "detail": exc.detail})
                continue
            if pcm.size != int(descriptor.get("samples", pcm.size)):
                bad.append({"node_id": node["id"], "reason": "sample_count_mismatch"})
        if bad:
            raise WorkbenchError(
                E_CACHE_HASH,
                "committed cache verification failed; workspace left untouched",
                {"failures": bad},
            )
        recovery["verified_caches"] = len(self.nodes)

    def _recover_orphans(self) -> dict:
        """Move unreferenced cache files into the orphan quarantine."""

        referenced = {
            node["cache"]["sha256"]
            for node in self.nodes.values()
            if node.get("cache")
        }
        orphans: list[str] = []
        if self.cache_dir.exists():
            self.orphan_dir.mkdir(parents=True, exist_ok=True)
            for entry in sorted(self.cache_dir.iterdir()):
                if not entry.is_file() or entry.suffix != ".wav":
                    continue
                stem = entry.stem
                if stem not in referenced:
                    destination = self.orphan_dir / entry.name
                    if destination.exists():
                        destination.unlink()
                    shutil.move(str(entry), str(destination))
                    orphans.append(entry.name)
        return {"reclaimed_orphans": orphans, "orphan_count": len(orphans)}

    # ------------------------------------------------------------------ #
    # Manifest, commits and shutdown markers
    # ------------------------------------------------------------------ #
    def _validate_references(self) -> None:
        if self.root_id not in self.nodes:
            raise WorkbenchError(E_CACHE_HASH, "manifest root node is missing")
        if self.current_id not in self.nodes:
            raise WorkbenchError(E_CACHE_HASH, "manifest current node is missing")
        for node in self.nodes.values():
            parent = node["parent_id"]
            if parent is not None and parent not in self.nodes:
                raise WorkbenchError(
                    E_CACHE_HASH,
                    "node references a missing parent",
                    {"node_id": node["id"], "parent_id": parent},
                )
        for label, stack in (("undo_stack", self.undo_stack), ("redo_stack", self.redo_stack)):
            for node_id in stack:
                if node_id not in self.nodes:
                    raise WorkbenchError(
                        E_CACHE_HASH, f"{label} references missing node", {"node_id": node_id}
                    )

    def _manifest_document(self, committed: bool) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "committed": bool(committed),
            "sequence": self.sequence,
            "created_at": self.created_at,
            "updated_at": _utc_now(),
            "sample_rate": self.sample_rate,
            "sample_count": self.sample_count,
            "root_id": self.root_id,
            "current_id": self.current_id,
            "undo_stack": list(self.undo_stack),
            "redo_stack": list(self.redo_stack),
            "nodes": [node_to_dict(node) for node in self.nodes.values()],
        }

    def _write_manifest(self, committed: bool) -> None:
        document = self._manifest_document(committed)
        document["updated_at"] = _utc_now()
        self.updated_at = document["updated_at"]
        _atomic_write(self.manifest_path, json.dumps(document, indent=2, ensure_ascii=False))

    def commit(
        self,
        node: dict,
        pcm: np.ndarray,
        undo_stack: list[str],
        redo_stack: list[str],
        current_id: str,
    ) -> None:
        """Atomically attach a new node + cache and advance the pointers.

        The cache is durably written first; only then is the manifest replaced.
        Any failure raises before the in-memory state is mutated, so callers
        never observe a partial commit and the previous manifest stays valid.
        """

        if node["id"] in self.nodes:
            # Content-addressed duplicate: only the pointers may need updating.
            if current_id not in self.nodes or any(
                nid not in self.nodes for nid in undo_stack + redo_stack
            ):
                raise WorkbenchError(
                    E_CACHE_HASH, "duplicate node id with inconsistent pointers"
                )
        else:
            content_hash = self._write_cache(pcm)
            stored = dict(node)
            stored["cache"] = {"sha256": content_hash, "samples": int(pcm.size)}
            self.nodes[stored["id"]] = stored

        self.undo_stack = list(undo_stack)
        self.redo_stack = list(redo_stack)
        self.current_id = current_id
        self.sequence += 1
        self._write_manifest(committed=True)

    def update_pointers(
        self, current_id: str, undo_stack: list[str], redo_stack: list[str]
    ) -> None:
        for node_id in [current_id, *undo_stack, *redo_stack]:
            if node_id not in self.nodes:
                raise WorkbenchError(
                    E_CACHE_HASH, "pointer update references missing node", {"node_id": node_id}
                )
        self.current_id = current_id
        self.undo_stack = list(undo_stack)
        self.redo_stack = list(redo_stack)
        self.sequence += 1
        self._write_manifest(committed=True)

    def write_exit_marker(self, kind: str, detail: dict | None = None) -> None:
        marker = {
            "kind": kind,  # "controlled" or "forced"
            "at": _utc_now(),
            "sequence": self.sequence,
            "current_id": self.current_id,
            "detail": dict(detail or {}),
        }
        _atomic_write(self.exit_path, json.dumps(marker, indent=2, ensure_ascii=False))

    def _read_exit_marker(self) -> dict | None:
        if not self.exit_path.exists():
            return None
        try:
            return json.loads(self.exit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"kind": "unreadable", "at": None}

    def close(self) -> None:
        """Controlled shutdown: final committed manifest + honest marker."""

        self._write_manifest(committed=True)
        self.write_exit_marker("controlled")

    def export_wav(self, node_id: str, destination: str | Path) -> Path:
        pcm = self.node_pcm(node_id)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                sf.write(handle, pcm, self.sample_rate, subtype="PCM_16", format="WAV")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, destination)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise WorkbenchError(E_IO, f"failed exporting WAV: {exc}") from exc
        return destination

    def snapshot(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "sequence": self.sequence,
            "sample_rate": self.sample_rate,
            "sample_count": self.sample_count,
            "root_id": self.root_id,
            "current_id": self.current_id,
            "undo_depth": len(self.undo_stack),
            "redo_depth": len(self.redo_stack),
            "undo_stack": list(self.undo_stack),
            "redo_stack": list(self.redo_stack),
            "node_count": len(self.nodes),
            "nodes": [node_to_dict(node) for node in self.nodes.values()],
            "last_recovery": self.last_recovery,
        }
