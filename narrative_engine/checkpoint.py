from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .encoding import atomic_write_json, canonical_digest
from .errors import DigestError


SNAPSHOT_FILE = "snapshot.json"
INTERRUPT_FILE = "interrupt.json"
HISTORY_DIR = "snapshots"
ARCHIVE_DIR = "archive"


def snapshot_envelope(body: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in body.items() if key != "digest"}
    payload["digest_algorithm"] = "sha256"
    payload["digest"] = canonical_digest(payload)
    return payload


def verify_snapshot_object(data: Any, *, path: str | Path | None = None) -> dict[str, Any]:
    location = str(path) if path is not None else "<snapshot>"
    if not isinstance(data, dict):
        raise DigestError("snapshot must be a JSON object", details={"path": location})
    digest = data.get("digest")
    if not isinstance(digest, str) or not digest:
        raise DigestError("snapshot digest is missing", details={"path": location, "reason": "missing_digest"})

    body = {key: value for key, value in data.items() if key != "digest"}
    expected = canonical_digest(body)
    if digest != expected:
        raise DigestError(
            "snapshot digest mismatch",
            details={
                "path": location,
                "reason": "digest_mismatch",
                "recorded_digest": digest,
                "computed_digest": expected,
            },
        )

    event_seq = data.get("event_seq")
    records = data.get("event_records")
    if not isinstance(event_seq, int) or isinstance(event_seq, bool) or event_seq < 0:
        raise DigestError("snapshot event_seq is invalid", details={"path": location, "reason": "bad_event_seq"})
    if not isinstance(records, list):
        raise DigestError("snapshot event_records is missing or invalid", details={"path": location, "reason": "bad_event_records"})
    if len(records) != event_seq:
        raise DigestError(
            "declared event_seq does not match actual event records",
            details={
                "path": location,
                "reason": "event_seq_mismatch",
                "declared_event_seq": event_seq,
                "actual_event_count": len(records),
            },
        )
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict) or record.get("seq") != index:
            raise DigestError(
                "event record sequence does not match its position",
                details={"path": location, "reason": "record_seq_mismatch", "position": index},
            )
        if record.get("event_ref") != f"event:{index}":
            raise DigestError(
                "event record ref does not match its sequence",
                details={"path": location, "reason": "record_ref_mismatch", "position": index},
            )
    return data


def verify_snapshot_file(path: str | Path) -> dict[str, Any]:
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        raise DigestError("snapshot file is missing", details={"path": str(snapshot_path), "reason": "missing_file"})
    try:
        with snapshot_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise DigestError(
            "snapshot is unreadable or invalid JSON",
            details={"path": str(snapshot_path), "reason": "invalid_json", "error": str(exc)},
        ) from exc
    return verify_snapshot_object(data, path=snapshot_path)


@dataclass
class RecoveryResult:
    body: dict[str, Any]
    source: Path
    issues: list[dict[str, Any]]


