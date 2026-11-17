"""Versioned checkpoints with atomic replacement and generation fencing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json_text
from .codes import Code
from .issues import Issue
from .models import FileResult
from .storage import FileLock, LockConflict, atomic_write_json_unlocked

CHECKPOINT_SCHEMA_VERSION = 1
ADJUDICATION_VERSION = 1


class CheckpointError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass
class CheckpointDecision:
    action: str
    generation: int
    completed: dict[str, FileResult] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    reason: str = ""


def hmac_keys_fingerprint(keys: dict[str, str | bytes]) -> str:
    safe = {}
    for key_id, key in sorted(keys.items()):
        material = key if isinstance(key, bytes) else key.encode("utf-8")
        safe[key_id] = hashlib.sha256(material).hexdigest()
    return hashlib.sha256(canonical_json_text(safe).encode("utf-8")).hexdigest()


def _accumulated_hash(completed: dict[str, FileResult]) -> str:
    state = [
        item.to_checkpoint_dict()
        for _, item in sorted(completed.items(), key=lambda pair: pair[0])
    ]
    return hashlib.sha256(canonical_json_text(state).encode("utf-8")).hexdigest()


class CheckpointManager:
    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path is not None else None
        self.lock_path = (
            self.path.with_name(self.path.name + ".lock") if self.path is not None else None
        )

    def load(
        self,
        *,
        algorithm_whitelist: Iterable[str],
        baseline_sha256: str,
        hmac_fingerprint: str,
        source_hashes: dict[str, str],
    ) -> CheckpointDecision:
        expected_algorithms = sorted(set(algorithm_whitelist))
        if self.path is None or self.lock_path is None:
            return CheckpointDecision(action="disabled", generation=0)
        try:
            with FileLock(self.lock_path, conflict_code=Code.CHECKPOINT_LOCK):
                if not self.path.exists():
                    return CheckpointDecision(action="fresh", generation=0)
                try:
                    raw = self.path.read_bytes()
                except OSError as exc:
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_read_error",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CORRUPT,
                                file_id=str(self.path),
                                details={"reason": str(exc)},
                            )
                        ],
                    )
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_corrupt",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CORRUPT,
                                file_id=str(self.path),
                                details={"reason": str(exc)},
                            )
                        ],
                    )

                if not isinstance(payload, dict):
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_corrupt",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CORRUPT,
                                file_id=str(self.path),
                                details={"reason": "checkpoint top-level must be an object"},
                            )
                        ],
                    )
                if payload.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_schema_version",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_VERSION,
                                file_id=str(self.path),
                                details={
                                    "found": payload.get("checkpoint_schema_version"),
                                    "expected": CHECKPOINT_SCHEMA_VERSION,
                                },
                            )
                        ],
                    )

                stored_algorithms = payload.get("algorithm_whitelist")
                if (
                    payload.get("adjudication_version") != ADJUDICATION_VERSION
                    or stored_algorithms != expected_algorithms
                    or payload.get("baseline_sha256") != baseline_sha256
                    or payload.get("hmac_keys_fingerprint") != hmac_fingerprint
                ):
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_config_changed",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CONFIG_CHANGED,
                                file_id=str(self.path),
                                details={
                                    "algorithm_whitelist": {
                                        "stored": stored_algorithms,
                                        "expected": expected_algorithms,
                                    },
                                    "baseline_changed": payload.get("baseline_sha256")
                                    != baseline_sha256,
                                    "hmac_keys_changed": payload.get(
                                        "hmac_keys_fingerprint"
                                    )
                                    != hmac_fingerprint,
                                },
                            )
                        ],
                    )

                completed_payload = payload.get("completed_files")
                if not isinstance(completed_payload, dict):
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_corrupt",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CORRUPT,
                                file_id=str(self.path),
                                details={"reason": "completed_files must be an object"},
                            )
                        ],
                    )
                completed: dict[str, FileResult] = {}
                try:
                    for source_id, item in completed_payload.items():
                        if not isinstance(source_id, str) or not isinstance(item, dict):
                            raise ValueError("invalid completed file entry")
                        completed[source_id] = FileResult.from_checkpoint_dict(item)
                except (KeyError, TypeError, ValueError) as exc:
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_corrupt",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CORRUPT,
                                file_id=str(self.path),
                                details={"reason": str(exc)},
                            )
                        ],
                    )

                missing_sources = sorted(set(completed) - set(source_hashes))
                if missing_sources:
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_evidence_set_changed",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CONFIG_CHANGED,
                                file_id=str(self.path),
                                details={"missing_sources": missing_sources},
                            )
                        ],
                    )
                changed_sources = sorted(
                    source_id
                    for source_id, file_result in completed.items()
                    if source_hashes.get(source_id) != file_result.sha256
                )
                if changed_sources:
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_source_changed",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CONFIG_CHANGED,
                                file_id=str(self.path),
                                details={"changed_sources": changed_sources},
                            )
                        ],
                    )

                expected_state_hash = payload.get("accumulated_chain_state_sha256")
                if expected_state_hash != _accumulated_hash(completed):
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_state_hash_mismatch",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CORRUPT,
                                file_id=str(self.path),
                                details={"reason": "accumulated chain-state hash mismatch"},
                            )
                        ],
                    )

                generation = payload.get("generation")
                if isinstance(generation, bool) or not isinstance(generation, int):
                    return CheckpointDecision(
                        action="rebuild",
                        generation=-1,
                        reason="checkpoint_corrupt",
                        issues=[
                            Issue(
                                Code.CHECKPOINT_CORRUPT,
                                file_id=str(self.path),
                                details={"reason": "generation must be an integer"},
                            )
                        ],
                    )
                return CheckpointDecision(
                    action="resume",
                    generation=generation,
                    completed=completed,
                )
        except LockConflict as exc:
            return CheckpointDecision(
                action="unavailable",
                generation=-1,
                reason="checkpoint_lock_conflict",
                issues=[
                    Issue(
                        Code.CHECKPOINT_LOCK,
                        file_id=str(self.path),
                        details={"lock_path": str(exc.path)},
                    )
                ],
            )

    def save(
        self,
        *,
        completed: dict[str, FileResult],
        expected_generation: int,
        algorithm_whitelist: Iterable[str],
        baseline_sha256: str,
        hmac_fingerprint: str,
        run_complete: bool,
    ) -> int:
        if self.path is None or self.lock_path is None:
            return expected_generation
        try:
            with FileLock(self.lock_path, conflict_code=Code.CHECKPOINT_LOCK):
                current_generation = 0
                if self.path.exists():
                    try:
                        current = json.loads(self.path.read_text(encoding="utf-8"))
                        current_generation = int(current.get("generation", 0))
                    except (OSError, ValueError, TypeError):
                        current_generation = 0
                if expected_generation >= 0 and current_generation != expected_generation:
                    raise CheckpointError(
                        Code.CHECKPOINT_CONFLICT,
                        "checkpoint generation changed before atomic replacement",
                    )

                generation = max(current_generation, expected_generation) + 1
                payload = {
                    "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "adjudication_version": ADJUDICATION_VERSION,
                    "algorithm_whitelist": sorted(set(algorithm_whitelist)),
                    "baseline_sha256": baseline_sha256,
                    "hmac_keys_fingerprint": hmac_fingerprint,
                    "generation": generation,
                    "run_complete": bool(run_complete),
                    "completed_source_ids": sorted(completed),
                    "completed_files": {
                        source_id: item.to_checkpoint_dict()
                        for source_id, item in sorted(completed.items())
                    },
                    "accumulated_chain_state_sha256": _accumulated_hash(completed),
                }
                atomic_write_json_unlocked(self.path, payload)
                return generation
        except LockConflict as exc:
            raise CheckpointError(
                Code.CHECKPOINT_LOCK,
                f"checkpoint lock is held: {exc.path}",
            ) from exc
