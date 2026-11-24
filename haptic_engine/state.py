from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import HapticError, canonical_json
from .models import ENGINE_SCHEMA

UTC = timezone.utc

_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _resolve_workspace_path(workspace: Path, configured: str) -> Path:
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate.resolve(strict=False)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_workspace_paths(workspace: Path, state_dir: str, export_dir: str) -> None:
    workspace = workspace.resolve(strict=False)
    state_path = _resolve_workspace_path(workspace, state_dir)
    export_path = _resolve_workspace_path(workspace, export_dir)
    for name, path in (("state_dir", state_path), ("export_dir", export_path)):
        if not _is_within(path, workspace) or path == workspace:
            raise HapticError(
                "E_CONFIG",
                f"{name} must be a writable directory inside the workspace",
                {"path": str(path), "workspace": str(workspace)},
            )
    if (
        state_path == export_path
        or _is_within(state_path, export_path)
        or _is_within(export_path, state_path)
    ):
        raise HapticError(
            "E_CONFIG",
            "state_dir and export_dir must be separate, non-nested directories",
            {"state_dir": str(state_path), "export_dir": str(export_path)},
        )


def _safe_device_component(device_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", device_id).strip(" .")
    if not safe:
        safe = "device"
    if safe.upper() in _RESERVED_WINDOWS_NAMES:
        safe = "device_" + safe
    digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:64]}-{digest}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except (OSError, AttributeError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class StateStore:
    def __init__(
        self, workspace: Path | str, state_dir: str, export_dir: str
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=False)
        self.state_dir = _resolve_workspace_path(self.workspace, state_dir)
        self.export_dir = _resolve_workspace_path(self.workspace, export_dir)
        self.version_path = self.state_dir / "_version.json"
        self.interrupt_path = self.state_dir / "interrupted.json"

    @property
    def commands_path(self) -> Path:
        return self.export_dir / "commands.json"

    @property
    def report_path(self) -> Path:
        return self.export_dir / "report.json"

    def write_version(
        self,
        version_id: str,
        manifest_digest: str,
        events_digest: str,
    ) -> None:
        atomic_write_json(
            self.version_path,
            {
                "schema_version": ENGINE_SCHEMA,
                "version_id": version_id,
                "manifest_digest": manifest_digest,
                "events_digest": events_digest,
                "created_at_utc": datetime.now(UTC).isoformat(),
            },
        )

    def _read_version(self) -> dict[str, Any] | None:
        try:
            with self.version_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _archive_path(self, path: Path, version_hint: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        digest = hashlib.sha256(version_hint.encode("utf-8")).hexdigest()[:10]
        archive = path.parent / f"{path.name}.archive-{stamp}-{digest}"
        counter = 0
        while archive.exists():
            counter += 1
            archive = path.parent / f"{path.name}.archive-{stamp}-{digest}-{counter}"
        return archive

    def _archive_directory(self, path: Path, version_hint: str) -> Path:
        archive = self._archive_path(path, version_hint)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, archive)
        return archive

    def prepare(
        self,
        version_id: str,
        manifest_digest: str,
        events_digest: str,
    ) -> dict[str, Any]:
        archived_state: str | None = None
        archived_export: str | None = None
        reused = False
        if self.state_dir.exists():
            existing = self._read_version()
            if (
                existing
                and existing.get("schema_version") == ENGINE_SCHEMA
                and existing.get("version_id") == version_id
                and existing.get("manifest_digest") == manifest_digest
                and existing.get("events_digest") == events_digest
            ):
                reused = True
            else:
                version_hint = (
                    str(existing.get("version_id"))
                    if existing and existing.get("version_id")
                    else "unknown-version"
                )
                archive = self._archive_directory(self.state_dir, version_hint)
                archived_state = str(archive)
                if self.export_dir.exists() and any(self.export_dir.iterdir()):
                    export_archive = self._archive_directory(
                        self.export_dir, version_hint
                    )
                    archived_export = str(export_archive)
        elif self.export_dir.exists() and any(self.export_dir.iterdir()):
            export_archive = self._archive_directory(self.export_dir, "no-state")
            archived_export = str(export_archive)

        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "devices").mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_temp_files()
        if not reused:
            self.write_version(version_id, manifest_digest, events_digest)
        return {
            "reused_state": reused,
            "archived_state": archived_state,
            "archived_export": archived_export,
        }

    def cleanup_temp_files(self) -> None:
        for root in (self.state_dir, self.export_dir):
            if not root.exists():
                continue
            for path in root.rglob(".*.tmp"):
                try:
                    path.unlink()
                except OSError:
                    pass

    def device_dir(self, device_id: str) -> Path:
        path = self.state_dir / "devices" / _safe_device_component(device_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def block_path(self, device_id: str, filename: str) -> Path:
        return self.device_dir(device_id) / filename

    def write_block(self, device_id: str, filename: str, payload: dict[str, Any]) -> Path:
        path = self.block_path(device_id, filename)
        atomic_write_json(path, payload)
        return path

    def read_json(self, path: Path) -> Any:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def inspect_block(
        self,
        path: Path,
        expected: dict[str, Any],
    ) -> dict[str, Any]:
        if not path.exists():
            return {"ok": False, "reason": "missing", "path": str(path)}
        try:
            actual = self.read_json(path)
        except (OSError, ValueError, TypeError) as error:
            return {
                "ok": False,
                "reason": "invalid_json",
                "path": str(path),
                "message": str(error),
            }
        if not isinstance(actual, dict):
            return {"ok": False, "reason": "invalid_shape", "path": str(path)}
        declared_digest = actual.get("digest")
        without_digest = {key: value for key, value in actual.items() if key != "digest"}
        try:
            computed_digest = hashlib.sha256(
                canonical_json(without_digest).encode("utf-8")
            ).hexdigest()
        except (ValueError, TypeError) as error:
            return {
                "ok": False,
                "reason": "invalid_values",
                "path": str(path),
                "message": str(error),
            }
        if declared_digest != computed_digest:
            return {
                "ok": False,
                "reason": "digest_mismatch",
                "path": str(path),
                "declared": declared_digest,
                "computed": computed_digest,
            }
        commands = actual.get("commands")
        if not isinstance(commands, list) or actual.get("command_count") != len(commands):
            return {
                "ok": False,
                "reason": "command_count_mismatch",
                "path": str(path),
                "declared": actual.get("command_count"),
                "actual": len(commands) if isinstance(commands, list) else None,
            }
        if actual != expected:
            return {
                "ok": False,
                "reason": "content_mismatch",
                "path": str(path),
            }
        return {
            "ok": True,
            "reason": "valid",
            "path": str(path),
            "digest": declared_digest,
        }

    def scan_block_files(self, device_id: str) -> set[str]:
        directory = self.device_dir(device_id)
        return {path.name for path in directory.glob("block_*.json")}

    def remove_orphan_blocks(
        self, device_id: str, expected_filenames: set[str]
    ) -> list[str]:
        removed: list[str] = []
        for filename in sorted(self.scan_block_files(device_id) - expected_filenames):
            try:
                (self.device_dir(device_id) / filename).unlink()
                removed.append(filename)
            except OSError:
                pass
        return removed

    def write_interrupt(self, payload: dict[str, Any]) -> Path:
        atomic_write_json(self.interrupt_path, payload)
        return self.interrupt_path

    def clear_interrupt(self) -> None:
        try:
            self.interrupt_path.unlink(missing_ok=True)
        except OSError:
            pass

    def write_exports(
        self,
        commands_payload: dict[str, Any],
        report_payload: dict[str, Any],
    ) -> tuple[Path, Path]:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[Path, Path]] = []
        try:
            for target, value in (
                (self.commands_path, commands_payload),
                (self.report_path, report_payload),
            ):
                file_descriptor, temp_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
                )
                temp_path = Path(temp_name)
                with os.fdopen(
                    file_descriptor, "w", encoding="utf-8", newline="\n"
                ) as handle:
                    json.dump(
                        value,
                        handle,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                        allow_nan=False,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                staged.append((temp_path, target))
            for temp_path, target in staged:
                os.replace(temp_path, target)
        except BaseException:
            for temp_path, _ in staged:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return self.commands_path, self.report_path

    def archive_for_error(self, version_id: str) -> str | None:
        if not self.state_dir.exists():
            return None
        archive = self._archive_directory(self.state_dir, version_id)
        return str(archive)


def state_relative_path(store: StateStore, path: Path) -> str:
    try:
        return path.relative_to(store.workspace).as_posix()
    except ValueError:
        return str(path)
