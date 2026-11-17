"""Checkpoint persistence and recovery with replay-equivalence verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .engine import (
    CHECKPOINT_SCHEMA_VERSION,
    RULESET_VERSION,
    TraceState,
    apply_edit,
    apply_edits,
    build_state,
    extract_edit_items,
    state_hash,
)
from .errors import (
    E_CHECKPOINT_CORRUPT,
    E_CHECKPOINT_ORDER,
    E_CHECKPOINT_RULE,
    E_CHECKPOINT_SCHEMA,
    E_CHECKPOINT_STATE,
    E_CHECKPOINT_TEMP_RESIDUE,
    TraceError,
)
from .storage import Workspace, WorkspaceLock, atomic_write_json, json_digest


@dataclass
class RecoveryResult:
    state: TraceState
    mode: str
    notices: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_used: bool = False

    def as_recovery_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "checkpoint_used": self.checkpoint_used,
            "codes": sorted({item["code"] for item in self.notices}),
            "details": {"notices": self.notices},
        }


def _validate_checkpoint(value: Any) -> None:
    if not isinstance(value, dict):
        raise TraceError(E_CHECKPOINT_CORRUPT, "checkpoint must be an object")
    if value.get("complete") is not True:
        raise TraceError(E_CHECKPOINT_CORRUPT, "checkpoint is not marked complete")


def _validate_checkpoint_for_write(value: Any) -> None:
    _validate_checkpoint(value)
    if not isinstance(value.get("state"), Mapping):
        raise TraceError(E_CHECKPOINT_CORRUPT, "checkpoint state payload is invalid")
    try:
        state = TraceState.from_dict(dict(value["state"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TraceError(
            E_CHECKPOINT_CORRUPT,
            "checkpoint state payload cannot be reconstructed",
        ) from exc
    if value.get("state_hash") != state_hash(state):
        raise TraceError(
            E_CHECKPOINT_STATE,
            "checkpoint state hash failed pre-replace validation",
        )
    if value.get("processed_edits") != state.processed_edits:
        raise TraceError(
            E_CHECKPOINT_ORDER,
            "checkpoint edit watermark failed pre-replace validation",
        )


class CheckpointStore:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.path = workspace.checkpoint_file

    def _checkpoint_payload(self, state: TraceState) -> dict[str, Any]:
        watermarks = {
            document_id: document.latest_version
            for document_id, document in sorted(state.documents.items())
            if document.latest_version is not None
        }
        return {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "rule_version": RULESET_VERSION,
            "complete": True,
            "snapshot_watermarks": watermarks,
            "processed_edits": state.processed_edits,
            "last_edit_seq": state.last_edit_seq,
            "state": state.to_dict(),
            "state_hash": state_hash(state),
            "edit_log_digest": json_digest(state.edit_log),
        }

    def save(self, state: TraceState, lock: WorkspaceLock | None = None) -> str:
        self.workspace.ensure()
        active_lock = lock
        owns_lock = False
        if active_lock is None:
            active_lock = WorkspaceLock(self.workspace.root)
            active_lock.acquire()
            owns_lock = True
        elif not active_lock.held:
            active_lock.acquire()
            owns_lock = True
        try:
            payload = self._checkpoint_payload(state)
            return atomic_write_json(
                self.path, payload, validator=_validate_checkpoint_for_write
            )
        finally:
            if owns_lock and active_lock is not None:
                active_lock.release()

    def load_document(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            import json

            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError) as exc:
            raise TraceError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint JSON is not parseable",
                {"path": str(self.path), "error": str(exc)},
            ) from exc
        _validate_checkpoint(value)
        return value

    def temporary_residue(self) -> list[str]:
        if not self.path.parent.exists():
            return []
        return sorted(
            str(path)
            for path in self.path.parent.glob(f".{self.path.name}.*.tmp")
        )


def _notice(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}


def _max_raw_versions(snapshots: list[Any]) -> dict[str, int]:
    from .engine import parse_version, _document_id, _snapshot_version_raw

    result: dict[str, int] = {}
    for item in snapshots:
        if not isinstance(item, Mapping):
            continue
        try:
            document_id = _document_id(item)
            version = parse_version(_snapshot_version_raw(item))
        except TraceError:
            continue
        result[document_id] = max(result.get(document_id, 0), version)
    return result


def _checkpoint_state_from_document(value: Mapping[str, Any]) -> TraceState:
    if not isinstance(value.get("state"), Mapping):
        raise TraceError(E_CHECKPOINT_CORRUPT, "checkpoint state payload is invalid")
    state = TraceState.from_dict(dict(value["state"]))
    observed_hash = state_hash(state)
    if value.get("state_hash") != observed_hash:
        raise TraceError(
            E_CHECKPOINT_STATE,
            "checkpoint state hash does not match its payload",
            {"expected": value.get("state_hash"), "observed": observed_hash},
        )
    if value.get("processed_edits") != state.processed_edits:
        raise TraceError(
            E_CHECKPOINT_ORDER, "checkpoint edit watermark and state disagree"
        )
    if value.get("last_edit_seq") != state.last_edit_seq:
        raise TraceError(
            E_CHECKPOINT_ORDER, "checkpoint edit sequence watermark and state disagree"
        )
    return state


def restore(
    workspace: Workspace,
    snapshots: list[Any],
    edits: list[Any],
    resume_edits: bool = True,
) -> RecoveryResult:
    """Restore the latest complete checkpoint or rebuild without mixing rules."""

    store = CheckpointStore(workspace)
    notices: list[dict[str, Any]] = []
    for residue in store.temporary_residue():
        notices.append(
            _notice(
                E_CHECKPOINT_TEMP_RESIDUE,
                "ignored an incomplete temporary checkpoint file",
                path=residue,
            )
        )

    def full_rebuild(mode: str = "rebuild") -> RecoveryResult:
        state = build_state(snapshots)
        if resume_edits:
            apply_edits(state, edits)
        return RecoveryResult(
            state=state,
            mode=mode,
            notices=notices,
            checkpoint_used=False,
        )

    try:
        checkpoint = store.load_document()
    except TraceError as exc:
        notices.append(_notice(exc.code, exc.message, **exc.details))
        return full_rebuild()
    if checkpoint is None:
        return full_rebuild(mode="fresh")

    if checkpoint.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
        notices.append(
            _notice(
                E_CHECKPOINT_SCHEMA,
                "checkpoint schema changed; rebuilt from source",
                found=checkpoint.get("checkpoint_schema_version"),
                expected=CHECKPOINT_SCHEMA_VERSION,
            )
        )
        return full_rebuild()

    if checkpoint.get("rule_version") != RULESET_VERSION:
        notices.append(
            _notice(
                E_CHECKPOINT_RULE,
                "derivation rules changed; rebuilt without reusing old conclusions",
                found=checkpoint.get("rule_version"),
                expected=RULESET_VERSION,
            )
        )
        return full_rebuild()

    try:
        checkpoint_state = _checkpoint_state_from_document(checkpoint)
    except TraceError as exc:
        notices.append(_notice(exc.code, exc.message, **exc.details))
        return full_rebuild()

    raw_watermarks = checkpoint.get("snapshot_watermarks")
    if not isinstance(raw_watermarks, Mapping):
        notices.append(
            _notice(E_CHECKPOINT_CORRUPT, "checkpoint snapshot watermarks are invalid")
        )
        return full_rebuild()
    watermarks: dict[str, int] = {}
    for document_id, version in raw_watermarks.items():
        if (
            not isinstance(document_id, str)
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
        ):
            notices.append(
                _notice(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint contains an invalid snapshot watermark",
                    document_id=str(document_id),
                    version=version,
                )
            )
            return full_rebuild()
        watermarks[document_id] = version

    edit_items = extract_edit_items(edits)
    processed_edits = checkpoint.get("processed_edits")
    if (
        isinstance(processed_edits, bool)
        or not isinstance(processed_edits, int)
        or processed_edits < 0
        or processed_edits > len(edit_items)
    ):
        notices.append(
            _notice(
                E_CHECKPOINT_ORDER,
                "checkpoint edit watermark is outside the available edit sequence",
                processed_edits=processed_edits,
                available=len(edit_items),
            )
        )
        return full_rebuild()

    max_versions = _max_raw_versions(snapshots)
    for document_id, version in max_versions.items():
        checkpoint_version = watermarks.get(document_id)
        if checkpoint_version is None or version > checkpoint_version:
            notices.append(
                _notice(
                    E_CHECKPOINT_ORDER,
                    "source contains snapshots newer than the checkpoint watermark",
                    document_id=document_id,
                    checkpoint_version=checkpoint_version,
                    source_version=version,
                )
            )
            return full_rebuild()
    for document_id, version in watermarks.items():
        if max_versions.get(document_id, 0) < version:
            notices.append(
                _notice(
                    E_CHECKPOINT_ORDER,
                    "checkpoint snapshot watermark is beyond available source data",
                    document_id=document_id,
                    checkpoint_version=version,
                    source_version=max_versions.get(document_id, 0),
                )
            )
            return full_rebuild()

    replayed = build_state(snapshots, snapshot_watermarks=watermarks)
    apply_edits(replayed, edit_items[:processed_edits])
    if replayed.to_dict() != checkpoint_state.to_dict():
        notices.append(
            _notice(
                E_CHECKPOINT_STATE,
                "checkpoint payload differs from deterministic replay; full rebuild used",
                checkpoint_hash=checkpoint.get("state_hash"),
                replay_hash=state_hash(replayed),
            )
        )
        return full_rebuild()

    if resume_edits:
        for index in range(processed_edits, len(edit_items)):
            apply_edit(replayed, edit_items[index], index)
    return RecoveryResult(
        state=replayed,
        mode="resume",
        notices=notices,
        checkpoint_used=True,
    )


def run_edit_session(
    state: TraceState,
    edits_payload: Any,
    store: CheckpointStore,
    lock: WorkspaceLock,
    checkpoint_every: int = 1,
    crash_after: int | None = None,
) -> list[dict[str, Any]]:
    """Apply edits, checkpointing transactionally; optionally terminate at a boundary."""

    edits = extract_edit_items(edits_payload)
    outcomes: list[dict[str, Any]] = []
    starting_edit_count = state.processed_edits
    if checkpoint_every < 1:
        raise TraceError(E_CHECKPOINT_ORDER, "checkpoint_every must be at least 1")
    if crash_after is not None and starting_edit_count >= crash_after:
        store.save(state, lock=lock)
        from .errors import ForcedTermination

        raise ForcedTermination(state.processed_edits)

    for index in range(starting_edit_count, len(edits)):
        outcomes.append(apply_edit(state, edits[index], index))
        if state.processed_edits % checkpoint_every == 0:
            store.save(state, lock=lock)
        if crash_after is not None and state.processed_edits >= crash_after:
            store.save(state, lock=lock)
            from .errors import ForcedTermination

            raise ForcedTermination(state.processed_edits)

    store.save(state, lock=lock)
    return outcomes
