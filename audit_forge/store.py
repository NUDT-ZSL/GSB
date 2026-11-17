from __future__ import annotations

import errno
import json
import os
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine import AuditEngine
from .rules import (
    RuleConfig,
    RuleConfigError,
    default_rule_config,
    parse_rule_document,
)
from .util import sha256_value

CHECKPOINT_FORMAT = "audit-forge-checkpoint"
CASE_SNAPSHOT_FORMAT = "audit-forge-cases"
SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        value = {"code": self.code, "message": self.message}
        value.update(self.details)
        return value


class LockBusy(StoreError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_LOCK", message, **details)


def replace_file(source: str | Path, destination: str | Path) -> None:
    """Indirection used by crash-injection tests and platform wrappers."""
    os.replace(source, destination)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


class WorkspaceLock(AbstractContextManager["WorkspaceLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file_descriptor: int | None = None

    def acquire(self) -> "WorkspaceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in {
                        errno.EACCES,
                        errno.EAGAIN,
                        getattr(errno, "EDEADLK", -1),
                        getattr(errno, "EDEADLOCK", -1),
                    }:
                        raise LockBusy(
                            "workspace lock is held by another process",
                            path=str(self.path),
                        ) from exc
                    raise
            else:
                import fcntl

                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError as exc:
                    raise LockBusy(
                        "workspace lock is held by another process",
                        path=str(self.path),
                    ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        self.file_descriptor = descriptor
        return self

    def release(self) -> None:
        if self.file_descriptor is None:
            return
        descriptor = self.file_descriptor
        self.file_descriptor = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(descriptor)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


@dataclass
class LoadResult:
    engine: AuditEngine
    checkpoint_exists: bool
    checkpoint: dict[str, Any] | None
    issues: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    cases_source: str = "none"
    requires_rebuild: bool = False

    @property
    def rules(self) -> RuleConfig:
        return self.engine.rules


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.checkpoint_path = self.root / "checkpoint.json"
        self.cases_path = self.root / "cases.json"
        self.lock_path = self.root / ".audit-forge.lock"

    def initialize(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError(
                "E_WORKSPACE",
                "cannot create workspace",
                path=str(self.root),
                detail=str(exc),
            ) from exc

    def lock(self) -> WorkspaceLock:
        self.initialize()
        return WorkspaceLock(self.lock_path).acquire()

    def stale_temp_files(self) -> list[str]:
        if not self.root.exists():
            return []
        names = []
        for name in (self.checkpoint_path.name, self.cases_path.name):
            for path in self.root.glob(f".{name}.*.tmp"):
                names.append(path.name)
        return sorted(names)

    def cleanup_stale_temp_files(self) -> list[str]:
        removed = []
        for path in self.root.glob(".*.tmp"):
            if path.name.startswith(
                (f".{self.checkpoint_path.name}.", f".{self.cases_path.name}.")
            ):
                try:
                    path.unlink()
                    removed.append(path.name)
                except OSError:
                    pass
        return sorted(removed)

    def _read_json_object(self, path: Path, code: str) -> dict[str, Any]:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise StoreError(
                code,
                "cannot read state file",
                path=str(path),
                detail=str(exc),
            ) from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError(
                code,
                "state file is not complete valid JSON",
                path=str(path),
                detail=str(exc),
            ) from exc
        if not isinstance(value, dict):
            raise StoreError(
                code,
                "state file root must be an object",
                path=str(path),
            )
        return value

    @staticmethod
    def _verify_checksum(document: dict[str, Any], code: str) -> None:
        expected = document.get("checksum")
        payload = dict(document)
        payload.pop("checksum", None)
        actual = sha256_value(payload)
        if not isinstance(expected, str) or expected != actual:
            raise StoreError(
                code,
                "state checksum mismatch; file is not a valid complete state",
                expected=expected,
                actual=actual,
            )

    def read_checkpoint(self) -> dict[str, Any] | None:
        try:
            raw = self._read_json_object(
                self.checkpoint_path,
                "E_CHECKPOINT_CORRUPT",
            )
        except FileNotFoundError:
            return None
        if raw.get("format") != CHECKPOINT_FORMAT:
            raise StoreError(
                "E_CHECKPOINT_CORRUPT",
                "unexpected checkpoint format",
                path=str(self.checkpoint_path),
            )
        self._verify_checksum(raw, "E_CHECKPOINT_CORRUPT")
        return raw

    def _read_case_snapshot(self) -> dict[str, Any] | None:
        try:
            snapshot = self._read_json_object(
                self.cases_path,
                "E_CASE_SNAPSHOT_CORRUPT",
            )
        except FileNotFoundError:
            return None
        return snapshot

    @staticmethod
    def _validate_case_snapshot(
        snapshot: dict[str, Any] | None,
        reference: Any,
        rules: RuleConfig,
        generation: int,
    ) -> tuple[bool, list[str]]:
        failures: list[str] = []
        if not isinstance(reference, dict):
            return False, ["missing_reference"]
        if snapshot is None:
            return False, ["missing_file"]
        if snapshot.get("format") != CASE_SNAPSHOT_FORMAT:
            failures.append("format")
        if snapshot.get("schema_version") != SCHEMA_VERSION:
            failures.append("schema_version")
        if snapshot.get("generation") != reference.get("generation"):
            failures.append("generation")
        if snapshot.get("generation") != generation:
            failures.append("checkpoint_generation")
        if snapshot.get("rule_version") != rules.version:
            failures.append("rule_version")
        if snapshot.get("rule_fingerprint") != rules.fingerprint:
            failures.append("rule_fingerprint")
        if not isinstance(snapshot.get("cases"), list):
            failures.append("cases")
        payload = dict(snapshot)
        expected_snapshot_checksum = payload.pop("checksum", None)
        if expected_snapshot_checksum != sha256_value(payload):
            failures.append("checksum")
        if reference.get("snapshot_checksum") != expected_snapshot_checksum:
            failures.append("reference_checksum")
        if reference.get("cases_checksum") != sha256_value(
            snapshot.get("cases", [])
        ):
            failures.append("cases_checksum")
        return not failures, failures

    def load(
        self,
        requested_rules: RuleConfig | None = None,
        *,
        inspect_snapshot: bool = True,
    ) -> LoadResult:
        checkpoint = self.read_checkpoint()
        if checkpoint is None:
            rules = requested_rules or default_rule_config()
            engine = AuditEngine(rules)
            return LoadResult(
                engine=engine,
                checkpoint_exists=False,
                checkpoint=None,
                issues=[
                    {
                        "code": "E_NO_CHECKPOINT",
                        "severity": "warning",
                        "message": "workspace has no persisted checkpoint",
                    }
                ],
                checks={
                    "checkpoint": {"exists": False},
                    "cases_snapshot": {"status": "absent"},
                    "temp_files": self.stale_temp_files(),
                },
                cases_source="none",
            )

        issues: list[dict[str, Any]] = []
        requires_rebuild = False
        schema_version = checkpoint.get("schema_version")
        generation = checkpoint.get("generation", 0)
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            raise StoreError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint generation must be a non-negative integer",
            )

        stored_rules: RuleConfig | None = None
        try:
            stored_rules = parse_rule_document(checkpoint.get("rules"))
        except RuleConfigError as exc:
            issues.append(
                {
                    "code": "E_CHECKPOINT_RULES",
                    "severity": "error",
                    "message": "checkpoint contains an invalid rule document",
                    "detail": str(exc),
                }
            )

        rules = requested_rules or stored_rules or default_rule_config()
        if stored_rules is not None:
            if (
                checkpoint.get("rule_version") != stored_rules.version
                or checkpoint.get("rule_fingerprint")
                != stored_rules.fingerprint
            ):
                requires_rebuild = True
                issues.append(
                    {
                        "code": "E_CHECKPOINT_RULES",
                        "severity": "error",
                        "message": "checkpoint rule metadata does not match the embedded rule document",
                        "stored_rule_version": checkpoint.get("rule_version"),
                        "embedded_rule_version": stored_rules.version,
                        "stored_rule_fingerprint": checkpoint.get(
                            "rule_fingerprint"
                        ),
                        "embedded_rule_fingerprint": stored_rules.fingerprint,
                    }
                )
            if (
                stored_rules.version != rules.version
                or stored_rules.fingerprint != rules.fingerprint
            ):
                requires_rebuild = True
                issues.append(
                    {
                        "code": "E_RULE_VERSION_CHANGED",
                        "severity": "warning",
                        "message": "rule version/content changed; derived cases will be rebuilt",
                        "stored_version": stored_rules.version,
                        "active_version": rules.version,
                        "stored_fingerprint": stored_rules.fingerprint,
                        "active_fingerprint": rules.fingerprint,
                    }
                )

        if schema_version != SCHEMA_VERSION:
            requires_rebuild = True
            issues.append(
                {
                    "code": "E_SCHEMA_VERSION_CHANGED",
                    "severity": "warning",
                    "message": "checkpoint structure version changed; state will be rebuilt from checkpoint events",
                    "stored_schema_version": schema_version,
                    "active_schema_version": SCHEMA_VERSION,
                }
            )

        if not isinstance(checkpoint.get("events"), list):
            raise StoreError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint events must be a list",
            )

        core_fields = {
            "events",
            "event_origins",
            "rejects",
            "ingest_anomalies",
            "watermarks",
            "stats",
        }
        core = {key: checkpoint.get(key) for key in core_fields}
        if core["events"] is None:
            core["events"] = []
        engine = AuditEngine.from_checkpoint_core(
            core,
            rules,
            generation,
        )
        issues.extend(engine.load_issues)

        snapshot_status = "not_checked"
        snapshot_failures: list[str] = []
        if requires_rebuild:
            engine.recompute_cases()
            cases_source = "rebuilt"
            snapshot_status = "ignored_version_change"
        elif not inspect_snapshot:
            engine.recompute_cases()
            cases_source = "rebuilt"
            snapshot_status = "skipped"
        else:
            try:
                snapshot = self._read_case_snapshot()
                valid, snapshot_failures = self._validate_case_snapshot(
                    snapshot,
                    checkpoint.get("cases_ref"),
                    rules,
                    generation,
                )
            except StoreError as exc:
                snapshot = None
                valid = False
                snapshot_failures = ["parse_error"]
                issues.append(
                    {
                        "code": "E_CASE_SNAPSHOT_CORRUPT",
                        "severity": "warning",
                        "message": "case snapshot is not complete valid JSON; it will be rebuilt",
                        "detail": exc.message,
                    }
                )
            if valid and snapshot is not None:
                engine.cases = snapshot["cases"]
                engine.cases_snapshot_valid = True
                cases_source = "snapshot"
                snapshot_status = "valid"
            else:
                engine.recompute_cases()
                cases_source = "rebuilt"
                snapshot_status = "stale"
                issues.append(
                    {
                        "code": "E_CASE_SNAPSHOT_STALE",
                        "severity": "warning",
                        "message": "case snapshot does not match checkpoint metadata; rebuilt from event state",
                        "failures": snapshot_failures,
                    }
                )

        checks = {
            "checkpoint": {
                "exists": True,
                "format": checkpoint.get("format"),
                "schema_version": schema_version,
                "generation": generation,
                "checksum_valid": True,
            },
            "cases_snapshot": {
                "status": snapshot_status,
                "failures": snapshot_failures,
            },
            "temp_files": self.stale_temp_files(),
        }
        return LoadResult(
            engine=engine,
            checkpoint_exists=True,
            checkpoint=checkpoint,
            issues=issues,
            checks=checks,
            cases_source=cases_source,
            requires_rebuild=requires_rebuild,
        )

    def save(self, engine: AuditEngine, rules: RuleConfig | None = None) -> int:
        self.initialize()
        self.cleanup_stale_temp_files()
        active_rules = rules or engine.rules
        engine.rules = active_rules
        generation = engine.generation + 1
        cases = engine.recompute_cases()
        snapshot: dict[str, Any] = {
            "format": CASE_SNAPSHOT_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "rule_version": active_rules.version,
            "rule_fingerprint": active_rules.fingerprint,
            "cases": cases,
        }
        snapshot["checksum"] = sha256_value(snapshot)
        atomic_write_json(self.cases_path, snapshot)

        core = engine.core_state()
        checkpoint: dict[str, Any] = {
            "format": CHECKPOINT_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "rule_version": active_rules.version,
            "rule_fingerprint": active_rules.fingerprint,
            "rules": active_rules.as_dict(),
            **core,
            "cases_ref": {
                "file": self.cases_path.name,
                "generation": generation,
                "snapshot_checksum": snapshot["checksum"],
                "cases_checksum": sha256_value(cases),
            },
        }
        checkpoint["checksum"] = sha256_value(checkpoint)
        atomic_write_json(self.checkpoint_path, checkpoint)
        engine.generation = generation
        engine.cases = cases
        engine.cases_snapshot_valid = True
        return generation

    def probe_write(self) -> bool:
        probe = self.root / f".write-probe-{os.getpid()}"
        renamed = self.root / f".write-probe-{os.getpid()}-renamed"
        try:
            with probe.open("wb") as stream:
                stream.write(b"probe\n")
                stream.flush()
                os.fsync(stream.fileno())
            replace_file(probe, renamed)
            renamed.unlink()
            return True
        except OSError:
            for path in (probe, renamed):
                try:
                    path.unlink()
                except OSError:
                    pass
            return False
