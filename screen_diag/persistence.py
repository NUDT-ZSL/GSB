from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .errors import SchemaError
from .hashing import canonical_json_bytes, sha256_hex


CHUNK_FORMAT = "screen-diag-chunk/v1"
INTERRUPT_MARKER = "INTERRUPTED.json"
MANIFEST_NAME = "chunk-manifest.json"


def atomic_write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256_hex(value)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return digest


class InterruptionController:
    def __init__(self, interrupt_after_chunks: Optional[int] = None):
        self.stop_requested = False
        self.reason: Optional[str] = None
        self.interrupt_after_chunks = interrupt_after_chunks
        self.written_chunks = 0
        self._previous_handlers: Dict[int, Any] = {}

    def request_stop(self, reason: str) -> None:
        if not self.stop_requested:
            self.reason = reason
        self.stop_requested = True

    def install_signal_handlers(self) -> None:
        handler: Callable[[int, Any], None] = self._handle_signal
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        if hasattr(signal, "SIGBREAK"):
            sigbreak = getattr(signal, "SIGBREAK")
            try:
                self._previous_handlers[sigbreak] = signal.getsignal(sigbreak)
                signal.signal(sigbreak, handler)
            except (ValueError, OSError):
                pass

    def restore_signal_handlers(self) -> None:
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        self._previous_handlers.clear()

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.request_stop(f"termination_signal:{signum}")

    def boundary_requested(self) -> bool:
        if (
            self.interrupt_after_chunks is not None
            and self.written_chunks >= self.interrupt_after_chunks
        ):
            self.request_stop(f"deterministic_boundary_after_{self.interrupt_after_chunks}_chunks")
        return self.stop_requested


@dataclass
class RecoveredState:
    chunk_count: int
    last_chunk_index: int
    last_boundary_logic_ms: Optional[int]
    chunks: List[Dict[str, Any]]
    invalid_chunk: Optional[Dict[str, Any]] = None
    records: List[Dict[str, Any]] = None  # type: ignore[assignment]


