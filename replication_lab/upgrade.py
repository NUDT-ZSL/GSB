"""Crash-safe, resumable record-level segment upgrades."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from .errors import FormatError, StateError
from .segment import (
    CURRENT_FORMAT_VERSION,
    Segment,
    iter_segment_paths,
    read_segment,
    upgrade_segment_bytes,
)
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    ensure_dir,
    mark_read_only,
    read_json,
    sha256_bytes,
)

CHECKPOINT_VERSION = 1
CrashHook = Callable[[str, str], None]


class UpgradeManager:
    """Upgrade all lower-version segments to the current format.

    A checkpoint is updated only after an atomic segment replacement.  If a
    process dies after replacement but before the checkpoint write, restart
    recognizes the already-written target digest; if it dies earlier, only a
    temporary file can exist and the old segment continues to be authoritative.
    """

    def __init__(
        self,
        log_dir: Path,
        backup_dir: Path,
        checkpoint_path: Path,
    ) -> None:
        self.log_dir = log_dir
        self.backup_dir = backup_dir
        self.checkpoint_path = checkpoint_path
        ensure_dir(self.log_dir)
        ensure_dir(self.backup_dir)
        ensure_dir(self.checkpoint_path.parent)

    def _checkpoint_default(self) -> dict[str, Any]:
        return {
            "checkpoint_version": CHECKPOINT_VERSION,
            "current_format_version": CURRENT_FORMAT_VERSION,
            "segments": {},
        }

    def load_checkpoint(self) -> dict[str, Any]:
        if not self.checkpoint_path.exists():
            return self._checkpoint_default()
        try:
            checkpoint = read_json(self.checkpoint_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            quarantine = self.checkpoint_path.with_suffix(
                self.checkpoint_path.suffix + ".corrupt"
            )
            try:
                os.replace(self.checkpoint_path, quarantine)
            except OSError:
                pass
            checkpoint = self._checkpoint_default()
            checkpoint["recovered_from_corrupt_checkpoint"] = True
        if checkpoint.get("checkpoint_version") != CHECKPOINT_VERSION:
            raise StateError("unsupported upgrade checkpoint version")
        if not isinstance(checkpoint.get("segments"), dict):
            raise StateError("upgrade checkpoint has no segment map")
        return checkpoint

    def _write_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        checkpoint["current_format_version"] = CURRENT_FORMAT_VERSION
        atomic_write_json(self.checkpoint_path, checkpoint)

    def _backup_path(self, filename: str, source_version: int, digest: str) -> Path:
        return self.backup_dir / (
            f"{filename}.v{source_version}.{digest[:12]}.readonly.bak"
        )

    def _ensure_backup(
        self,
        *,
        filename: str,
        source_version: int,
        source_data: bytes,
        source_digest: str,
    ) -> Path:
        target = self._backup_path(filename, source_version, source_digest)
        if target.exists():
            existing = target.read_bytes()
            if sha256_bytes(existing) != source_digest:
                raise StateError(f"backup collision with different bytes: {target.name}")
        else:
            atomic_write_bytes(target, source_data)
        mark_read_only(target)
        return target

    def _read_optional_segment(self, path: Path) -> Segment | None:
        try:
            return read_segment(path)
        except FormatError:
            return None

    def run(self, *, crash_hook: CrashHook | None = None) -> dict[str, Any]:
        checkpoint = self.load_checkpoint()
        paths = list(iter_segment_paths(self.log_dir))
        # Temporary files are intentionally not in the .rlog namespace.  Any
        # stale temp files are harmless and can be removed safely.
        for temp in self.log_dir.glob(".*.rlog.tmp-*"):
            try:
                temp.unlink()
            except OSError:
                pass

        report: dict[str, Any] = {
            "business_code": None,
            "current_format_version": CURRENT_FORMAT_VERSION,
            "checkpoint_path": str(self.checkpoint_path),
            "scanned_segment_count": len(paths),
            "upgraded": [],
            "skipped": [],
            "failed": [],
            "backups": [],
            "version_set_before": [],
            "version_set_after": [],
            "complete": True,
        }
        before_versions: set[int] = set()

        for path in paths:
            filename = path.name
            source_data = path.read_bytes()
            source_digest = sha256_bytes(source_data)
            try:
                segment = read_segment(path)
            except FormatError as exc:
                record = checkpoint["segments"].setdefault(filename, {})
                record.update(
                    {
                        "status": "FAILED",
                        "source_digest": source_digest,
                        "error": exc.to_dict(),
                    }
                )
                report["failed"].append(
                    {
                        "filename": filename,
                        "source_digest": source_digest,
                        "error": exc.to_dict(),
                        "preserved_original": True,
                    }
                )
                report["complete"] = False
                report["business_code"] = "E_FORMAT"
                self._write_checkpoint(checkpoint)
                continue

            before_versions.update(segment.versions)
            prior = checkpoint["segments"].get(filename)
            if (
                segment.format_version == CURRENT_FORMAT_VERSION
                and all(record.format_version == CURRENT_FORMAT_VERSION for record in segment.records)
            ):
                completed_after_crash = bool(
                    prior
                    and prior.get("status") == "IN_PROGRESS"
                    and prior.get("target_digest") == segment.digest
                )
                record = checkpoint["segments"].setdefault(filename, {})
                record.update(
                    {
                        "status": "COMPLETED",
                        "source_digest": segment.digest,
                        "target_digest": segment.digest,
                        "source_format_version": CURRENT_FORMAT_VERSION,
                        "target_format_version": CURRENT_FORMAT_VERSION,
                    }
                )
                self._write_checkpoint(checkpoint)
                skipped_item = {
                    "filename": filename,
                    "format_version": segment.format_version,
                    "digest": segment.digest,
                }
                if completed_after_crash:
                    skipped_item["checkpoint_completed_after_crash"] = True
                report["skipped"].append(skipped_item)
                continue

            if prior and prior.get("status") == "COMPLETED":
                if prior.get("target_digest") == source_digest and all(
                    record.format_version == CURRENT_FORMAT_VERSION
                    for record in segment.records
                ):
                    report["skipped"].append(
                        {
                            "filename": filename,
                            "format_version": segment.format_version,
                            "digest": segment.digest,
                            "checkpoint_resumed": True,
                        }
                    )
                    continue

            target_data = upgrade_segment_bytes(segment)
            target_digest = sha256_bytes(target_data)

            # Crash after the atomic replacement but before checkpoint completion
            # leaves the target digest on disk while the checkpoint still says
            # IN_PROGRESS.  Trust it only after a full decode and digest match.
            try:
                if (
                    prior
                    and prior.get("status") == "IN_PROGRESS"
                    and prior.get("target_digest") == source_digest
                ):
                    target_segment = read_segment(path,)
                    if all(
                        record.format_version == CURRENT_FORMAT_VERSION
                        for record in target_segment.records
                    ):
                        prior["status"] = "COMPLETED"
                        prior["target_digest"] = target_digest
                        self._write_checkpoint(checkpoint)
                        report["skipped"].append(
                            {
                                "filename": filename,
                                "format_version": target_segment.format_version,
                                "digest": target_digest,
                                "checkpoint_completed_after_crash": True,
                            }
                        )
                        continue
            except FormatError:
                pass

            backup = self._ensure_backup(
                filename=filename,
                source_version=segment.format_version,
                source_data=source_data,
                source_digest=source_digest,
            )
            report["backups"].append(
                {
                    "filename": filename,
                    "backup_path": str(backup),
                    "source_format_version": segment.format_version,
                }
            )
            record = checkpoint["segments"].setdefault(filename, {})
            record.update(
                {
                    "status": "IN_PROGRESS",
                    "source_digest": source_digest,
                    "target_digest": target_digest,
                    "source_format_version": segment.format_version,
                    "target_format_version": CURRENT_FORMAT_VERSION,
                    "backup_path": str(backup),
                    "error": None,
                }
            )
            self._write_checkpoint(checkpoint)

            try:
                atomic_write_bytes(path, target_data)
            except OSError as exc:
                record["status"] = "FAILED"
                record["error"] = {
                    "business_code": "E_FORMAT",
                    "message": f"atomic segment replacement failed: {exc}",
                }
                report["failed"].append(
                    {
                        "filename": filename,
                        "source_digest": source_digest,
                        "error": record["error"],
                        "preserved_original": path.exists()
                        and sha256_bytes(path.read_bytes()) == source_digest,
                    }
                )
                report["complete"] = False
                report["business_code"] = "E_FORMAT"
                self._write_checkpoint(checkpoint)
                continue

            if crash_hook is not None:
                crash_hook("after_replace", filename)

            record["status"] = "COMPLETED"
            record["target_digest"] = target_digest
            self._write_checkpoint(checkpoint)
            report["upgraded"].append(
                {
                    "filename": filename,
                    "source_format_version": segment.format_version,
                    "target_format_version": CURRENT_FORMAT_VERSION,
                    "source_digest": source_digest,
                    "target_digest": target_digest,
                    "backup_path": str(backup),
                    "record_count": segment.record_count,
                }
            )

        after_versions: set[int] = set()
        for path in iter_segment_paths(self.log_dir):
            try:
                segment = read_segment(path)
            except FormatError:
                continue
            after_versions.update(segment.versions)
        report["version_set_before"] = sorted(before_versions)
        report["version_set_after"] = sorted(after_versions)
        if report["failed"]:
            report["business_code"] = report.get("business_code") or "E_FORMAT"
            report["complete"] = False
        self._write_checkpoint(checkpoint)
        return report


def upgrade_report_errors(report: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        list(report.get("failed", [])),
        key=lambda item: str(item.get("filename", "")),
    )
