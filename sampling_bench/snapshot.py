"""版本化快照与整体替换。

三类快照（采样结果、预算账本、逐请求决策）各自维护单调递增版本号与
SHA-256 内容校验和。写入采用「同目录临时文件 + os.replace 原子替换」，
读取时校验版本号与校验和：

* 旧版本试图覆盖新版本 -> ``E_SNAPSHOT_ROLLBACK``；
* 文件损坏、JSON 非法或校验和不符 -> ``E_SNAPSHOT_CORRUPT``。

检测到版本/校验和变化时整体替换整份快照，绝不做增量修补。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BenchError, E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK

SNAPSHOT_KINDS = ("samples", "ledger", "decisions")


def canonical_payload(payload: Any) -> bytes:
    """统一的规范化 JSON 字节串，保证校验和跨进程稳定。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def checksum_of(payload: Any) -> str:
    return hashlib.sha256(canonical_payload(payload)).hexdigest()


@dataclass
class Snapshot:
    name: str
    version: int
    checksum: str
    payload: Any

    def to_envelope(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "checksum": checksum_of(self.payload),
            "payload": self.payload,
        }


class SnapshotStore:
    """快照目录，每个快照名对应一个原子替换的 JSON 文件。"""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        return self.directory / f"{name}.snapshot.json"

    def write(self, name: str, payload: Any, *, expected_next_version: int) -> Snapshot:
        """整体写入新版本快照。

        ``expected_next_version`` 必须恰好比当前已提交版本大 1，否则按回退/
        跳号处理为 ``E_SNAPSHOT_ROLLBACK``。
        """
        current = self.read_current(name) if self.path_for(name).exists() else None
        if current is not None:
            if expected_next_version <= current.version:
                raise BenchError(
                    E_SNAPSHOT_ROLLBACK,
                    (
                        f"snapshot {name} v{expected_next_version} cannot replace "
                        f"newer v{current.version}"
                    ),
                    stage="snapshot.write",
                    field=name,
                    detail={"current_version": current.version},
                )
            if expected_next_version != current.version + 1:
                raise BenchError(
                    E_SNAPSHOT_ROLLBACK,
                    (
                        f"snapshot {name} version jump: current v{current.version}, "
                        f"got v{expected_next_version}"
                    ),
                    stage="snapshot.write",
                    field=name,
                )

        snapshot = Snapshot(
            name=name,
            version=expected_next_version,
            checksum=checksum_of(payload),
            payload=payload,
        )
        envelope = snapshot.to_envelope()
        target = self.path_for(name)
        self._atomic_write_json(target, envelope)
        return snapshot

    def read_current(self, name: str) -> Snapshot:
        """读取并校验当前快照；任何损坏都抛出 ``E_SNAPSHOT_CORRUPT``。"""
        path = self.path_for(name)
        try:
            raw = path.read_text(encoding="utf-8")
            envelope = json.loads(raw)
            version = int(envelope["version"])
            payload = envelope["payload"]
            stored_checksum = str(envelope["checksum"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise BenchError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot {name} unreadable or malformed: {exc}",
                stage="snapshot.read",
                field=name,
            ) from exc

        actual_checksum = checksum_of(payload)
        if stored_checksum != actual_checksum:
            raise BenchError(
                E_SNAPSHOT_CORRUPT,
                (
                    f"checksum mismatch for snapshot {name} v{version}: "
                    f"stored={stored_checksum} actual={actual_checksum}"
                ),
                stage="snapshot.read",
                field=name,
                detail={"version": version, "snapshot_name": name},
            )
        if str(envelope.get("name")) != name:
            raise BenchError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot name mismatch: file={name} envelope={envelope.get('name')}",
                stage="snapshot.read",
                field=name,
                detail={"version": version, "snapshot_name": name},
            )
        return Snapshot(
            name=name, version=version, checksum=stored_checksum, payload=payload
        )

    def latest_version(self, name: str) -> int:
        if not self.path_for(name).exists():
            return 0
        return self.read_current(name).version

    @staticmethod
    def _atomic_write_json(target: Path, envelope: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
