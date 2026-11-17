"""Checkpoint persistence and incremental editing sessions.

A checkpoint captures the applied document-version watermark together with the
full lineage state needed to continue without reprocessing earlier snapshots.
On resume the engine is rebuilt exactly once from the checkpoint payload, so
the result is identical to a clean run.  Any structure/rules-version mismatch
forces a complete rebuild instead of mixing old conclusions with new rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .atomic import (
    IntegrityError,
    LockUnavailable,
    atomic_write_json,
    build_envelope,
    file_lock,
    read_envelope,
    verify_envelope,
)
from .errors import Diagnostic, E_CORRUPT_CHECKPOINT
from .lineage import LineageEngine
from .models import Derivation, Field, Snapshot, version_sort_key
from .parser import parse_snapshot

# Bump when the on-disk checkpoint structure changes.
STRUCTURE_VERSION = 1
# Bump when derivation declaration handling rules change.
RULES_VERSION = 2

CHECKPOINT_NAME = "checkpoint.json"
LOCK_NAME = ".checkpoint.lock"


@dataclass
class LoadResult:
    engine: Optional[LineageEngine]
    applied_versions: List[str]
    diagnostics: List[Diagnostic]
    rebuilt: bool


class CheckpointStore:
    """File-backed, lock-serialised checkpoint manager for a work directory."""

    def __init__(self, workdir: str) -> None:
        self.workdir = workdir
        self.checkpoint_path = os.path.join(workdir, CHECKPOINT_NAME)
        self.lock_path = os.path.join(workdir, LOCK_NAME)

    # ------------------------------------------------------------- serialisation
    @staticmethod
    def _serialize(engine: LineageEngine, applied_versions: List[str]) -> Dict[str, Any]:
        snapshots = []
        for snapshot in engine.snapshots:
            snapshots.append(
                {
                    "doc_id": snapshot.doc_id,
                    "version": snapshot.version,
                    "data": snapshot.data,
                    "changes": snapshot.changes,
                    "markers": snapshot.markers,
                    "source_file": snapshot.source_file,
                    "fields": [
                        {
                            "tokens": list(field.tokens),
                            "sensitivity": field.sensitivity,
                            "entry_seq": field.entry_seq,
                        }
                        for field in snapshot.fields
                    ],
                    "derivations": [
                        {
                            "source_tokens": list(derivation.source_tokens),
                            "target_tokens": list(derivation.target_tokens),
                            "decl_version": derivation.decl_version,
                            "transitive": derivation.transitive,
                            "entry_seq": derivation.entry_seq,
                        }
                        for derivation in snapshot.derivations
                    ],
                }
            )
        return {
            "applied_versions": sorted(applied_versions, key=version_sort_key),
            "parse_diagnostics": [
                item.to_dict() for item in engine.parse_diagnostics
            ],
            "snapshots": snapshots,
        }

    @staticmethod
    def _deserialize(payload: Dict[str, Any]) -> Tuple[LineageEngine, List[str]]:
        engine = LineageEngine()
        snapshots: List[Snapshot] = []
        for raw in payload["snapshots"]:
            snapshot = Snapshot(
                doc_id=raw["doc_id"],
                version=raw["version"],
                data=raw["data"],
                changes=list(raw.get("changes", [])),
                markers=list(raw.get("markers", [])),
                source_file=raw.get("source_file"),
            )
            snapshot.fields = [
                Field(
                    tokens=tuple(item["tokens"]),
                    sensitivity=item["sensitivity"],
                    entry_seq=item["entry_seq"],
                )
                for item in raw["fields"]
            ]
            snapshot.derivations = [
                Derivation(
                    source_tokens=tuple(item["source_tokens"]),
                    target_tokens=tuple(item["target_tokens"]),
                    decl_version=item["decl_version"],
                    transitive=item["transitive"],
                    entry_seq=item["entry_seq"],
                )
                for item in raw["derivations"]
            ]
            snapshots.append(snapshot)
        parse_diagnostics = [
            Diagnostic(**item) for item in payload.get("parse_diagnostics", [])
        ]
        engine.ingest_all(snapshots, parse_diagnostics)
        return engine, list(payload["applied_versions"])

    # ------------------------------------------------------------------- locking
    def save(self, engine: LineageEngine, applied_versions: List[str],
             crash_after_tmp: bool = False) -> None:
        """Persist a complete checkpoint under the work-directory lock."""

        try:
            with file_lock(self.lock_path):
                envelope = build_envelope(
                    STRUCTURE_VERSION,
                    RULES_VERSION,
                    self._serialize(engine, applied_versions),
                )
                atomic_write_json(
                    self.checkpoint_path, envelope, crash_after_tmp=crash_after_tmp
                )
        except LockUnavailable as exc:
            raise exc

    def load(self) -> LoadResult:
        """Load the last complete checkpoint or signal a full rebuild.

        A missing checkpoint means a fresh start.  A corrupt/incompatible
        checkpoint is reported and ignored so the caller rebuilds from scratch.
        """

        if not os.path.exists(self.checkpoint_path):
            return LoadResult(None, [], [], False)
        try:
            envelope = read_envelope(self.checkpoint_path)
            payload = verify_envelope(
                envelope, STRUCTURE_VERSION, RULES_VERSION
            )
            engine, applied = self._deserialize(payload)
            return LoadResult(engine, applied, [], False)
        except (IntegrityError, KeyError, ValueError, TypeError) as exc:
            diagnostic = Diagnostic(
                E_CORRUPT_CHECKPOINT,
                "",
                0,
                f"discarding incompatible checkpoint and rebuilding: {exc}",
                path=self.checkpoint_path,
            )
            return LoadResult(None, [], [diagnostic], True)

    @staticmethod
    def build_from_documents(documents: List[Dict[str, Any]]) -> Tuple[LineageEngine, List[str], List[Diagnostic], List[str]]:
        """Parse raw documents and build a fresh engine.

        Returns ``(engine, applied_versions, diagnostics, failed_sources)``.
        A single document failure never aborts the rest of the batch.
        """

        snapshots: List[Snapshot] = []
        diagnostics: List[Diagnostic] = []
        applied: List[str] = []
        failed: List[str] = []
        for raw in documents:
            source = raw.get("__source_file__") if isinstance(raw, dict) else None
            try:
                snapshot, snapshot_diagnostics = parse_snapshot(raw, source)
            except Exception as exc:  # whole-document failure: keep going
                failed.append(source or "<inline>")
                diagnostics.append(
                    Diagnostic(
                        "E_FIELD",
                        str(raw.get("version", "?")) if isinstance(raw, dict) else "?",
                        0,
                        f"document rejected: {exc}",
                    )
                )
                continue
            if snapshot is None:
                failed.append(source or "<inline>")
                continue
            diagnostics.extend(snapshot_diagnostics)
            snapshots.append(snapshot)
            applied.append(snapshot.version)

        engine = LineageEngine()
        engine.ingest_all(snapshots, diagnostics)
        return engine, sorted(set(applied), key=version_sort_key), engine.diagnostics, failed