class ChunkStore:
    def __init__(self, state_dir: Path | str, version_id: str):
        self.state_dir = Path(state_dir)
        self.chunks_dir = self.state_dir / "chunks"
        self.manifest_path = self.state_dir / MANIFEST_NAME
        self.interrupt_path = self.state_dir / INTERRUPT_MARKER
        self.version_id = version_id
        self.manifest: Dict[str, Any] = {
            "format": CHUNK_FORMAT,
            "version_id": version_id,
            "chunks": [],
        }

    def initialize(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        if self.manifest_path.exists():
            try:
                existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SchemaError("chunk manifest is unreadable", {"path": str(self.manifest_path)}) from exc
            if existing.get("version_id") != self.version_id:
                raise SchemaError(
                    "state directory contains another input version",
                    {"existing_version_id": existing.get("version_id"), "version_id": self.version_id},
                )
            self.manifest = existing

    def _chunk_path(self, chunk_index: int) -> Path:
        return self.chunks_dir / f"chunk-{chunk_index:04d}.json"

    def existing_valid_prefix(self) -> RecoveredState:
        recovered = self.recover_latest()
        return recovered

    def recover_latest(self) -> RecoveredState:
        if not self.manifest_path.exists():
            return RecoveredState(0, -1, None, [], records=[])
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return RecoveredState(
                0,
                -1,
                None,
                [],
                {"chunk_index": None, "reason": f"manifest unreadable: {exc}"},
                records=[],
            )
        if manifest.get("version_id") != self.version_id:
            return RecoveredState(
                0,
                -1,
                None,
                [],
                {"chunk_index": None, "reason": "version_id mismatch"},
                records=[],
            )

        valid_chunks: List[Dict[str, Any]] = []
        valid_records: List[Dict[str, Any]] = []
        previous_digest: Optional[str] = None
        invalid: Optional[Dict[str, Any]] = None
        for record in manifest.get("chunks", []):
            index = record.get("chunk_index")
            path = Path(record.get("path") or self._chunk_path(int(index)))
            if not path.is_absolute():
                path = self.state_dir / path
            if not path.is_file():
                invalid = {"chunk_index": index, "path": str(path), "reason": "chunk file missing"}
                break
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                payload = envelope["payload"]
                envelope_digest = envelope["content_digest"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                invalid = {"chunk_index": index, "path": str(path), "reason": f"chunk unreadable: {exc}"}
                break

            actual_digest = sha256_hex(payload)
            if actual_digest != envelope_digest or actual_digest != record.get("content_digest"):
                invalid = {
                    "chunk_index": index,
                    "path": str(path),
                    "reason": "chunk digest mismatch",
                    "expected_digest": record.get("content_digest"),
                    "actual_digest": actual_digest,
                }
                break
            if record.get("previous_digest") != previous_digest:
                invalid = {"chunk_index": index, "path": str(path), "reason": "chunk chain mismatch"}
                break
            if payload.get("version_id") != self.version_id:
                invalid = {"chunk_index": index, "path": str(path), "reason": "chunk version_id mismatch"}
                break

            valid_chunks.append(payload)
            valid_records.append(record)
            previous_digest = actual_digest

        last = valid_chunks[-1] if valid_chunks else None
        return RecoveredState(
            chunk_count=len(valid_chunks),
            last_chunk_index=last.get("chunk_index", -1) if last else -1,
            last_boundary_logic_ms=(last or {}).get("boundary_logic_ms"),
            chunks=valid_chunks,
            invalid_chunk=invalid,
            records=valid_records,
        )

    def write_chunk(self, chunk_index: int, boundary_logic_ms: int, payload_body: Dict[str, Any]) -> Dict[str, Any]:
        previous_digest = self.manifest["chunks"][-1]["content_digest"] if self.manifest.get("chunks") else None
        payload = {
            "format": CHUNK_FORMAT,
            "version_id": self.version_id,
            "chunk_index": chunk_index,
            "boundary_logic_ms": boundary_logic_ms,
            "previous_digest": previous_digest,
            "body": payload_body,
        }
        content_digest = sha256_hex(payload)
        envelope = {"content_digest": content_digest, "payload": payload}
        path = self._chunk_path(chunk_index)
        atomic_write_json(path, envelope)

        relative_path = path.relative_to(self.state_dir).as_posix()
        self.manifest["version_id"] = self.version_id
        chunks = [item for item in self.manifest["chunks"] if item.get("chunk_index") != chunk_index]
        chunks.append(
            {
                "chunk_index": chunk_index,
                "boundary_logic_ms": boundary_logic_ms,
                "path": relative_path,
                "content_digest": content_digest,
                "previous_digest": previous_digest,
            }
        )
        chunks.sort(key=lambda item: item["chunk_index"])
        self.manifest["chunks"] = chunks
        atomic_write_json(self.manifest_path, self.manifest)
        return payload

    def write_interrupted_marker(self, controller: InterruptionController, after_chunk_index: int) -> None:
        marker = {
            "status": "interrupted_at_chunk_boundary",
            "exit_code": 130,
            "reason": controller.reason,
            "after_chunk_index": after_chunk_index,
            "version_id": self.version_id,
            "recover_with": "analyze --resume",
        }
        atomic_write_json(self.interrupt_path, marker)

    def clear_interrupted_marker(self) -> None:
        if self.interrupt_path.exists():
            self.interrupt_path.unlink()


def build_chunk_bodies(
    manifest: Any,
    merged: Dict[str, Any],
    replay_result: Any,
    comparison: Dict[str, Any],
) -> List[Dict[str, Any]]:
    interval = manifest.chunk_interval_ms
    max_logic = 0
    decision_logics = [decision.logic_ms for decision in replay_result.decisions]
    if decision_logics:
        max_logic = max(max_logic, max(decision_logics))
    point_logics = [point.get("logic_ms", 0) for point in merged.get("points", [])]
    if point_logics:
        max_logic = max(max_logic, max(point_logics))
    if max_logic == 0:
        max_logic = interval
    last_boundary = ((max_logic + interval - 1) // interval) * interval
    boundaries = list(range(interval, last_boundary + interval, interval))

    decision_dicts = [decision.to_dict() for decision in replay_result.decisions]
    chunks: List[Dict[str, Any]] = []
    previous_boundary = 0
    for chunk_index, boundary in enumerate(boundaries):
        decisions = [
            decision
            for decision in decision_dicts
            if previous_boundary < decision["logic_ms"] <= boundary
        ]
        points = [
            point
            for point in merged.get("points", [])
            if previous_boundary < point["logic_ms"] <= boundary
        ]
        conflicts = [
            conflict
            for conflict in merged.get("conflicts", [])
            if previous_boundary < conflict["logic_ms"] <= boundary
        ]
        screen_states: Dict[str, Any] = {}
        for session in manifest.sessions:
            for screen in session.screens:
                from .replay import state_at_logic_ms

                screen_states[screen.canonical_id] = state_at_logic_ms(manifest, replay_result, screen.screen_id, boundary)

        chunks.append(
            {
                "chunk_index": chunk_index,
                "boundary_logic_ms": boundary,
                "logic_range_ms": [previous_boundary + 1 if previous_boundary else 0, boundary],
                "decisions": decisions,
                "merged_points": points,
                "merged_conflicts": conflicts,
                "screen_states_at_boundary": screen_states,
                "comparison_summary": comparison.get("stats", {}),
            }
        )
        previous_boundary = boundary
    return chunks
