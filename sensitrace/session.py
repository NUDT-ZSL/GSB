"""High-level editing session orchestrating checkpoints and the engine."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .checkpoint import CheckpointStore
from .errors import Diagnostic
from .lineage import LineageEngine
from .models import Snapshot, version_sort_key
from .parser import parse_snapshot

MANIFEST_NAME = "manifest.json"


@dataclass
class ImportOutcome:
    engine: LineageEngine
    applied_versions: List[str]
    diagnostics: List[Diagnostic]
    failed_sources: List[str]
    resumed_from_checkpoint: bool
    rebuilt: bool


class Session:
    """A work-directory session with watermark-based incremental imports."""

    def __init__(self, workdir: str) -> None:
        self.workdir = workdir
        self.store = CheckpointStore(workdir)
        self.manifest_path = os.path.join(workdir, MANIFEST_NAME)
        os.makedirs(workdir, exist_ok=True)

    def _load_manifest(self) -> List[str]:
        if not os.path.exists(self.manifest_path):
            return []
        with open(self.manifest_path, "r", encoding="utf-8") as handle:
            return json.load(handle).get("sources", [])

    def _save_manifest(self, sources: List[str]) -> None:
        temporary = self.manifest_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"sources": sources}, handle, indent=2, sort_keys=True)
        os.replace(temporary, self.manifest_path)

    def import_documents(self, paths: List[str],
                         crash_after_tmp: bool = False) -> ImportOutcome:
        """Import JSON documents, resuming from the last complete checkpoint.

        Only document versions beyond the applied watermark are newly parsed;
        everything is rebuilt through the checkpoint payload so conclusions
        stay identical to a clean run.
        """

        load_result = self.store.load()
        resumed = load_result.engine is not None
        rebuild_notes = list(load_result.diagnostics)

        documents: List[Dict[str, Any]] = []
        failed_sources: List[str] = []
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                failed_sources.append(path)
                rebuild_notes.append(
                    Diagnostic("E_FIELD", "?", 0, f"cannot read document: {exc}", path=path)
                )
                continue
            if isinstance(raw, dict):
                raw["__source_file__"] = path
            documents.append(raw)

        if resumed:
            engine = load_result.engine
            applied = set(load_result.applied_versions)
            new_snapshots: List[Snapshot] = []
            parse_diagnostics: List[Diagnostic] = list(engine.parse_diagnostics)
            for raw in documents:
                source = raw.get("__source_file__")
                try:
                    snapshot, snapshot_diagnostics = parse_snapshot(raw, source)
                except Exception as exc:
                    failed_sources.append(source or "<inline>")
                    parse_diagnostics.append(
                        Diagnostic(
                            "E_FIELD",
                            str(raw.get("version", "?")) if isinstance(raw, dict) else "?",
                            0,
                            f"document rejected: {exc}",
                        )
                    )
                    continue
                if snapshot is None or snapshot.version in applied:
                    continue
                parse_diagnostics.extend(snapshot_diagnostics)
                new_snapshots.append(snapshot)
                applied.add(snapshot.version)
            for snapshot in new_snapshots:
                engine.ingest(snapshot)
            engine.parse_diagnostics = parse_diagnostics
            engine.rebuild()
            applied_versions = sorted(applied, key=version_sort_key)
        else:
            engine, applied_versions, parse_diagnostics, build_failed = (
                CheckpointStore.build_from_documents(documents)
            )
            failed_sources.extend(build_failed)
            engine.parse_diagnostics = rebuild_notes + engine.parse_diagnostics
            engine.rebuild()

        self.store.save(engine, applied_versions, crash_after_tmp=crash_after_tmp)
        self._save_manifest(sorted(set(self._load_manifest() + paths)))
        return ImportOutcome(
            engine=engine,
            applied_versions=applied_versions,
            diagnostics=engine.diagnostics,
            failed_sources=failed_sources,
            resumed_from_checkpoint=resumed,
            rebuilt=load_result.rebuilt,
        )

    def load_engine(self) -> Optional[LineageEngine]:
        """Return the engine described by the last complete checkpoint."""

        return self.store.load().engine

    def validate_edit(self, edited_path: str) -> Tuple[LineageEngine, List[Diagnostic]]:
        """Re-run all checks against an edited snapshot without mutating state.

        The edited document replaces the same-version snapshot in a fresh,
        in-memory engine so the user learns whether the edit breaks the masking
        conventions or derivation consistency before committing.
        """

        load_result = self.store.load()
        if load_result.engine is None:
            raise FileNotFoundError("no complete checkpoint; run import first")

        with open(edited_path, "r", encoding="utf-8") as handle:
            edited_raw = json.load(handle)
        edited_snapshot, edited_diagnostics = parse_snapshot(edited_raw, edited_path)

        base_snapshots = [
            snapshot for snapshot in load_result.engine.snapshots
            if snapshot.version != edited_snapshot.version
        ]
        base_snapshots.append(edited_snapshot)

        engine = LineageEngine()
        parse_diagnostics = [
            item for item in load_result.engine.parse_diagnostics
            if item.version != edited_snapshot.version
        ]
        parse_diagnostics.extend(edited_diagnostics)
        engine.ingest_all(base_snapshots, parse_diagnostics)
        return engine, engine.diagnostics