class CheckpointStore:
    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir)
        self.snapshot_path = self.root / SNAPSHOT_FILE
        self.interrupt_path = self.root / INTERRUPT_FILE
        self.history_dir = self.root / HISTORY_DIR
        self.archive_dir = self.root / ARCHIVE_DIR

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def save_snapshot(self, body: dict[str, Any]) -> Path:
        self.ensure_dirs()
        if self.snapshot_path.exists():
            self._archive_current_if_version_differs(body.get("version_id", "unknown"))
            self.history_dir.mkdir(parents=True, exist_ok=True)

        envelope = snapshot_envelope(body)
        event_seq = int(body.get("event_seq", 0))
        history_path = self.history_dir / f"snapshot-{event_seq:08d}.json"
        atomic_write_json(history_path, envelope)
        atomic_write_json(self.snapshot_path, envelope)
        return self.snapshot_path

    def write_interrupt(self, marker: dict[str, Any]) -> None:
        self.ensure_dirs()
        marker = dict(marker)
        marker.setdefault("status", "interrupted")
        atomic_write_json(self.interrupt_path, marker)

    def read_interrupt(self) -> dict[str, Any] | None:
        if not self.interrupt_path.exists():
            return None
        with self.interrupt_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def clear_interrupt(self) -> None:
        try:
            self.interrupt_path.unlink()
        except FileNotFoundError:
            pass

    def recover(
        self,
        *,
        expected_version: str | None = None,
        run_fingerprint: str | None = None,
    ) -> RecoveryResult | None | str:
        """Return RecoveryResult, None for no checkpoint, or 'version_mismatch'/'run_mismatch'."""

        issues: list[dict[str, Any]] = []
        current_body: dict[str, Any] | None = None
        current_source: Path | None = None

        if self.snapshot_path.exists():
            try:
                current_body = verify_snapshot_file(self.snapshot_path)
                current_source = self.snapshot_path
            except DigestError as exc:
                issues.append(exc.to_dict()["error"])
        else:
            issues.append({"code": "E_DIGEST", "reason": "missing_current_snapshot"})

        if current_body is not None:
            mismatch = self._identity_mismatch(current_body, expected_version, run_fingerprint)
            if mismatch:
                return mismatch
            return RecoveryResult(current_body, current_source or self.snapshot_path, issues)

        candidates = sorted(
            self.history_dir.glob("snapshot-*.json"),
            key=lambda path: self._sequence_from_name(path.name),
            reverse=True,
        ) if self.history_dir.exists() else []

        valid_issues: list[dict[str, Any]] = list(issues)
        for candidate in candidates:
            try:
                body = verify_snapshot_file(candidate)
            except DigestError as exc:
                valid_issues.append(exc.to_dict()["error"])
                continue
            mismatch = self._identity_mismatch(body, expected_version, run_fingerprint)
            if mismatch:
                continue
            return RecoveryResult(body, candidate, valid_issues)

        if any(item.get("reason") not in {"missing_current_snapshot"} for item in valid_issues):
            raise DigestError(
                "no valid snapshot could be recovered",
                details={"state_dir": str(self.root), "issues": valid_issues},
            )
        return None

    def archive_state(self, reason: str, body: dict[str, Any] | None = None) -> Path | None:
        if not self.snapshot_path.exists() and not self.interrupt_path.exists() and not self.history_dir.exists():
            return None
        version = (body or {}).get("version_id")
        fingerprint = (body or {}).get("run_fingerprint", "run")
        if version is None and self.snapshot_path.exists():
            try:
                import json

                with self.snapshot_path.open("r", encoding="utf-8") as handle:
                    old = json.load(handle)
                version = old.get("version_id", "unknown")
                fingerprint = old.get("run_fingerprint", fingerprint)
            except (OSError, json.JSONDecodeError):
                version = "unknown"
        safe_version = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(version or "unknown"))
        safe_fingerprint = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(fingerprint or "run"))[:24]
        destination = self.archive_dir / f"{reason}-{safe_version}-{safe_fingerprint}"
        suffix = 1
        while destination.exists():
            destination = self.archive_dir / f"{reason}-{safe_version}-{safe_fingerprint}-{suffix}"
            suffix += 1
        destination.mkdir(parents=True, exist_ok=False)

        for path in [self.snapshot_path, self.interrupt_path]:
            if path.exists():
                shutil.move(str(path), str(destination / path.name))
        if self.history_dir.exists():
            shutil.move(str(self.history_dir), str(destination / self.history_dir.name))
        return destination

    def _archive_current_if_version_differs(self, new_version: Any) -> None:
        try:
            with self.snapshot_path.open("r", encoding="utf-8") as handle:
                old = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        old_version = old.get("version_id") if isinstance(old, dict) else None
        if old_version and new_version and old_version != new_version:
            version = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(old_version))
            event_seq = old.get("event_seq", "unknown") if isinstance(old, dict) else "unknown"
            digest_prefix = str(old.get("digest", "unknown"))[:12] if isinstance(old, dict) else "unknown"
            destination = self.archive_dir / f"pre-replace-{version}-{event_seq}-{digest_prefix}"
            suffix = 1
            while destination.exists():
                destination = self.archive_dir / f"pre-replace-{suffix}"
                suffix += 1
            destination.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.snapshot_path), str(destination / SNAPSHOT_FILE))
            if self.history_dir.exists():
                shutil.move(str(self.history_dir), str(destination / HISTORY_DIR))

    @staticmethod
    def _identity_mismatch(
        body: dict[str, Any],
        expected_version: str | None,
        run_fingerprint: str | None,
    ) -> str | None:
        if expected_version is not None and body.get("version_id") != expected_version:
            return "version_mismatch"
        if run_fingerprint is not None and body.get("run_fingerprint") != run_fingerprint:
            return "run_mismatch"
        return None

    @staticmethod
    def _sequence_from_name(name: str) -> int:
        match = re.search(r"snapshot-(\d+)\.json$", name)
        return int(match.group(1)) if match else -1
