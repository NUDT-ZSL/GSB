"""Versioned, digest-protected snapshots and resume recovery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .constraints import (
    CandidateRejected,
    ConstraintViolation,
    check_constraint,
    check_state_integrity,
)
from .errors import SolverError, input_error
from .grid import LevelState, Room
from .model import LevelSpec, canonical_json, digest_value

SNAPSHOT_FORMAT = 1
MANIFEST_FORMAT = 1
MARKER_FORMAT = 1


@dataclass
class IntegrityIssue:
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class LoadedSnapshot:
    path: Path
    run_id: str
    spec_digest: str
    seed: int
    applied_count: int
    applied_constraints: tuple[str, ...]
    selected_attempts: tuple[int, ...]
    selected_attempt: int
    grid: tuple[tuple[int, ...], ...]
    rooms: tuple[Room, ...]
    rollback_count: int
    attempts_total: int
    commit_sequence: int
    parent_version_id: str | None
    version_id: str
    digest: str

    @property
    def state(self) -> LevelState:
        return LevelState(grid=self.grid, rooms=self.rooms)


@dataclass
class ResumeInfo:
    start_index: int
    state: LevelState
    rollback_count: int
    attempts_total: int
    selected_attempts: list[int]
    chain: list[LoadedSnapshot] = field(default_factory=list)
    warnings: list[IntegrityIssue] = field(default_factory=list)
    source: str = "fresh"
    restarted: bool = False
    snapshots_checked: int = 0
    history_complete: bool = True

    @property
    def latest_version_id(self) -> str | None:
        return self.chain[-1].version_id if self.chain else None


def _remove_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("digest", None)
    return result


def _with_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["digest"] = digest_value(_remove_digest(result))
    return result


def _verify_digest(payload: Any) -> bool:
    try:
        return (
            isinstance(payload, Mapping)
            and isinstance(payload.get("digest"), str)
            and payload["digest"] == digest_value(_remove_digest(payload))
        )
    except (TypeError, ValueError):
        return False


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(payload) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(str(exc)) from exc


def _unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def snapshot_version_payload(
    *,
    grid: Sequence[Sequence[int]],
    rooms: Sequence[Room],
    applied_count: int,
    applied_constraints: Sequence[str],
    selected_attempts: Sequence[int],
    seed: int,
    spec_digest: str,
    parent_version_id: str | None = None,
    commit_sequence: int = 0,
) -> dict[str, Any]:
    return {
        "grid": [list(row) for row in grid],
        "rooms": [room.canonical() for room in rooms],
        "applied_count": applied_count,
        "applied_constraints": list(applied_constraints),
        "selected_attempts": list(selected_attempts),
        "seed": seed,
        "spec_digest": spec_digest,
        "parent_version_id": parent_version_id,
        "commit_sequence": commit_sequence,
    }


class SnapshotStore:
    def __init__(self, root: str | Path, spec: LevelSpec) -> None:
        self.root = Path(root)
        self.spec = spec
        self.manifest_path = self.root / "manifest.json"
        self.marker_path = self.root / "interrupt.json"
        self.snapshots_dir = self.root / "snapshots"
        self.archive_dir = self.root / "archive"
        self.runs_archive = self.archive_dir / "runs"
        self.superseded_archive = self.archive_dir / "superseded"
        self.corrupt_archive = self.archive_dir / "corrupt"
        self._latest: LoadedSnapshot | None = None

    # ------------------------------------------------------------------
    # Layout and old-version preservation
    # ------------------------------------------------------------------
    def _ensure_layout(self) -> None:
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.runs_archive.mkdir(parents=True, exist_ok=True)
        self.superseded_archive.mkdir(parents=True, exist_ok=True)
        self.corrupt_archive.mkdir(parents=True, exist_ok=True)

    def _active_artifacts_exist(self) -> bool:
        if self.manifest_path.exists() or self.marker_path.exists():
            return True
        return self.snapshots_dir.exists() and any(self.snapshots_dir.iterdir())

    def _old_run_hint(self) -> str:
        try:
            manifest = _read_json(self.manifest_path)
            if isinstance(manifest, Mapping) and isinstance(
                manifest.get("run_id"), str
            ):
                return manifest["run_id"]
        except (ValueError, OSError):
            pass
        if self.snapshots_dir.exists():
            for path in sorted(self.snapshots_dir.glob("*.json")):
                try:
                    payload = _read_json(path)
                except (ValueError, OSError):
                    continue
                if isinstance(payload, Mapping) and isinstance(
                    payload.get("run_id"), str
                ):
                    return payload["run_id"]
        material = "\0".join(
            sorted(path.name for path in self.snapshots_dir.glob("*.json"))
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def archive_active(self, reason: str) -> str | None:
        if not self._active_artifacts_exist():
            return None
        old_run = self._old_run_hint()
        safe_reason = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in reason
        )[:40]
        destination = self.runs_archive / (
            f"{old_run[:16]}-{safe_reason}"
        )
        destination = _unique_destination(self.runs_archive, destination.name)
        destination.mkdir(parents=True, exist_ok=True)
        for path in (self.manifest_path, self.marker_path):
            if path.exists():
                shutil.move(str(path), str(destination / path.name))
        if self.snapshots_dir.exists():
            shutil.move(
                str(self.snapshots_dir), str(destination / self.snapshots_dir.name)
            )
        self._ensure_layout()
        return str(destination)

    def _archive_corrupt(self, path: Path) -> None:
        if not path.exists():
            return
        self.corrupt_archive.mkdir(parents=True, exist_ok=True)
        destination = _unique_destination(self.corrupt_archive, path.name)
        shutil.move(str(path), str(destination))

    def _archive_superseded(self, path: Path) -> None:
        if not path.exists():
            return
        self.superseded_archive.mkdir(parents=True, exist_ok=True)
        destination = _unique_destination(self.superseded_archive, path.name)
        shutil.move(str(path), str(destination))

    def archive_unreachable(self, keep: Sequence[LoadedSnapshot]) -> int:
        keep_paths = {snapshot.path.resolve() for snapshot in keep}
        archived = 0
        if not self.snapshots_dir.exists():
            return archived
        for path in sorted(self.snapshots_dir.glob("*.json")):
            if path.resolve() not in keep_paths:
                self._archive_superseded(path)
                archived += 1
        return archived

    # ------------------------------------------------------------------
    # Manifest and marker
    # ------------------------------------------------------------------
    def read_manifest(self) -> tuple[dict[str, Any] | None, IntegrityIssue | None]:
        if not self.manifest_path.exists():
            return None, None
        try:
            payload = _read_json(self.manifest_path)
        except ValueError as exc:
            return None, IntegrityIssue(
                "E_DIGEST", f"manifest is unreadable: {exc}", "manifest.json"
            )
        if not isinstance(payload, Mapping):
            return None, IntegrityIssue(
                "E_DIGEST", "manifest is not an object", "manifest.json"
            )
        if payload.get("format_version") != MANIFEST_FORMAT:
            return None, IntegrityIssue(
                "E_DIGEST", "manifest format version is unsupported", "manifest.json"
            )
        if not _verify_digest(payload):
            return None, IntegrityIssue(
                "E_DIGEST", "manifest digest is missing or does not match", "manifest.json"
            )
        return dict(payload), None

    def read_marker(self) -> tuple[dict[str, Any] | None, IntegrityIssue | None]:
        if not self.marker_path.exists():
            return None, None
        try:
            payload = _read_json(self.marker_path)
        except ValueError as exc:
            return None, IntegrityIssue(
                "E_DIGEST", f"interrupt marker is unreadable: {exc}", "interrupt.json"
            )
        if not isinstance(payload, Mapping):
            return None, IntegrityIssue(
                "E_DIGEST", "interrupt marker is not an object", "interrupt.json"
            )
        if payload.get("format_version") != MARKER_FORMAT:
            return None, IntegrityIssue(
                "E_DIGEST",
                "interrupt marker format version is unsupported",
                "interrupt.json",
            )
        if not _verify_digest(payload):
            return None, IntegrityIssue(
                "E_DIGEST",
                "interrupt marker digest is missing or does not match",
                "interrupt.json",
            )
        return dict(payload), None

    def _manifest_payload(
        self,
        *,
        status: str,
        latest: LoadedSnapshot | None,
        rollback_count: int,
        attempts_total: int,
        superseded: Sequence[Mapping[str, Any]] = (),
        unresolved: Sequence[str] = (),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "format_version": MANIFEST_FORMAT,
            "run_id": self.spec.run_id,
            "spec_digest": self.spec.spec_digest,
            "seed": self.spec.seed,
            "status": status,
            "latest_snapshot": latest.path.name if latest else None,
            "latest_version_id": latest.version_id if latest else None,
            "latest_snapshot_digest": latest.digest if latest else None,
            "applied_count": latest.applied_count if latest else 0,
            "commit_sequence": latest.commit_sequence if latest else 0,
            "rollback_count": rollback_count,
            "attempts_total": attempts_total,
            "superseded_versions": [dict(item) for item in superseded],
            "unresolved_constraints": list(unresolved),
        }
        return _with_digest(payload)

    def _write_manifest(
        self,
        *,
        status: str,
        latest: LoadedSnapshot | None,
        rollback_count: int,
        attempts_total: int,
        superseded: Sequence[Mapping[str, Any]] = (),
        unresolved: Sequence[str] = (),
    ) -> None:
        payload = self._manifest_payload(
            status=status,
            latest=latest,
            rollback_count=rollback_count,
            attempts_total=attempts_total,
            superseded=superseded,
            unresolved=unresolved,
        )
        _atomic_write_json(self.manifest_path, payload)

    def update_manifest(
        self,
        latest: LoadedSnapshot,
        *,
        status: str = "running",
        rollback_count: int,
        attempts_total: int,
    ) -> None:
        old_manifest, _issue = self.read_manifest()
        superseded: list[dict[str, Any]] = []
        if old_manifest is not None and old_manifest.get("run_id") == self.spec.run_id:
            superseded.extend(old_manifest.get("superseded_versions", []))
            old_version = old_manifest.get("latest_version_id")
            if old_version and old_version != latest.version_id:
                marker = {
                    "version_id": old_version,
                    "snapshot": old_manifest.get("latest_snapshot"),
                    "digest": old_manifest.get("latest_snapshot_digest"),
                    "reason": "replaced",
                }
                if not any(
                    item.get("version_id") == old_version for item in superseded
                ):
                    superseded.append(marker)
        self._write_manifest(
            status=status,
            latest=latest,
            rollback_count=rollback_count,
            attempts_total=attempts_total,
            superseded=superseded,
        )
        self._latest = latest

    def mark_completed(
        self,
        latest: LoadedSnapshot | None,
        *,
        rollback_count: int,
        attempts_total: int,
    ) -> None:
        self._write_manifest(
            status="completed",
            latest=latest,
            rollback_count=rollback_count,
            attempts_total=attempts_total,
        )
        self.clear_interrupt()

    def mark_conflict(
        self,
        latest: LoadedSnapshot | None,
        *,
        rollback_count: int,
        attempts_total: int,
        unresolved: Sequence[str],
    ) -> None:
        self._write_manifest(
            status="conflict",
            latest=latest,
            rollback_count=rollback_count,
            attempts_total=attempts_total,
            unresolved=unresolved,
        )

    def write_interrupt(
        self,
        latest: LoadedSnapshot | None,
        *,
        rollback_count: int,
        attempts_total: int,
        applied_count: int,
        reason: str,
    ) -> Path:
        self._write_manifest(
            status="interrupted",
            latest=latest,
            rollback_count=rollback_count,
            attempts_total=attempts_total,
        )
        marker = _with_digest(
            {
                "format_version": MARKER_FORMAT,
                "run_id": self.spec.run_id,
                "spec_digest": self.spec.spec_digest,
                "seed": self.spec.seed,
                "status": "interrupted",
                "reason": reason,
                "applied_count": applied_count,
                "commit_sequence": latest.commit_sequence if latest else 0,
                "latest_snapshot": latest.path.name if latest else None,
                "latest_version_id": latest.version_id if latest else None,
                "latest_snapshot_digest": latest.digest if latest else None,
                "rollback_count": rollback_count,
                "attempts_total": attempts_total,
            }
        )
        _atomic_write_json(self.marker_path, marker)
        return self.marker_path

    def clear_interrupt(self) -> None:
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Snapshot validation and writing
    # ------------------------------------------------------------------
    def _validate_snapshot(
        self, path: Path, payload: Any
    ) -> tuple[LoadedSnapshot | None, IntegrityIssue | None]:
        label = str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)
        if not isinstance(payload, Mapping):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot is not a JSON object", label
            )
        if payload.get("format_version") != SNAPSHOT_FORMAT:
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot format version is unsupported", label
            )
        if not _verify_digest(payload):
            return None, IntegrityIssue(
                "E_DIGEST",
                "snapshot content digest is missing or does not match",
                label,
            )
        if payload.get("run_id") != self.spec.run_id:
            return None, IntegrityIssue(
                "E_VERSION", "snapshot belongs to another run", label
            )
        if payload.get("spec_digest") != self.spec.spec_digest:
            return None, IntegrityIssue(
                "E_VERSION", "snapshot specification digest is stale", label
            )
        if payload.get("seed") != self.spec.seed:
            return None, IntegrityIssue(
                "E_VERSION", "snapshot seed is stale", label
            )

        applied_count = payload.get("applied_count")
        if (
            isinstance(applied_count, bool)
            or not isinstance(applied_count, int)
            or applied_count < 0
            or applied_count > len(self.spec.constraints)
        ):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot applied_count is invalid", label
            )
        applied_constraints = payload.get("applied_constraints")
        selected_attempts = payload.get("selected_attempts")
        if (
            not isinstance(applied_constraints, list)
            or len(applied_constraints) != applied_count
            or tuple(applied_constraints)
            != tuple(self.spec.constraint_ids()[:applied_count])
        ):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot applied constraint sequence is invalid", label
            )
        if (
            not isinstance(selected_attempts, list)
            or len(selected_attempts) != applied_count
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value >= self.spec.config.attempt_limit
                for value in selected_attempts
            )
        ):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot selected attempts are invalid", label
            )

        grid = payload.get("grid")
        rooms_raw = payload.get("rooms")
        if not isinstance(grid, list) or len(grid) != self.spec.height:
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot grid height is invalid", label
            )
        normalized_grid: list[list[int]] = []
        for row in grid:
            if not isinstance(row, list) or len(row) != self.spec.width:
                return None, IntegrityIssue(
                    "E_DIGEST", "snapshot grid width is invalid", label
                )
            normalized_row = []
            for value in row:
                if isinstance(value, bool) or value not in (-1, 0, 1, 2):
                    return None, IntegrityIssue(
                        "E_DIGEST", "snapshot grid contains an invalid cell", label
                    )
                normalized_row.append(int(value))
            normalized_grid.append(normalized_row)
        if not isinstance(rooms_raw, list):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot rooms metadata is missing", label
            )
        rooms: list[Room] = []
        for room_raw in rooms_raw:
            if not isinstance(room_raw, Mapping):
                return None, IntegrityIssue(
                    "E_DIGEST", "snapshot room metadata is invalid", label
                )
            values = [room_raw.get(key) for key in ("x", "y", "w", "h")]
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in values
            ):
                return None, IntegrityIssue(
                    "E_DIGEST", "snapshot room metadata is invalid", label
                )
            rooms.append(Room(*values))
        state = LevelState.from_lists(normalized_grid, rooms)
        try:
            check_state_integrity(self.spec, state)
        except CandidateRejected as exc:
            return None, IntegrityIssue(
                "E_DIGEST", f"snapshot state is inconsistent: {exc.message}", label
            )
        for satisfied in self.spec.constraints[:applied_count]:
            try:
                check_constraint(self.spec, satisfied, state)
            except ConstraintViolation as exc:
                return None, IntegrityIssue(
                    "E_DIGEST",
                    f"snapshot does not satisfy applied constraint {satisfied.id}: "
                    f"{exc.message}",
                    label,
                )

        parent = payload.get("parent_version_id")
        if parent is not None and not isinstance(parent, str):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot parent version id is invalid", label
            )
        commit_sequence = payload.get("commit_sequence")
        if (
            isinstance(commit_sequence, bool)
            or not isinstance(commit_sequence, int)
            or commit_sequence < 1
        ):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot commit sequence is invalid", label
            )
        version_payload = snapshot_version_payload(
            grid=normalized_grid,
            rooms=rooms,
            applied_count=applied_count,
            applied_constraints=applied_constraints,
            selected_attempts=selected_attempts,
            seed=self.spec.seed,
            spec_digest=self.spec.spec_digest,
            parent_version_id=parent,
            commit_sequence=commit_sequence,
        )
        expected_version = digest_value(version_payload)
        if payload.get("version_id") != expected_version:
            return None, IntegrityIssue(
                "E_DIGEST",
                "snapshot version identifier does not match its content",
                label,
            )
        rollback_count = payload.get("rollback_count")
        attempts_total = payload.get("attempts_total")
        if (
            isinstance(rollback_count, bool)
            or not isinstance(rollback_count, int)
            or rollback_count < 0
            or isinstance(attempts_total, bool)
            or not isinstance(attempts_total, int)
            or attempts_total < applied_count
        ):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot counters are invalid", label
            )
        selected_attempt = payload.get("selected_attempt")
        if selected_attempt != (
            selected_attempts[-1] if selected_attempts else None
        ):
            return None, IntegrityIssue(
                "E_DIGEST", "snapshot selected_attempt is inconsistent", label
            )
        return (
            LoadedSnapshot(
                path=path,
                run_id=str(payload["run_id"]),
                spec_digest=str(payload["spec_digest"]),
                seed=int(payload["seed"]),
                applied_count=applied_count,
                applied_constraints=tuple(str(item) for item in applied_constraints),
                selected_attempts=tuple(int(item) for item in selected_attempts),
                selected_attempt=int(selected_attempt),
                grid=tuple(tuple(row) for row in normalized_grid),
                rooms=tuple(rooms),
                rollback_count=rollback_count,
                attempts_total=attempts_total,
                commit_sequence=commit_sequence,
                parent_version_id=parent,
                version_id=str(payload["version_id"]),
                digest=str(payload["digest"]),
            ),
            None,
        )

    def load_snapshot_file(
        self, path: Path
    ) -> tuple[LoadedSnapshot | None, IntegrityIssue | None]:
        try:
            payload = _read_json(path)
        except ValueError as exc:
            label = (
                str(path.relative_to(self.root))
                if path.is_relative_to(self.root)
                else str(path)
            )
            return None, IntegrityIssue(
                "E_DIGEST", f"snapshot is unreadable: {exc}", label
            )
        return self._validate_snapshot(path, payload)

    def write_snapshot(
        self,
        state: LevelState,
        *,
        applied_count: int,
        selected_attempts: Sequence[int],
        rollback_count: int,
        attempts_total: int,
        parent: LoadedSnapshot | None,
    ) -> LoadedSnapshot:
        applied_constraints = self.spec.constraint_ids()[:applied_count]
        previous_sequence = parent.commit_sequence if parent else 0
        manifest, _manifest_issue = self.read_manifest()
        if manifest is not None and manifest.get("run_id") == self.spec.run_id:
            manifest_sequence = manifest.get("commit_sequence", 0)
            if not isinstance(manifest_sequence, bool) and isinstance(
                manifest_sequence, int
            ):
                previous_sequence = max(previous_sequence, manifest_sequence)
        commit_sequence = previous_sequence + 1
        version_payload = snapshot_version_payload(
            grid=state.grid,
            rooms=state.rooms,
            applied_count=applied_count,
            applied_constraints=applied_constraints,
            selected_attempts=selected_attempts,
            seed=self.spec.seed,
            spec_digest=self.spec.spec_digest,
            parent_version_id=parent.version_id if parent else None,
            commit_sequence=commit_sequence,
        )
        version_id = digest_value(version_payload)
        payload = _with_digest(
            {
                "format_version": SNAPSHOT_FORMAT,
                "run_id": self.spec.run_id,
                "spec_digest": self.spec.spec_digest,
                "seed": self.spec.seed,
                "width": self.spec.width,
                "height": self.spec.height,
                "applied_count": applied_count,
                "applied_constraints": applied_constraints,
                "selected_attempts": list(selected_attempts),
                "constraint_index": applied_count - 1,
                "selected_attempt": selected_attempts[-1],
                "grid": [list(row) for row in state.grid],
                "rooms": [room.canonical() for room in state.rooms],
                "rollback_count": rollback_count,
                "attempts_total": attempts_total,
                "commit_sequence": commit_sequence,
                "parent_version_id": parent.version_id if parent else None,
                "version_id": version_id,
            }
        )
        filename = f"step-{applied_count:06d}-{version_id[:16]}.json"
        path = self.snapshots_dir / filename
        if path.exists():
            existing, issue = self.load_snapshot_file(path)
            if existing is not None and existing.digest == payload["digest"]:
                return existing
            self._archive_corrupt(path)
            raise SolverError(
                "E_DIGEST",
                "an existing snapshot with the same version failed validation",
                {"issue": issue.as_dict() if issue else None},
            )
        _atomic_write_json(path, payload)
        loaded, issue = self.load_snapshot_file(path)
        if loaded is None:
            raise SolverError(
                "E_DIGEST",
                "newly written snapshot failed digest validation",
                {"issue": issue.as_dict() if issue else None},
            )
        self._latest = loaded
        return loaded

    # ------------------------------------------------------------------
    # Resume and integrity reporting
    # ------------------------------------------------------------------
    def _scan_snapshots(
        self,
    ) -> tuple[dict[str, LoadedSnapshot], list[IntegrityIssue], int]:
        valid: dict[str, LoadedSnapshot] = {}
        issues: list[IntegrityIssue] = []
        checked = 0
        if not self.snapshots_dir.exists():
            return valid, issues, checked
        for path in sorted(self.snapshots_dir.glob("*.json")):
            checked += 1
            loaded, issue = self.load_snapshot_file(path)
            if loaded is None:
                if issue is not None:
                    issues.append(issue)
                continue
            valid[loaded.version_id] = loaded
        return valid, issues, checked

    @staticmethod
    def _choose_latest(
        valid: Mapping[str, LoadedSnapshot],
        excluded_versions: Iterable[str] = (),
    ) -> LoadedSnapshot | None:
        excluded = set(excluded_versions)
        candidates = [
            snapshot
            for version, snapshot in valid.items()
            if version not in excluded
        ]
        if not candidates:
            candidates = list(valid.values())
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda snapshot: (
                snapshot.commit_sequence,
                snapshot.applied_count,
                snapshot.version_id,
            ),
        )

    @classmethod
    def _recover_latest(
        cls,
        valid: Mapping[str, LoadedSnapshot],
        manifest_latest: LoadedSnapshot | None,
        excluded_versions: Iterable[str],
    ) -> LoadedSnapshot | None:
        excluded = set(excluded_versions)
        candidate = cls._choose_latest(valid, excluded)
        if (
            manifest_latest is not None
            and manifest_latest.version_id in excluded
        ):
            manifest_latest = None
        if manifest_latest is None:
            return candidate
        if candidate is None:
            return manifest_latest
        # Prefer the authenticated manifest pointer on a commit-sequence tie,
        # but accept a higher persisted sequence (a crash between snapshot
        # write and manifest update).
        if candidate.commit_sequence > manifest_latest.commit_sequence:
            return candidate
        return manifest_latest

    def _build_chain(
        self, latest: LoadedSnapshot, valid: Mapping[str, LoadedSnapshot]
    ) -> tuple[list[LoadedSnapshot], bool]:
        reversed_chain: list[LoadedSnapshot] = []
        seen: set[str] = set()
        current: LoadedSnapshot | None = latest
        complete = True
        while current is not None:
            if current.version_id in seen:
                complete = False
                break
            seen.add(current.version_id)
            reversed_chain.append(current)
            if current.parent_version_id is None:
                break
            parent = valid.get(current.parent_version_id)
            if parent is None:
                complete = False
                break
            current = parent
        chain = list(reversed(reversed_chain))
        if chain:
            expected = list(range(1, chain[-1].applied_count + 1))
            actual = [snapshot.applied_count for snapshot in chain]
            if actual != expected:
                complete = False
            sequences = [snapshot.commit_sequence for snapshot in chain]
            if any(
                current <= previous
                for previous, current in zip(sequences, sequences[1:])
            ):
                complete = False
        return chain, complete

    def prepare(
        self, *, fresh: bool, strict_digest: bool
    ) -> ResumeInfo:
        if fresh and self._active_artifacts_exist():
            self.archive_active("fresh")
        if not self._active_artifacts_exist():
            self._ensure_layout()
            return ResumeInfo(
                start_index=0,
                state=LevelState.initial(
                    self.spec.width, self.spec.height, self.spec.blocked
                ),
                rollback_count=0,
                attempts_total=0,
                selected_attempts=[],
                source="fresh",
                restarted=True,
            )

        self._ensure_layout()
        issues: list[IntegrityIssue] = []
        manifest, manifest_issue = self.read_manifest()
        if manifest_issue is not None:
            issues.append(manifest_issue)
        marker, marker_issue = self.read_marker()
        if marker_issue is not None:
            issues.append(marker_issue)

        # A different run/specification must never reuse the active directory.
        if manifest is not None and manifest.get("run_id") != self.spec.run_id:
            self.archive_active("run_changed")
            self._ensure_layout()
            return ResumeInfo(
                start_index=0,
                state=LevelState.initial(
                    self.spec.width, self.spec.height, self.spec.blocked
                ),
                rollback_count=0,
                attempts_total=0,
                selected_attempts=[],
                warnings=issues,
                source="fresh_after_run_change",
                restarted=True,
            )
        if marker is not None and marker.get("run_id") != self.spec.run_id:
            self.archive_active("run_changed")
            self._ensure_layout()
            return ResumeInfo(
                start_index=0,
                state=LevelState.initial(
                    self.spec.width, self.spec.height, self.spec.blocked
                ),
                rollback_count=0,
                attempts_total=0,
                selected_attempts=[],
                warnings=issues,
                source="fresh_after_run_change",
                restarted=True,
            )

        valid, scan_issues, checked = self._scan_snapshots()
        issues.extend(scan_issues)
        excluded_versions: set[str] = set()
        if manifest is not None:
            for item in manifest.get("superseded_versions", []):
                if isinstance(item, Mapping) and isinstance(
                    item.get("version_id"), str
                ):
                    excluded_versions.add(item["version_id"])

        latest: LoadedSnapshot | None = None
        source = ""
        manifest_latest: LoadedSnapshot | None = None
        if manifest is not None:
            version = manifest.get("latest_version_id")
            if isinstance(version, str):
                manifest_latest = valid.get(version)
                if manifest_latest is None and manifest.get("latest_snapshot"):
                    issues.append(
                        IntegrityIssue(
                            "E_DIGEST",
                            "manifest references an invalid or missing snapshot",
                            str(manifest.get("latest_snapshot")),
                        )
                    )
                elif manifest_latest is not None and (
                    manifest.get("latest_snapshot_digest")
                    != manifest_latest.digest
                    or manifest.get("applied_count")
                    != manifest_latest.applied_count
                    or manifest.get("commit_sequence")
                    != manifest_latest.commit_sequence
                ):
                    issues.append(
                        IntegrityIssue(
                            "E_DIGEST",
                            "manifest metadata disagrees with its latest snapshot",
                            "manifest.json",
                        )
                    )
                    manifest_latest = None

        marker_version = marker.get("latest_version_id") if marker else None
        marker_latest = (
            valid.get(marker_version) if isinstance(marker_version, str) else None
        )
        marker_checked = marker is not None and marker_issue is None
        marker_metadata_mismatch = False
        if marker_checked and marker_latest is not None and (
            marker.get("latest_snapshot_digest") != marker_latest.digest
            or marker.get("applied_count") != marker_latest.applied_count
            or marker.get("commit_sequence") != marker_latest.commit_sequence
        ):
            marker_metadata_mismatch = True
            issues.append(
                IntegrityIssue(
                    "E_DIGEST",
                    "interrupt marker metadata disagrees with its snapshot",
                    "interrupt.json",
                )
            )
        if (
            marker_checked
            and isinstance(marker_version, str)
            and marker_latest is None
        ):
            issues.append(
                IntegrityIssue(
                    "E_DIGEST",
                    "interrupt marker references a missing or invalid snapshot",
                    "interrupt.json",
                )
            )
        if strict_digest and issues:
            raise SolverError(
                "E_DIGEST",
                "snapshot store contains invalid or stale integrity metadata",
                {"issues": [issue.as_dict() for issue in issues]},
            )

        if marker_checked:
            if marker_metadata_mismatch:
                self.archive_active("marker_metadata_mismatch")
                return ResumeInfo(
                    start_index=0,
                    state=LevelState.initial(
                        self.spec.width, self.spec.height, self.spec.blocked
                    ),
                    rollback_count=0,
                    attempts_total=0,
                    selected_attempts=[],
                    warnings=issues
                    + [
                        IntegrityIssue(
                            "E_VERSION",
                            "interrupt marker metadata is stale",
                            "interrupt.json",
                        )
                    ],
                    source="fresh_after_marker_metadata_mismatch",
                    restarted=True,
                    snapshots_checked=checked,
                )
            marker_consistent = (
                marker.get("spec_digest") == self.spec.spec_digest
                and marker.get("seed") == self.spec.seed
            )
            if not marker_consistent:
                # The current input is not the run that was interrupted.
                self.archive_active("marker_version_changed")
                return ResumeInfo(
                    start_index=0,
                    state=LevelState.initial(
                        self.spec.width, self.spec.height, self.spec.blocked
                    ),
                    rollback_count=0,
                    attempts_total=0,
                    selected_attempts=[],
                    warnings=issues
                    + [
                        IntegrityIssue(
                            "E_VERSION",
                            "interrupt marker version does not match the input",
                            "interrupt.json",
                        )
                    ],
                    source="fresh_after_marker_version_change",
                    restarted=True,
                    snapshots_checked=checked,
                )
            if marker_latest is not None:
                if (
                    manifest_latest is not None
                    and manifest_latest.version_id != marker_latest.version_id
                ):
                    self.archive_active("marker_version_mismatch")
                    return ResumeInfo(
                        start_index=0,
                        state=LevelState.initial(
                            self.spec.width,
                            self.spec.height,
                            self.spec.blocked,
                        ),
                        rollback_count=0,
                        attempts_total=0,
                        selected_attempts=[],
                        warnings=issues
                        + [
                            IntegrityIssue(
                                "E_VERSION",
                                "interrupt marker and manifest name different versions",
                                "interrupt.json",
                            )
                        ],
                        source="fresh_after_marker_version_mismatch",
                        restarted=True,
                        snapshots_checked=checked,
                    )
                latest = marker_latest
                source = "interrupt_marker"
            elif marker_version is None:
                if valid:
                    # A marker saying "no snapshots" while valid snapshots
                    # exist is a stale version and triggers a clean restart.
                    self.archive_active("stale_marker")
                    return ResumeInfo(
                        start_index=0,
                        state=LevelState.initial(
                            self.spec.width, self.spec.height, self.spec.blocked
                        ),
                        rollback_count=0,
                        attempts_total=0,
                        selected_attempts=[],
                        warnings=issues
                        + [
                            IntegrityIssue(
                                "E_VERSION",
                                "stale marker reports no snapshot",
                                "interrupt.json",
                            )
                        ],
                        source="fresh_after_stale_marker",
                        restarted=True,
                        snapshots_checked=checked,
                    )
                latest = None
                source = "interrupt_marker_empty"
            else:
                named_file = marker.get("latest_snapshot")
                named_path = (
                    self.snapshots_dir / str(named_file)
                    if isinstance(named_file, str)
                    else None
                )
                if named_path is None or not named_path.exists():
                    # A syntactically valid marker that names no persisted
                    # version is stale.  Per the version rule, restart cleanly.
                    self.archive_active("stale_marker_version")
                    return ResumeInfo(
                        start_index=0,
                        state=LevelState.initial(
                            self.spec.width,
                            self.spec.height,
                            self.spec.blocked,
                        ),
                        rollback_count=0,
                        attempts_total=0,
                        selected_attempts=[],
                        warnings=issues
                        + [
                            IntegrityIssue(
                                "E_VERSION",
                                "interrupt marker names a stale version",
                                "interrupt.json",
                            )
                        ],
                        source="fresh_after_stale_marker_version",
                        restarted=True,
                        snapshots_checked=checked,
                    )
                named_snapshot, _named_issue = self.load_snapshot_file(named_path)
                if (
                    named_snapshot is not None
                    and named_snapshot.version_id != marker_version
                ):
                    # The file itself is valid but the marker names a different
                    # (stale) version identifier.
                    self.archive_active("stale_marker_version")
                    return ResumeInfo(
                        start_index=0,
                        state=LevelState.initial(
                            self.spec.width,
                            self.spec.height,
                            self.spec.blocked,
                        ),
                        rollback_count=0,
                        attempts_total=0,
                        selected_attempts=[],
                        warnings=issues
                        + [
                            IntegrityIssue(
                                "E_VERSION",
                                "interrupt marker version disagrees with the snapshot file",
                                "interrupt.json",
                            )
                        ],
                        source="fresh_after_stale_marker_version",
                        restarted=True,
                        snapshots_checked=checked,
                    )
                # The marker names a present snapshot whose digest failed.
                # Treat this as digest damage and fall back safely.
                latest = self._recover_latest(
                    valid, manifest_latest, excluded_versions
                )
                source = "digest_fallback"
        else:
            # With no usable marker this is forced-termination recovery.  Use
            # the highest authenticated applied step, which also recovers a
            # snapshot atomically written just before a crash in manifest
            # update.
            latest = self._recover_latest(
                valid, manifest_latest, excluded_versions
            )
            if latest is None:
                source = "fresh"
            elif marker is None and marker_issue is None:
                source = "latest_snapshot_no_marker"
            elif marker_issue is not None:
                source = "digest_fallback"
            else:
                source = "manifest"

        if latest is None:
            latest = self._recover_latest(
                valid, manifest_latest, excluded_versions
            )
            if latest is not None:
                source = "latest_snapshot_no_marker"

        if latest is not None:
            if (
                marker_checked
                and marker_version is None
                and latest.applied_count > 0
            ):
                self.archive_active("stale_marker")
                return ResumeInfo(
                    start_index=0,
                    state=LevelState.initial(
                        self.spec.width, self.spec.height, self.spec.blocked
                    ),
                    rollback_count=0,
                    attempts_total=0,
                    selected_attempts=[],
                    warnings=issues
                    + [
                        IntegrityIssue(
                            "E_VERSION",
                            "stale marker does not name a snapshot",
                            "interrupt.json",
                        )
                    ],
                    source="fresh_after_stale_marker",
                    restarted=True,
                    snapshots_checked=checked,
                )
            chain, complete = self._build_chain(latest, valid)
            for issue in scan_issues:
                path = self.root / issue.path if issue.path else None
                if path is not None and path.exists():
                    self._archive_corrupt(path)
            # Snapshots above the selected state (or outside its chain) are
            # preserved under archive rather than overwritten.
            active_chain_versions = {snapshot.version_id for snapshot in chain}
            for snapshot in valid.values():
                if snapshot.version_id not in active_chain_versions:
                    self._archive_superseded(snapshot.path)
            self.update_manifest(
                latest,
                status="running",
                rollback_count=latest.rollback_count,
                attempts_total=latest.attempts_total,
            )
            self.clear_interrupt()
            return ResumeInfo(
                start_index=latest.applied_count,
                state=latest.state,
                rollback_count=latest.rollback_count,
                attempts_total=latest.attempts_total,
                selected_attempts=list(latest.selected_attempts),
                chain=chain,
                warnings=issues,
                source=source,
                restarted=source == "digest_fallback",
                snapshots_checked=checked,
                history_complete=complete,
            )

        if issues:
            # No authenticated state remains.  Preserve all evidence and start
            # from the deterministic initial state, as allowed by the resume
            # fallback contract.
            self.archive_active("all_snapshots_invalid")
            return ResumeInfo(
                start_index=0,
                state=LevelState.initial(
                    self.spec.width, self.spec.height, self.spec.blocked
                ),
                rollback_count=0,
                attempts_total=0,
                selected_attempts=[],
                warnings=issues,
                source="fresh_after_digest_failure",
                restarted=True,
                snapshots_checked=checked,
            )
        self._write_manifest(
            status="running",
            latest=None,
            rollback_count=0,
            attempts_total=0,
        )
        return ResumeInfo(
            start_index=0,
            state=LevelState.initial(self.spec.width, self.spec.height, self.spec.blocked),
            rollback_count=0,
            attempts_total=0,
            selected_attempts=[],
            warnings=issues,
            source=source or "fresh",
            restarted=True,
            snapshots_checked=checked,
        )

    def verify(self) -> dict[str, Any]:
        issues: list[IntegrityIssue] = []
        manifest, manifest_issue = self.read_manifest()
        if manifest_issue is not None:
            issues.append(manifest_issue)
        marker, marker_issue = self.read_marker()
        if marker_issue is not None:
            issues.append(marker_issue)
        valid, scan_issues, checked = self._scan_snapshots()
        issues.extend(scan_issues)
        if not checked:
            issues.append(
                IntegrityIssue("E_SNAPSHOT", "no snapshots are present")
            )
        latest_reference: LoadedSnapshot | None = None
        if manifest is not None:
            latest_version = manifest.get("latest_version_id")
            if latest_version is not None and latest_version not in valid:
                issues.append(
                    IntegrityIssue(
                        "E_DIGEST",
                        "manifest latest snapshot is invalid or missing",
                        str(manifest.get("latest_snapshot")),
                    )
                )
            elif latest_version is not None:
                latest_reference = valid[latest_version]
                if (
                    manifest.get("latest_snapshot_digest")
                    != latest_reference.digest
                    or manifest.get("applied_count")
                    != latest_reference.applied_count
                    or manifest.get("commit_sequence")
                    != latest_reference.commit_sequence
                ):
                    issues.append(
                        IntegrityIssue(
                            "E_DIGEST",
                            "manifest metadata disagrees with its latest snapshot",
                            "manifest.json",
                        )
                    )
        if marker is not None:
            marker_version = marker.get("latest_version_id")
            if marker_version is not None and marker_version not in valid:
                issues.append(
                    IntegrityIssue(
                        "E_DIGEST",
                        "interrupt marker references a missing or invalid snapshot",
                        "interrupt.json",
                    )
                )
            elif marker_version is not None:
                marker_snapshot = valid[marker_version]
                if (
                    marker.get("latest_snapshot_digest")
                    != marker_snapshot.digest
                    or marker.get("applied_count")
                    != marker_snapshot.applied_count
                    or marker.get("commit_sequence")
                    != marker_snapshot.commit_sequence
                ):
                    issues.append(
                        IntegrityIssue(
                            "E_DIGEST",
                            "interrupt marker metadata disagrees with its snapshot",
                            "interrupt.json",
                        )
                    )
        if latest_reference is None:
            latest_reference = self._choose_latest(valid)
        if latest_reference is not None:
            _chain, complete = self._build_chain(latest_reference, valid)
            if not complete:
                issues.append(
                    IntegrityIssue(
                        "E_DIGEST",
                        "active snapshot ancestry is incomplete",
                        "snapshots",
                    )
                )
        if issues:
            raise SolverError(
                "E_DIGEST",
                "snapshot verification failed",
                {
                    "issues": [issue.as_dict() for issue in issues],
                    "snapshots_checked": checked,
                },
            )
        return {
            "ok": True,
            "snapshots_checked": checked,
            "valid_versions": sorted(valid),
            "manifest": manifest,
        }


def verify_snapshot_payload(
    spec: LevelSpec, payload: Any, path: str | Path = "<memory>"
) -> dict[str, Any]:
    """Standalone snapshot body verifier used by tests and the CLI."""

    store = SnapshotStore(Path(path).parent if str(path) != "<memory>" else ".", spec)
    loaded, issue = store._validate_snapshot(Path(path), payload)
    if loaded is None:
        raise SolverError(
            "E_DIGEST",
            issue.message if issue else "snapshot is invalid",
            {"issue": issue.as_dict()} if issue else {},
        )
    return {
        "ok": True,
        "version_id": loaded.version_id,
        "applied_count": loaded.applied_count,
    }
