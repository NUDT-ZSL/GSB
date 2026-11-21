"""Versioned, checksummed, atomically replaced alert artifacts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, sha256_obj
from .errors import E_CHECKSUM_MISMATCH, E_LOCK_TIMEOUT, AppError


def _summary(features: list[dict[str, Any]]) -> dict[str, Any]:
    by_severity: dict[str, int] = {}
    for feature in features:
        severity = str(feature["severity"])
        by_severity[severity] = by_severity.get(severity, 0) + 1
    return {
        "detected_count": len(features),
        "by_severity": dict(sorted(by_severity.items())),
    }


def build_alert_content(
    config_version: int,
    window: dict[str, Any],
    feature_records: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(
        feature_records,
        key=lambda record: (
            -int(record.get("_severity_order", 0)),
            -float(record.get("drift", 0.0)),
            str(record["feature"]),
        ),
    )
    clean_records = [
        {key: value for key, value in record.items() if not key.startswith("_")}
        for record in ordered
    ]
    return {
        "artifact_type": "drift_alert",
        "config_version": config_version,
        "window": window,
        "features": clean_records,
        "summary": _summary(clean_records),
    }


class AlertStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.history = self.root / "history"
        self.history.mkdir(parents=True, exist_ok=True)
        self.current_path = self.root / "current.json"
        self.lock_path = self.root / ".alerts.lock"

    def _load_envelope(self, path: Path) -> dict[str, Any]:
        try:
            with open(path, "rb") as handle:
                envelope = json.loads(handle.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AppError(
                E_CHECKSUM_MISMATCH,
                "告警工件无法读取或校验",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        content = envelope.get("content")
        checksum = envelope.get("checksum")
        if checksum != sha256_obj(content):
            raise AppError(
                E_CHECKSUM_MISMATCH,
                "告警内容校验和变化，工件不可信",
                details={"path": str(path), "alert_id": envelope.get("alert_id")},
            )
        version = int(envelope.get("version", 0))
        alert_id = f"alert-{version:06d}"
        if envelope.get("alert_id") != alert_id or version <= 0:
            raise AppError(
                E_CHECKSUM_MISMATCH,
                "告警版本号与告警 ID 不一致",
                details={"path": str(path), "version": version},
            )
        return envelope

    def load_current(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        return self._load_envelope(self.current_path)

    def list_alerts(self) -> list[dict[str, Any]]:
        alerts = []
        for path in sorted(self.history.glob("alert-*.json")):
            alerts.append(self._load_envelope(path))
        return sorted(alerts, key=lambda item: int(item["version"]))

    def get_alert(self, alert_id: str) -> dict[str, Any]:
        path = self.history / f"{alert_id}.json"
        if not path.exists():
            raise AppError(
                "E_ALERT_NOT_FOUND",
                "告警工件不存在",
                details={"alert_id": alert_id},
            )
        return self._load_envelope(path)

    def _acquire_lock(self, timeout_seconds: float = 10.0) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
                return descriptor
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.005)
        raise AppError(E_LOCK_TIMEOUT, "等待告警写锁超时")

    def _release_lock(self, descriptor: int) -> None:
        os.close(descriptor)
        self.lock_path.unlink(missing_ok=True)

    def _write_atomic(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary_path.unlink(missing_ok=True)

    def emit(
        self,
        config_version: int,
        window: dict[str, Any],
        feature_records: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not feature_records:
            return None
        content = build_alert_content(config_version, window, feature_records)
        checksum = sha256_obj(content)
        descriptor = self._acquire_lock()
        try:
            corrupted = False
            try:
                current = self.load_current()
            except AppError as error:
                if error.code != E_CHECKSUM_MISMATCH:
                    raise
                corrupted = True
                current = None
            if current is not None and current["checksum"] == checksum:
                return {**current, "created": False}
            history = self.list_alerts()
            history_max = max((int(item["version"]) for item in history), default=0)
            base_version = max(
                int(current["version"]) if current is not None else 0,
                history_max,
            )
            version = base_version + 1
            alert_id = f"alert-{version:06d}"
            envelope = {
                "alert_id": alert_id,
                "version": version,
                "checksum": checksum,
                "content": content,
            }
            payload = canonical_bytes(envelope)
            history_path = self.history / f"{alert_id}.json"
            if history_path.exists():
                existing = self._load_envelope(history_path)
                if existing["checksum"] != checksum:
                    raise AppError(
                        E_CHECKSUM_MISMATCH,
                        "同版本历史告警校验和不一致，拒绝增量修补",
                        details={"alert_id": alert_id},
                    )
            else:
                self._write_atomic(history_path, payload)
            self._write_atomic(self.current_path, payload)
            return {**envelope, "created": True, "replaced_corrupt_current": corrupted}
        finally:
            self._release_lock(descriptor)

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.history.mkdir(parents=True, exist_ok=True)

