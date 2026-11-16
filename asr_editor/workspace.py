"""Workspace orchestration: load, recover, edit and commit snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codes import E_IO, E_SNAPSHOT_INVALID
from .formats import (
    build_sidecar,
    read_sidecar,
    sidecar_file_hash,
    transcript_fingerprint,
    v2_content_hash,
    write_outputs,
    write_sidecar,
    write_v1,
    write_v2,
)
from .interop import upgrade_v2
from .models import Transcript
from .operations import apply_operation_with_precision
from .parsers import FailureRecord, ParseResult, parse_v1_file, parse_v2_file
from .snapshots import Snapshot, SnapshotError, SnapshotStore
from .timeutil import PrecisionStats


@dataclass(slots=True)
class WorkspacePaths:
    v1: Path
    v2: Path
    sidecar: Path
    state_dir: Path


@dataclass(slots=True)
class RecoveryRecord:
    restored: bool
    reasons: list[str] = field(default_factory=list)
    previous_workspace_version: int | None = None
    snapshot_version: int | None = None
    resume_after_operation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored": self.restored,
            "reasons": self.reasons,
            "previous_workspace_version": self.previous_workspace_version,
            "snapshot_version": self.snapshot_version,
            "resume_after_operation": self.resume_after_operation,
        }


def default_sidecar_path(v1_path: str | Path) -> Path:
    source = Path(v1_path)
    name = source.name
    if name.lower().endswith(".v1.json"):
        base = name[: -len(".v1.json")]
    elif name.lower().endswith(".json"):
        base = name[: -len(".json")]
    else:
        base = source.stem
    return source.with_name(f"{base}.tokens.meta.json")


def default_state_dir(v1_path: str | Path) -> Path:
    source = Path(v1_path)
    name = source.name
    if name.lower().endswith(".v1.json"):
        base = name[: -len(".v1.json")]
    elif name.lower().endswith(".json"):
        base = name[: -len(".json")]
    else:
        base = source.stem
    return source.parent / ".asr_state" / base


def resolve_paths(
    v1_path: str | Path,
    v2_path: str | Path,
    sidecar_path: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> WorkspacePaths:
    v1 = Path(v1_path)
    v2 = Path(v2_path)
    sidecar = (
        Path(sidecar_path) if sidecar_path is not None else default_sidecar_path(v1)
    )
    state = Path(state_dir) if state_dir is not None else default_state_dir(v1)
    return WorkspacePaths(v1=v1, v2=v2, sidecar=sidecar, state_dir=state)


class Workspace:
    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths
        self.store = SnapshotStore(paths.state_dir)
        self.transcript = Transcript(source_version=1, utterances=[])
        self.version = 0
        self.precision = PrecisionStats()
        self.operation_log: list[dict[str, Any]] = []
        self.sidecar: dict[str, Any] = {}
        self.failures: list[FailureRecord] = []
        self.diagnostics: list[str] = []
        self.plan_digest: str | None = None
        self.resume_after_operation = 0
        self.recovery = RecoveryRecord(restored=False)
        self.current_v1_result: ParseResult | None = None
        self.current_v2_result: ParseResult | None = None

    @classmethod
    def open(
        cls,
        v1_path: str | Path,
        v2_path: str | Path,
        sidecar_path: str | Path | None = None,
        state_dir: str | Path | None = None,
        recover: bool = True,
        create_initial_snapshot: bool = False,
    ) -> Workspace:
        workspace = cls(resolve_paths(v1_path, v2_path, sidecar_path, state_dir))
        workspace._load_current_files()
        snapshot: Snapshot | None = None
        if recover:
            try:
                snapshot = workspace.store.load_latest()
            except SnapshotError as exc:
                workspace.failures.append(
                    FailureRecord(
                        stage="recover",
                        source_file=str(workspace.paths.state_dir),
                        item_index=None,
                        code=E_SNAPSHOT_INVALID,
                        exception_type=type(exc).__name__,
                        message=str(exc),
                        line_number=None,
                    )
                )
        if snapshot is not None:
            workspace._apply_or_restore_snapshot(snapshot)
        elif create_initial_snapshot:
            workspace.initialize()
        return workspace

    def _load_current_files(self) -> None:
        results: list[ParseResult] = []
        v1_result: ParseResult | None = None
        v2_result: ParseResult | None = None
        if self.paths.v1.exists():
            v1_result = parse_v1_file(self.paths.v1)
            self.current_v1_result = v1_result
            results.append(v1_result)
        else:
            self.failures.append(
                FailureRecord(
                    stage="parse",
                    source_file=str(self.paths.v1),
                    item_index=None,
                    code=E_IO,
                    exception_type="FileNotFoundError",
                    message="v1 transcript file does not exist",
                    line_number=None,
                )
            )
        if self.paths.v2.exists():
            v2_result = parse_v2_file(self.paths.v2)
            self.current_v2_result = v2_result
            results.append(v2_result)

        for result in results:
            self.failures.extend(result.failures)
            self.precision.merge(result.precision)

        if v1_result is not None and v1_result.transcript.utterances:
            self.transcript = v1_result.transcript
        elif v2_result is not None and v2_result.transcript.utterances:
            upgraded = upgrade_v2(
                v2_result.transcript, self.paths.sidecar, self.precision
            )
            self.transcript = upgraded.transcript
            self.failures.extend(upgraded.failures)
            self.diagnostics.extend(upgraded.diagnostic_codes)
        else:
            self.transcript = Transcript(
                source_version=1, utterances=[], timing_origin="v1"
            )
        self.sidecar = build_sidecar(self.transcript)

    def _apply_or_restore_snapshot(self, snapshot: Snapshot) -> None:
        state: dict[str, Any] | None = None
        progress: dict[str, Any] | None = None
        try:
            state = self.store.read_state()
        except SnapshotError as exc:
            self.failures.append(
                FailureRecord(
                    stage="recover",
                    source_file=str(self.store.state_path),
                    item_index=None,
                    code=E_SNAPSHOT_INVALID,
                    exception_type=type(exc).__name__,
                    message=str(exc),
                    line_number=None,
                )
            )
        try:
            progress = self.store.read_progress()
        except SnapshotError as exc:
            self.failures.append(
                FailureRecord(
                    stage="recover",
                    source_file=str(self.store.progress_path),
                    item_index=None,
                    code=E_SNAPSHOT_INVALID,
                    exception_type=type(exc).__name__,
                    message=str(exc),
                    line_number=None,
                )
            )

        reasons: list[str] = []
        state_version = int(state.get("workspace_version", -1)) if state else None
        if state is None:
            reasons.append("workspace state is missing")
        elif state_version is not None and state_version != snapshot.version:
            if state_version > snapshot.version:
                reasons.append(
                    "workspace version is newer than latest complete snapshot"
                )
            else:
                reasons.append("snapshot version is newer than workspace state")
        if state is not None and state.get("snapshot_version") != snapshot.version:
            reasons.append("state snapshot version does not match latest snapshot")
        if state is not None and state.get("ir_hash") != snapshot.ir_hash:
            reasons.append("state IR hash does not match latest snapshot")

        current_hash = transcript_fingerprint(self.transcript)
        if current_hash != snapshot.ir_hash:
            reasons.append(
                "current transcript content fingerprint does not match latest snapshot"
            )
        current_v2_hash = (
            v2_content_hash(self.current_v2_result.transcript)
            if self.current_v2_result is not None
            else None
        )
        if current_v2_hash != snapshot.sidecar.get("v2_content_hash"):
            reasons.append(
                "current v2 sentence content does not match latest snapshot sidecar"
            )
        try:
            if not self.paths.sidecar.exists():
                reasons.append("sidecar metadata file is missing")
            else:
                actual_sidecar = read_sidecar(self.paths.sidecar)
                if sidecar_file_hash(actual_sidecar) != snapshot.sidecar_hash:
                    reasons.append(
                        "sidecar metadata hash does not match latest snapshot"
                    )
        except (OSError, ValueError, TypeError):
            reasons.append("sidecar metadata is unreadable or malformed")

        if reasons:
            self._restore_snapshot(snapshot)
            self.recovery = RecoveryRecord(
                restored=True,
                reasons=reasons,
                previous_workspace_version=state_version,
                snapshot_version=snapshot.version,
                resume_after_operation=snapshot.last_completed_operation,
            )
        else:
            self._adopt_snapshot(snapshot)
            self.recovery = RecoveryRecord(
                restored=False,
                reasons=[],
                previous_workspace_version=state_version,
                snapshot_version=snapshot.version,
                resume_after_operation=snapshot.last_completed_operation,
            )

        if (
            progress is not None
            and progress.get("snapshot_version") == snapshot.version
        ):
            if progress.get("ir_hash") == snapshot.ir_hash:
                try:
                    completed = int(
                        progress.get(
                            "last_completed_operation",
                            snapshot.last_completed_operation,
                        )
                    )
                except (TypeError, ValueError):
                    completed = snapshot.last_completed_operation
                self.resume_after_operation = max(
                    snapshot.last_completed_operation, completed
                )
                self.plan_digest = progress.get("operation_plan_digest")
        else:
            self.resume_after_operation = snapshot.last_completed_operation
            self.plan_digest = snapshot.plan_digest

    def _adopt_snapshot(self, snapshot: Snapshot) -> None:
        self.transcript = snapshot.transcript.normalized_copy()
        self.version = snapshot.version
        self.precision = PrecisionStats.from_dict(snapshot.precision.to_dict())
        self.operation_log = list(snapshot.operation_log)
        self.sidecar = dict(snapshot.sidecar)
        self.plan_digest = snapshot.plan_digest
        self.resume_after_operation = snapshot.last_completed_operation

    def _restore_snapshot(self, snapshot: Snapshot) -> None:
        self._adopt_snapshot(snapshot)
        write_v1(self.paths.v1, self.transcript)
        write_v2(self.paths.v2, self.transcript)
        write_sidecar(self.paths.sidecar, self.sidecar)
        self.store.write_state(snapshot, plan_digest=snapshot.plan_digest)
        self.store.write_progress(snapshot, plan_digest=snapshot.plan_digest)

    def initialize(self, plan_digest: str | None = None) -> Snapshot:
        self.plan_digest = plan_digest
        sidecar = build_sidecar(self.transcript)
        snapshot = Snapshot(
            version=self.version,
            transcript=self.transcript.normalized_copy(),
            sidecar=sidecar,
            precision=PrecisionStats.from_dict(self.precision.to_dict()),
            operation_log=[],
            plan_digest=plan_digest,
            last_completed_operation=0,
        ).finalize()
        self.store.write_snapshot(snapshot)
        write_outputs(self.transcript, self.paths.v1, self.paths.v2, self.paths.sidecar)
        self.store.write_state(
            snapshot, plan_digest=plan_digest, last_completed_operation=0
        )
        self.store.write_progress(
            snapshot, plan_digest=plan_digest, last_completed_operation=0
        )
        self.sidecar = sidecar
        self.resume_after_operation = 0
        return snapshot

    def apply_operation(
        self,
        operation: dict[str, Any],
        operation_index: int,
        plan_digest: str | None = None,
    ) -> dict[str, Any]:
        next_precision = PrecisionStats.from_dict(self.precision.to_dict())
        result = apply_operation_with_precision(
            self.transcript, operation, next_precision
        )
        next_version = self.version + 1
        sidecar = build_sidecar(result.transcript)
        log_entry = {
            "seq": len(self.operation_log) + 1,
            "operation_index": operation_index,
            "op": operation.get("op", operation.get("type")),
            "args": operation,
            "summary": result.summary,
            "details": result.details,
            "snapshot_version": next_version,
        }
        next_log = self.operation_log + [log_entry]
        snapshot = Snapshot(
            version=next_version,
            transcript=result.transcript,
            sidecar=sidecar,
            precision=next_precision,
            operation_log=next_log,
            plan_digest=plan_digest if plan_digest is not None else self.plan_digest,
            last_completed_operation=operation_index,
        ).finalize()

        # The fully self-contained snapshot is committed first.  If the process
        # dies while output files are being replaced, startup can reconstruct
        # every output from this snapshot.
        self.store.write_snapshot(snapshot)
        try:
            write_v1(self.paths.v1, result.transcript)
            write_v2(self.paths.v2, result.transcript)
            write_sidecar(self.paths.sidecar, sidecar)
            self.store.write_state(snapshot, plan_digest=snapshot.plan_digest)
            self.store.write_progress(snapshot, plan_digest=snapshot.plan_digest)
        except Exception as exc:
            self.failures.append(
                FailureRecord(
                    stage="edit",
                    source_file=str(self.paths.v1),
                    item_index=operation_index,
                    code=E_IO,
                    exception_type=type(exc).__name__,
                    message=f"snapshot committed but output/state write failed: {exc}",
                    line_number=None,
                )
            )
            raise

        self.transcript = result.transcript
        self.version = next_version
        self.precision = next_precision
        self.operation_log = next_log
        self.sidecar = sidecar
        self.plan_digest = snapshot.plan_digest
        self.resume_after_operation = operation_index
        return {
            "version": self.version,
            "summary": result.summary,
            "details": result.details,
            "snapshot_version": snapshot.version,
            "ir_hash": snapshot.ir_hash,
            "sidecar_hash": snapshot.sidecar_hash,
            "last_completed_operation": operation_index,
        }

    def explicit_recover(self) -> RecoveryRecord:
        snapshot = self.store.load_latest()
        if snapshot is None:
            self.recovery = RecoveryRecord(
                restored=False, reasons=["no complete snapshot exists"]
            )
            return self.recovery
        self._restore_snapshot(snapshot)
        self.recovery = RecoveryRecord(
            restored=True,
            reasons=["explicit recovery request"],
            previous_workspace_version=self.version,
            snapshot_version=snapshot.version,
            resume_after_operation=snapshot.last_completed_operation,
        )
        return self.recovery

    def failure_dicts(self) -> list[dict[str, Any]]:
        return [failure.to_dict() for failure in self.failures]

    def add_failure(
        self,
        stage: str,
        source_file: str,
        item_index: int | None,
        code: str,
        exception_type: str,
        message: str,
        line_number: int | None = None,
    ) -> None:
        self.failures.append(
            FailureRecord(
                stage=stage,
                source_file=source_file,
                item_index=item_index,
                code=code,
                exception_type=exception_type,
                message=message,
                line_number=line_number,
            )
        )
