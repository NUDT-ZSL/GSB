"""Persisted stage progress and cleanup for interrupted batches."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, read_json

STAGE_SAMPLE_INDEX = 1
STAGE_BATCH_PROCESSING = 2
STAGE_WINDOW_AND_BOUNDARY = 3
STAGE_SNAPSHOTS = 4
STAGE_REPORT = 5
STAGE_COMPLETE = 6


class RecoveryManager:
    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)
        self.state_path = self.workdir / "state" / "recovery.json"
        self.inflight_dir = self.workdir / "state" / "inflight"
        self.staging_dir = self.workdir / "state" / "staging"
        self.pending_dir = self.workdir / "cache" / "pending"
        for directory in (self.state_path.parent, self.inflight_dir, self.staging_dir, self.pending_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any] | None:
        if not self.state_path.is_file():
            return None
        return read_json(self.state_path)

    def save(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, state)

    def reset(self, plan_fingerprint: str, now_ms: int) -> dict[str, Any]:
        state = {
            "status": "running",
            "plan_fingerprint": plan_fingerprint,
            "committed_stage_index": STAGE_SAMPLE_INDEX,
            "stage_name": "sample_index",
            "committed_batches": [],
            "batch_cache_keys": {},
            "cleaned_unfinished": [],
            "recovery_notes": [],
            "signal": None,
            "started_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
        self.save(state)
        return state

    def prepare_for_plan(self, plan_fingerprint: str, now_ms: int) -> tuple[dict[str, Any], dict[str, Any]]:
        existing = self.load()
        cleaned = self.cleanup_unfinished()
        if (
            existing
            and existing.get("status") == "interrupted"
            and existing.get("plan_fingerprint") == plan_fingerprint
        ):
            existing["status"] = "running"
            existing["restarted_at_ms"] = now_ms
            existing["updated_at_ms"] = now_ms
            existing["cleaned_unfinished"] = cleaned
            existing["pending_cleanup"] = []
            note = (
                f"resumed from stage {existing.get('committed_stage_index')} "
                f"with {len(existing.get('committed_batches', []))} committed batches"
            )
            existing.setdefault("recovery_notes", []).append(note)
            self.save(existing)
            return existing, {"recovered": True, "cleaned": cleaned, "note": note}

        state = self.reset(plan_fingerprint, now_ms)
        state["cleaned_unfinished"] = cleaned
        if cleaned:
            state["recovery_notes"].append(f"cleaned {len(cleaned)} unfinished artifacts from stale run")
            self.save(state)
        return state, {"recovered": False, "cleaned": cleaned}

    def cleanup_unfinished(self) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        for directory, kind in (
            (self.inflight_dir, "inflight_marker"),
            (self.staging_dir, "staging_directory"),
            (self.pending_dir, "pending_cache_pointer"),
        ):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("**/*")):
                if path.is_file() or path.is_symlink():
                    cleaned.append({"kind": kind, "path": str(path.relative_to(self.workdir))})
                    path.unlink(missing_ok=True)
                elif path.is_dir() and kind == "staging_directory":
                    shutil.rmtree(path, ignore_errors=True)
                    cleaned.append({"kind": kind, "path": str(path.relative_to(self.workdir))})
        return cleaned

    def mark_stage(self, state: dict[str, Any], stage_index: int, stage_name: str, now_ms: int) -> None:
        state["committed_stage_index"] = stage_index
        state["stage_name"] = stage_name
        state["updated_at_ms"] = now_ms
        self.save(state)

    def begin_batch(self, batch_id: str, cache_key: str, payload: dict[str, Any] | None = None) -> None:
        safe = batch_id.replace("/", "_")
        atomic_write_json(
            self.inflight_dir / f"{safe}.json",
            {"batch_id": batch_id, "cache_key": cache_key, **(payload or {})},
        )
        atomic_write_json(
            self.pending_dir / f"{safe}.json",
            {"batch_id": batch_id, "cache_key": cache_key, "state": "pending"},
        )
        staging_path = self.staging_dir / safe
        staging_path.mkdir(parents=True, exist_ok=True)
        atomic_write_json(staging_path / "partial.json", {"batch_id": batch_id, "status": "computing"})

    def update_intermediate(self, batch_id: str, intermediate: dict[str, Any]) -> None:
        safe = batch_id.replace("/", "_")
        atomic_write_json(self.staging_dir / safe / "intermediate.json", intermediate)

    def commit_batch(self, state: dict[str, Any], batch_id: str, cache_key: str, now_ms: int) -> None:
        committed = state.setdefault("committed_batches", [])
        if batch_id not in committed:
            committed.append(batch_id)
        state.setdefault("batch_cache_keys", {})[batch_id] = cache_key
        state["committed_stage_index"] = STAGE_BATCH_PROCESSING
        state["stage_name"] = "batch_processing"
        state["updated_at_ms"] = now_ms
        safe = batch_id.replace("/", "_")
        for path in (
            self.inflight_dir / f"{safe}.json",
            self.pending_dir / f"{safe}.json",
        ):
            path.unlink(missing_ok=True)
        staging_path = self.staging_dir / safe
        if staging_path.exists():
            shutil.rmtree(staging_path, ignore_errors=True)
        self.save(state)

    def persist_interrupt(self, state: dict[str, Any], signum: int | str, now_ms: int) -> dict[str, Any]:
        state["status"] = "interrupted"
        state["signal"] = signum
        state["updated_at_ms"] = now_ms
        state["pending_cleanup"] = [
            str(path.relative_to(self.workdir))
            for directory in (self.inflight_dir, self.staging_dir, self.pending_dir)
            if directory.exists()
            for path in sorted(directory.glob("**/*"))
        ]
        state.setdefault("recovery_notes", []).append(
            f"persisted stage {state.get('committed_stage_index')} before exit for signal {signum}"
        )
        self.save(state)
        return state

    def complete(self, state: dict[str, Any], now_ms: int) -> None:
        state["status"] = "completed"
        state["committed_stage_index"] = STAGE_COMPLETE
        state["stage_name"] = "complete"
        state["updated_at_ms"] = now_ms
        self.save(state)
