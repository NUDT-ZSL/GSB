from __future__ import annotations

import copy
import shutil
import threading
from pathlib import Path
from typing import Any

from .errors import fail
from .utils import atomic_write_json, content_hash, read_json


SNAPSHOT_CATEGORIES = ("chains", "trust_table", "arbitration")


class SnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            if self.root.exists():
                shutil.rmtree(self.root)
            self.root.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        category: str,
        payload: Any,
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        if category not in SNAPSHOT_CATEGORIES:
            raise ValueError(f"unknown snapshot category: {category}")
        with self._lock:
            latest = self._read_current(category, required=False)
            checksum = content_hash(payload)
            if latest is not None:
                current_version = int(latest["version"])
                if version is not None and int(version) < current_version:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "禁止用旧版本覆盖新快照",
                        category=category,
                        version=int(version),
                        current_version=current_version,
                    )
                if version is None:
                    version = current_version + 1
                if int(version) == current_version:
                    if checksum == latest["checksum"]:
                        return latest
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "同版本快照校验和发生变化，必须递增版本后整体替换",
                        category=category,
                        version=int(version),
                        current_version=current_version,
                        reason="checksum_changed_without_version_bump",
                    )
            else:
                version = int(version if version is not None else 1)
                if version < 1:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "快照版本必须从 1 开始",
                        category=category,
                        version=version,
                        current_version=0,
                    )

            envelope = {
                "schema_version": "snapshot-v1",
                "category": category,
                "version": int(version),
                "checksum": checksum,
                "payload": payload,
            }
            history_path = self._history_path(category, int(version))
            current_path = self._current_path(category)
            atomic_write_json(history_path, envelope)
            atomic_write_json(current_path, envelope)
            return self._metadata(envelope)

    def latest(self, category: str, *, verify: bool = True) -> dict[str, Any] | None:
        with self._lock:
            return self._read_current(category, required=False, verify=verify)

    def status(self) -> dict[str, Any]:
        result = {}
        for category in SNAPSHOT_CATEGORIES:
            snapshot = self.latest(category)
            if snapshot is None:
                result[category] = {
                    "category": category,
                    "version": 0,
                    "checksum": None,
                    "exists": False,
                }
            else:
                result[category] = {
                    **self._metadata(snapshot),
                    "exists": True,
                }
        return result

    def probe_rollback(self, category: str) -> None:
        latest = self.latest(category)
        if latest is None:
            fail(
                "E_SNAPSHOT_ROLLBACK",
                "快照不存在，无法执行回退探针",
                category=category,
                version=0,
                current_version=0,
            )
        stale_version = max(0, int(latest["version"]) - 1)
        fail(
            "E_SNAPSHOT_ROLLBACK",
            "旧版本快照被拒绝覆盖当前版本",
            category=category,
            version=stale_version,
            current_version=int(latest["version"]),
        )

    def probe_corruption(self, category: str) -> None:
        latest = self.latest(category)
        if latest is None:
            fail(
                "E_SNAPSHOT_CORRUPT",
                "快照不存在，无法完成校验",
                category=category,
                version=0,
            )
        corrupted = copy.deepcopy(latest)
        corrupted["payload"] = {
            "__corruption_probe__": True,
            "original_checksum": corrupted["checksum"],
        }
        probe_path = self.root / category / f".corrupt-probe-v{latest['version']}.json"
        atomic_write_json(probe_path, corrupted)
        try:
            self._verify_envelope(corrupted, category, int(latest["version"]))
        finally:
            if probe_path.exists():
                probe_path.unlink()

    def read_version(self, category: str, version: int) -> dict[str, Any]:
        with self._lock:
            path = self._history_path(category, int(version))
            if not path.exists():
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "指定版本快照不存在",
                    category=category,
                    version=int(version),
                )
            envelope = read_json(path)
            return self._verify_envelope(envelope, category, int(version))

    def _read_current(
        self,
        category: str,
        *,
        required: bool,
        verify: bool = True,
    ) -> dict[str, Any] | None:
        path = self._current_path(category)
        if not path.exists():
            if required:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "当前快照不存在",
                    category=category,
                    version=0,
                )
            return None
        envelope = read_json(path)
        if verify:
            return self._verify_envelope(
                envelope,
                category,
                int(envelope.get("version", 0)),
            )
        return envelope

    def _verify_envelope(
        self,
        envelope: dict[str, Any],
        category: str,
        version: int,
    ) -> dict[str, Any]:
        actual = content_hash(envelope.get("payload"))
        if (
            envelope.get("category") != category
            or int(envelope.get("version", -1)) != int(version)
            or envelope.get("checksum") != actual
        ):
            fail(
                "E_SNAPSHOT_CORRUPT",
                "快照内容校验和不匹配",
                category=category,
                version=int(version),
                expected_checksum=envelope.get("checksum"),
                actual_checksum=actual,
            )
        return envelope

    def _metadata(self, envelope: dict[str, Any]) -> dict[str, Any]:
        return {
            "category": envelope["category"],
            "version": int(envelope["version"]),
            "checksum": envelope["checksum"],
            "payload_hash": content_hash(envelope["payload"]),
        }

    def _current_path(self, category: str) -> Path:
        return self.root / category / "current.json"

    def _history_path(self, category: str, version: int) -> Path:
        return self.root / category / f"v{version:06d}.json"
