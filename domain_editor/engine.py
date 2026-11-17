"""Execution engine for loading, editing, snapshots, diagnostics, and replay."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import SerializationResult
from .bundle import Bundle
from .config import (
    ensure_role_table,
    load_injections,
    load_rule_config,
    max_workers as config_workers,
)
from .degradation import recompute_losses
from .diagnostics import build_diagnostic_report, report_bytes
from .documents import load_documents
from .edits import EDIT_TYPES, apply_edit, validate_documents
from .errors import E_EDIT_INVALID, E_SNAPSHOT_STALE, EditInvalidError, SnapshotInvalidError
from .jsonutil import atomic_write_json, atomic_write_text, dumps_canonical, sha256_text, stable_hash
from .model import EditLog, Failure, ProjectState
from .snapshots import SnapshotStore
from .v1 import parse_v1, serialize_v1
from .v2 import parse_v2, serialize_v2


@dataclass
class RunResult:
    state: ProjectState | None
    report: dict[str, Any] | None
    exit_code: int
    snapshot_hit: bool
    stopped_by_signal: bool
    processed_commands: int
    snapshot: dict[str, Any] | None


class Engine:
    def __init__(
        self,
        bundle: Bundle,
        workspace: str | Path,
        *,
        max_workers: int | None = None,
        injection_config: dict[str, Any] | None = None,
    ):
        self.bundle = bundle
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config = load_rule_config(bundle.rules_path)
        self.max_workers = config_workers(self.config, max_workers)
        self.injection_config = injection_config or load_injections(bundle.injections_path)
        self.store = SnapshotStore(self.workspace)
        self.state: ProjectState | None = None
        self.stop_requested = False
        self.stop_signal: int | None = None
        self.snapshot_hit = False
        self.last_snapshot: dict[str, Any] | None = None
        self.pending_warnings: list[dict[str, Any]] = []
        self.processed_commands = 0
        self.one_command = False
        self.abrupt_after: int | None = None
        self.graceful_after: int | None = None
        self.output_path_v1 = self.workspace / "project.v1.txt"
        self.output_path_v2 = self.workspace / "project.v2.json"
        self.sidecar_path = self.workspace / "project.v2.v1-origin.json"
        self.report_path = self.workspace / "report.json"
        self.final_state_path = self.workspace / "final_state.json"

    # ------------------------------------------------------------------ setup
    def reset_workspace(self) -> None:
        self.store.reset()
        for path in (
            self.output_path_v1,
            self.output_path_v2,
            self.sidecar_path,
            self.report_path,
            self.final_state_path,
            self.workspace / "losses.json",
            self.workspace / "failures.json",
            self.workspace / "edit_log.jsonl",
            self.workspace / "progress.json",
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.state = None
        self.snapshot_hit = False

    def request_stop(self, signum: int | None = None) -> None:
        self.stop_requested = True
        self.stop_signal = signum

    def _stale_warning(self, reason: str, message: str) -> dict[str, Any]:
        return {
            "business_code": E_SNAPSHOT_STALE,
            "stage": "snapshot",
            "reason": reason,
            "message": message,
        }

    def prepare(self, source_version: str, *, resume: bool) -> None:
        """Restore a matching snapshot or parse the selected source version."""

        fingerprint = self.bundle.fingerprint(source_version)
        if resume:
            try:
                loaded = self.store.load()
            except SnapshotInvalidError as exc:
                loaded = None
                stale = self._stale_warning("integrity", str(exc))
            else:
                stale = None
            if loaded is not None:
                snapshot, pointer = loaded
                reasons: list[str] = []
                if snapshot.get("source_version") != source_version:
                    reasons.append(
                        f"source version changed: {snapshot.get('source_version')!r} -> {source_version!r}"
                    )
                if snapshot.get("source_fingerprint") != fingerprint:
                    reasons.append("source file fingerprint changed")
                if reasons:
                    stale = self._stale_warning("source_changed", "; ".join(reasons))
                else:
                    self.state = ProjectState.from_dict(snapshot["state"])
                    self.snapshot_hit = True
                    self.last_snapshot = pointer
                    self._rewrite_artifact(snapshot.get("artifact", {}))
                    return
            self.state = self._parse_state(source_version, fingerprint)
            if stale is not None:
                self.pending_warnings.append(stale)
                self.state.warnings.append(stale)
                self.state.canonicalize()
            self.snapshot_hit = False
            return
        self.state = self._parse_state(source_version, fingerprint)
        self.snapshot_hit = False

    # ----------------------------------------------------------------- parsing
    def _parse_state(self, source_version: str, fingerprint: str) -> ProjectState:
        documents, document_failures = load_documents(self.bundle.documents_path, self.max_workers)
        if source_version == "v1":
            parsed, failures = parse_v1(
                self.bundle.v1_path, documents, self.config, self.max_workers
            )
            role_names: dict[str, str] = {}
            source_metadata: dict[str, Any] = {}
        elif source_version == "v2":
            parsed, role_names, source_metadata, failures = parse_v2(
                self.bundle.v2_path, documents, self.config, self.max_workers
            )
        else:
            raise ValueError(f"unknown source version {source_version!r}")
        state = ProjectState(
            source_version=source_version,
            source_fingerprint=fingerprint,
            target_version=source_version,
            global_version=1,
            documents=parsed,
            rule_config=copy_dict(self.config),
            role_names=role_names,
            source_metadata=source_metadata,
            failures=sorted(document_failures + failures, key=Failure.sort_key),
        )
        if source_version == "v2":
            try:
                validate_documents(state.documents, state.rule_config)
            except EditInvalidError as exc:
                state.failures.append(
                    Failure.from_exception(exc, stage="parse:v2")
                )
        state.canonicalize()
        recompute_losses(state, state.rule_config, self.max_workers)
        return state

    # ------------------------------------------------------------------ output
    def _serialize(self, state: ProjectState) -> SerializationResult:
        if state.target_version == "v1":
            return serialize_v1(state, state.rule_config, self.max_workers)
        return serialize_v2(state, state.rule_config, self.max_workers)

    def _merge_failures(self, state: ProjectState, failures: list[Failure]) -> None:
        existing = {dumps_canonical(failure.to_dict()) for failure in state.failures}
        for failure in failures:
            key = dumps_canonical(failure.to_dict())
            if key not in existing:
                state.failures.append(failure)
                existing.add(key)
        state.failures.sort(key=Failure.sort_key)

    def _artifact_payload(
        self,
        state: ProjectState,
        serialization: SerializationResult,
    ) -> dict[str, Any]:
        if state.target_version == "v1":
            filename = self.output_path_v1.name
            sidecar_filename = None
            sidecar_hash = None
        else:
            filename = self.output_path_v2.name
            sidecar_filename = self.sidecar_path.name if serialization.sidecar is not None else None
            sidecar_hash = (
                sha256_text(dumps_canonical(serialization.sidecar, newline=True))
                if serialization.sidecar is not None
                else None
            )
        return {
            "target_version": state.target_version,
            "filename": filename,
            "sha256": sha256_text(serialization.text),
            "text": serialization.text,
            "sidecar_filename": sidecar_filename,
            "sidecar_sha256": sidecar_hash,
            "sidecar": serialization.sidecar,
        }

    def _write_artifacts(self, state: ProjectState, artifact: dict[str, Any]) -> None:
        if artifact.get("target_version") == "v1":
            atomic_write_text(self.output_path_v1, artifact.get("text", ""))
            try:
                self.output_path_v2.unlink()
            except FileNotFoundError:
                pass
            try:
                self.sidecar_path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_text(self.output_path_v2, artifact.get("text", ""))
            sidecar = artifact.get("sidecar")
            if sidecar is not None:
                atomic_write_json(self.sidecar_path, sidecar)
            else:
                try:
                    self.sidecar_path.unlink()
                except FileNotFoundError:
                    pass
            try:
                self.output_path_v1.unlink()
            except FileNotFoundError:
                pass

    def _rewrite_artifact(self, artifact: dict[str, Any]) -> None:
        if not artifact:
            return
        text = artifact.get("text")
        if not isinstance(text, str):
            return
        if sha256_text(text) != artifact.get("sha256"):
            self.state.warnings.append(
                self._stale_warning("artifact_hash", "snapshot artifact hash mismatch")
            )
            return
        if artifact.get("target_version") == "v1":
            atomic_write_text(self.output_path_v1, text)
        elif artifact.get("target_version") == "v2":
            atomic_write_text(self.output_path_v2, text)
            if artifact.get("sidecar") is not None:
                atomic_write_json(self.sidecar_path, artifact["sidecar"])

    def _snapshot(self, status: str = "running") -> dict[str, Any]:
        if self.state is None:
            raise RuntimeError("cannot snapshot without state")
        serialization = self._serialize(self.state)
        if serialization.losses:
            self.state.losses = sorted(serialization.losses, key=lambda loss: loss.sort_key())
        elif self.state.target_version == "v2":
            self.state.losses = []
        self._merge_failures(self.state, serialization.failures)
        self.state.canonicalize()
        artifact = self._artifact_payload(self.state, serialization)
        pointer = self.store.write(
            self.state,
            artifact=artifact,
            operation_index=self.state.last_operation_index,
            command_id=self.state.last_command_id,
            status=status,
        )
        self.last_snapshot = pointer
        self._write_artifacts(self.state, artifact)
        return artifact

    # --------------------------------------------------------------- processing
    def _add_failure(
        self,
        exc: BaseException,
        command: dict[str, Any] | None,
        stage: str = "edit",
    ) -> None:
        if self.state is None:
            return
        operation_index = int(command["op"]) if command and "op" in command else None
        command_id = str(command["command_id"]) if command and "command_id" in command else None
        doc_id = str(command.get("doc_id", "")) if command else ""
        self.state.failures.append(
            Failure.from_exception(
                exc,
                stage=stage,
                doc_id=doc_id,
                entry_index=-1,
                entry_id=command.get("entry_id") if command else None,
                operation_index=operation_index,
                command_id=command_id,
            )
        )
        self.state.failures.sort(key=Failure.sort_key)

    def _load_command(self, command: dict[str, Any]) -> int:
        source_version = str(command.get("source_version", "v1"))
        if source_version not in ("v1", "v2"):
            raise EditInvalidError(f"invalid source_version {source_version!r}")
        fingerprint = self.bundle.fingerprint(source_version)
        self.state = self._parse_state(source_version, fingerprint)
        for warning in self.pending_warnings:
            if warning not in self.state.warnings:
                self.state.warnings.append(warning)
        self.state.last_operation_index = int(command["op"])
        self.state.last_command_id = str(command["command_id"])
        return 0

    def _switch_version(self, command: dict[str, Any]) -> int:
        if self.state is None:
            raise EditInvalidError("load_project must precede switch_version")
        target = str(command.get("target_version", ""))
        if target not in ("v1", "v2"):
            raise EditInvalidError("switch_version.target_version must be v1 or v2")
        candidate = self.state.clone()
        if target == "v2":
            roles = {
                argument.role
                for doc in candidate.documents.values()
                for event in doc.events
                for argument in event.arguments
            }
            candidate.role_names = ensure_role_table(
                candidate.rule_config, candidate.role_names, roles
            )
        candidate.target_version = target
        validate_documents(candidate.documents, candidate.rule_config, self.max_workers)
        recompute_losses(candidate, candidate.rule_config, self.max_workers)
        candidate.canonicalize()
        self.state = candidate
        return 1

    def _process_non_edit(self, command: dict[str, Any]) -> tuple[int, str, list[dict[str, str]]] | None:
        command_type = str(command.get("type", ""))
        if command_type == "load_project":
            version_increment = self._load_command(command)
            return version_increment, "load_project", []
        if command_type == "switch_version":
            version_increment = self._switch_version(command)
            return version_increment, "switch_version", []
        if command_type == "exit":
            return 0, "exit", []
        return None

    def execute(
        self,
        commands: list[dict[str, Any]],
        *,
        resume_skipping: bool = False,
        one_command: bool = False,
        abrupt_after: int | None = None,
        graceful_after: int | None = None,
    ) -> RunResult:
        self.one_command = one_command
        self.abrupt_after = abrupt_after
        self.graceful_after = graceful_after
        processed_this_run = 0
        exited = False
        for command in commands:
            if self.stop_requested:
                break
            operation_index = int(command["op"])
            if (
                resume_skipping
                and self.state is not None
                and operation_index <= self.state.last_operation_index
            ):
                continue
            if self.state is not None and operation_index <= self.state.last_operation_index:
                self._add_failure(
                    EditInvalidError("operation indices must be strictly increasing"),
                    command,
                    stage="edit",
                )
                self.state.last_operation_index = operation_index
                self.state.last_command_id = str(command.get("command_id"))
                self._snapshot()
                processed_this_run += 1
                continue

            command_type = str(command.get("type", ""))
            if command_type == "exit":
                if self.state is not None:
                    self.state.last_operation_index = operation_index
                    self.state.last_command_id = str(command.get("command_id"))
                    self._snapshot(status="graceful_exit")
                exited = True
                processed_this_run += 1
                break

            try:
                non_edit = self._process_non_edit(command)
            except BaseException as exc:
                self._add_failure(
                    exc,
                    command,
                    stage="edit" if command_type == "switch_version" else "load",
                )
                if self.state is not None:
                    self.state.last_operation_index = operation_index
                    self.state.last_command_id = str(command.get("command_id"))
                    self.state.canonicalize()
                    self._snapshot()
                processed_this_run += 1
                if self.abrupt_after is not None and processed_this_run >= self.abrupt_after:
                    os._exit(137)
                if self.graceful_after is not None and processed_this_run >= self.graceful_after:
                    self.request_stop(None)
                    break
                if self.one_command:
                    break
                continue
            if non_edit is not None:
                version_increment, operation, affected = non_edit
                if self.state is not None:
                    if operation != "load_project":
                        self.state.global_version += version_increment
                    if operation == "switch_version":
                        self.state.logs.append(
                            EditLog(
                                operation_index=operation_index,
                                command_id=str(command["command_id"]),
                                operation=operation,
                                affected=[{"doc_id": "", "kind": "project", "entry_id": self.state.target_version}],
                                target_version=self.state.target_version,
                                global_version=self.state.global_version,
                            )
                        )
                    self.state.last_operation_index = operation_index
                    self.state.last_command_id = str(command["command_id"])
                    self.state.canonicalize()
                    self._snapshot()
                processed_this_run += 1
            elif command_type in EDIT_TYPES:
                if self.state is None:
                    self._add_failure(
                        EditInvalidError("load_project must precede edits"), command, stage="edit"
                    )
                else:
                    try:
                        candidate, affected, detail = apply_edit(
                            self.state, command, self.config, self.max_workers
                        )
                    except BaseException as exc:
                        self._add_failure(exc, command, stage="edit")
                        self.state.last_operation_index = operation_index
                        self.state.last_command_id = str(command["command_id"])
                        self.state.canonicalize()
                        self._snapshot()
                    else:
                        candidate.global_version += 1
                        candidate.last_operation_index = operation_index
                        candidate.last_command_id = str(command["command_id"])
                        candidate.logs.append(
                            EditLog(
                                operation_index=operation_index,
                                command_id=str(command["command_id"]),
                                operation=command_type,
                                affected=affected,
                                target_version=candidate.target_version,
                                global_version=candidate.global_version,
                            )
                        )
                        candidate.canonicalize()
                        self.state = candidate
                        self._snapshot()
                processed_this_run += 1
            else:
                self._add_failure(
                    EditInvalidError(f"unknown command type {command_type!r}"),
                    command,
                    stage="edit",
                )
                if self.state is not None:
                    self.state.last_operation_index = operation_index
                    self.state.last_command_id = str(command.get("command_id"))
                    self.state.canonicalize()
                    self._snapshot()
                processed_this_run += 1

            if self.abrupt_after is not None and processed_this_run >= self.abrupt_after:
                os._exit(137)
            if self.graceful_after is not None and processed_this_run >= self.graceful_after:
                self.request_stop(None)
                break
            if self.one_command:
                break

        self.processed_commands += processed_this_run
        if self.state is not None:
            exit_seen = any(
                str(command.get("type")) == "exit"
                and int(command.get("op", -1)) <= self.state.last_operation_index
                for command in commands
            )
            status = (
                "graceful_exit"
                if (exit_seen or self.stop_requested or not self.one_command)
                else "running"
            )
            self.store.write_progress(
                last_completed_operation=self.state.last_operation_index,
                snapshot_version=self.state.global_version,
                source_version=self.state.source_version,
                target_version=self.state.target_version,
                last_command_id=self.state.last_command_id,
                status=status,
            )
        report = self.finalize()
        if self.stop_requested:
            exit_code = 0
        elif self.state is not None and self.state.failures:
            exit_code = 1
        else:
            exit_code = 0
        return RunResult(
            state=self.state,
            report=report,
            exit_code=exit_code,
            snapshot_hit=self.snapshot_hit,
            stopped_by_signal=self.stop_requested,
            processed_commands=processed_this_run,
            snapshot=self.last_snapshot,
        )

    # ---------------------------------------------------------------- reporting
    def finalize(self) -> dict[str, Any] | None:
        if self.state is None:
            return None
        report = build_diagnostic_report(
            self.state,
            self.config,
            self.max_workers,
            self.injection_config,
        )
        report["run"] = {
            "snapshot_hit": self.snapshot_hit,
            "stopped_by_signal": self.stop_requested,
            "processed_commands": self.processed_commands,
            "last_snapshot": self.last_snapshot,
            "logs": [log.to_dict() for log in self.state.logs],
            "losses": [loss.to_dict() for loss in self.state.losses],
        }
        atomic_write_json(self.report_path, report)
        atomic_write_json(self.final_state_path, self.state.to_dict())
        atomic_write_json(self.workspace / "losses.json", [loss.to_dict() for loss in self.state.losses])
        atomic_write_json(self.workspace / "failures.json", [failure.to_dict() for failure in self.state.failures])
        log_text = "".join(dumps_canonical(log.to_dict(), newline=True) for log in self.state.logs)
        atomic_write_text(self.workspace / "edit_log.jsonl", log_text)
        return report


def copy_dict(value: dict[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(value)
