"""Persistent block storage for one replica node."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BlockError, IntegrityError
from .spec import BlockManifest, NodeSpec, block_data_path
from .util import JsonlEventLog, atomic_write_bytes, canonical_json_text, is_positive_int, sha256_hex, utc_now_iso


@dataclass(frozen=True)
class StoredBlockInfo:
    block_id: str
    exists: bool
    byte_size: int
    digest: str
    manifest_match: bool
    path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "exists": self.exists,
            "byte_size": self.byte_size,
            "digest": self.digest,
            "manifest_match": self.manifest_match,
            "path": self.path,
        }


class NodeStorage:
    """Node-local block files plus a crash-safe protocol journal.

    Every write goes to a same-directory temporary file, is fsynced, and is then
    atomically installed with ``os.replace``.  Startup recovery only scans
    persistent block files and replays protocol records.  No process-exit report
    is produced or implied after a forced termination.
    """

    def __init__(
        self,
        node: NodeSpec,
        *,
        logger: JsonlEventLog | None = None,
    ) -> None:
        self.node = node
        self.logger = logger
        self.node.directory.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.node.directory / "protocol.jsonl"
        self._lock = threading.RLock()
        self._manifest_map = {block.block_id: block for block in node.blocks}

    @property
    def block_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._manifest_map))

    def _manifest(self, block_id: str) -> BlockManifest:
        manifest = self._manifest_map.get(block_id)
        if manifest is None:
            raise BlockError(
                "节点未持有该块",
                "protocol.block_id",
                node_id=self.node.node_id,
                block_id=block_id,
            )
        return manifest

    def _path(self, block_id: str) -> Path:
        return block_data_path(self.node, block_id)

    def append_protocol_record(self, record: dict[str, Any]) -> None:
        payload = {
            "timestamp_utc": utc_now_iso(),
            "node_id": self.node.node_id,
            **record,
        }
        line = canonical_json_text(payload) + "\n"
        with self._lock:
            with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())

    def recover(self) -> dict[str, Any]:
        """Rebuild the observable state from block files and protocol records."""

        with self._lock:
            blocks: list[StoredBlockInfo] = []
            for block_id in self.block_ids:
                manifest = self._manifest(block_id)
                path = self._path(block_id)
                if not path.is_file():
                    blocks.append(
                        StoredBlockInfo(
                            block_id=block_id,
                            exists=False,
                            byte_size=0,
                            digest="",
                            manifest_match=False,
                            path=str(path),
                        )
                    )
                    continue
                data = path.read_bytes()
                digest = sha256_hex(data)
                manifest_match = len(data) == manifest.byte_size
                if manifest_match:
                    cursor = 0
                    for shard in manifest.shards:
                        chunk = data[cursor : cursor + shard.byte_size]
                        cursor += shard.byte_size
                        if sha256_hex(chunk) != shard.digest:
                            manifest_match = False
                            break
                blocks.append(
                    StoredBlockInfo(
                        block_id=block_id,
                        exists=True,
                        byte_size=len(data),
                        digest=digest,
                        manifest_match=manifest_match,
                        path=str(path),
                    )
                )
            protocol_records = 0
            corrupt_protocol_records = 0
            put_records: dict[str, str] = {}
            if self.journal_path.is_file():
                import json

                with self.journal_path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            corrupt_protocol_records += 1
                            continue
                        protocol_records += 1
                        if record.get("event") == "PUT_COMMIT":
                            block_id = record.get("block_id")
                            digest = record.get("digest")
                            if isinstance(block_id, str) and isinstance(digest, str):
                                put_records[block_id] = digest
            summary = {
                "mode": "cold-scan",
                "exit_report": False,
                "blocks": [info.to_dict() for info in blocks],
                "protocol_records": protocol_records,
                "corrupt_protocol_records": corrupt_protocol_records,
                "put_records": [
                    {"block_id": key, "digest": put_records[key]}
                    for key in sorted(put_records)
                ],
                "observation": (
                    "recovered only from persistent block files and protocol records; "
                    "no clean-exit report is assumed"
                ),
            }
            if self.logger is not None:
                self.logger.emit(
                    "NODE_RECOVERY_SCAN",
                    node_id=self.node.node_id,
                    block_id="*",
                    field_path="node.directory",
                    context={
                        "mode": "cold-scan",
                        "exit_report": False,
                        "protocol_records": protocol_records,
                        "corrupt_protocol_records": corrupt_protocol_records,
                    },
                )
            return summary

    def read_block(self, block_id: str) -> tuple[bytes, str]:
        manifest = self._manifest(block_id)
        path = self._path(block_id)
        try:
            stat_result = path.stat()
        except OSError as exc:
            raise BlockError(
                "节点块文件不存在",
                "protocol.block_id",
                node_id=self.node.node_id,
                block_id=block_id,
                details={"path": str(path), "os_error": str(exc)},
            ) from exc
        if not path.is_file() or stat_result.st_size != manifest.byte_size:
            raise BlockError(
                "节点块文件长度异常",
                "protocol.block_id",
                node_id=self.node.node_id,
                block_id=block_id,
                details={
                    "expected_byte_size": manifest.byte_size,
                    "actual_byte_size": stat_result.st_size,
                },
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BlockError(
                "节点块文件读取失败",
                "protocol.block_id",
                node_id=self.node.node_id,
                block_id=block_id,
                details={"path": str(path), "os_error": str(exc)},
            ) from exc
        if len(data) != manifest.byte_size:
            raise BlockError(
                "节点块文件在读取期间长度变化",
                "protocol.block_id",
                node_id=self.node.node_id,
                block_id=block_id,
                details={
                    "expected_byte_size": manifest.byte_size,
                    "actual_byte_size": len(data),
                },
            )
        return data, sha256_hex(data)

    def query_digest(
        self, block_id: str, shard_layout: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        data, digest = self.read_block(block_id)
        manifest = self._manifest(block_id)
        layout = shard_layout
        if layout is None:
            layout = [
                {
                    "index": shard.index,
                    "offset": shard.offset,
                    "byte_size": shard.byte_size,
                }
                for shard in manifest.shards
            ]
        shard_rows = _validate_and_hash_layout(data, layout)
        return {
            "block_id": block_id,
            "byte_size": len(data),
            "digest": digest,
            "shards": shard_rows,
            "digest_algorithm": "sha256",
        }

    def put_block(
        self,
        block_id: str,
        data: bytes,
        *,
        expected_digest: str,
        expected_byte_size: int,
        request_id: str,
    ) -> dict[str, Any]:
        manifest = self._manifest(block_id)
        if not is_positive_int(expected_byte_size):
            raise BlockError(
                "修复写入的期望字节数必须为正整数",
                "protocol.expected_byte_size",
                node_id=self.node.node_id,
                block_id=block_id,
            )
        if len(data) != expected_byte_size:
            raise BlockError(
                "修复写入载荷长度与期望字节数不一致",
                "protocol.data",
                node_id=self.node.node_id,
                block_id=block_id,
                details={
                    "expected_byte_size": expected_byte_size,
                    "actual_byte_size": len(data),
                },
            )
        if expected_byte_size != manifest.byte_size:
            raise BlockError(
                "修复写入长度与节点块清单不一致",
                "protocol.expected_byte_size",
                node_id=self.node.node_id,
                block_id=block_id,
                details={
                    "manifest_byte_size": manifest.byte_size,
                    "expected_byte_size": expected_byte_size,
                },
            )
        if not isinstance(expected_digest, str) or len(expected_digest) != 64:
            raise IntegrityError(
                "修复写入的期望摘要格式非法",
                "protocol.expected_digest",
                node_id=self.node.node_id,
                block_id=block_id,
            )
        actual_digest = sha256_hex(data)
        if actual_digest != expected_digest.lower():
            raise IntegrityError(
                "修复写入载荷摘要不匹配",
                "protocol.expected_digest",
                node_id=self.node.node_id,
                block_id=block_id,
                details={
                    "expected_digest": expected_digest.lower(),
                    "actual_digest": actual_digest,
                },
            )
        path = self._path(block_id)
        with self._lock:
            atomic_write_bytes(path, data)
            self.append_protocol_record(
                {
                    "event": "PUT_COMMIT",
                    "request_id": request_id,
                    "block_id": block_id,
                    "digest": actual_digest,
                    "byte_size": len(data),
                    "file": path.name,
                }
            )
        return {
            "block_id": block_id,
            "byte_size": len(data),
            "digest": actual_digest,
            "committed": True,
        }


def _validate_and_hash_layout(
    data: bytes, layout: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(layout, list) or not layout:
        raise BlockError("分片布局不能为空", "protocol.shard_layout")
    cursor = 0
    result: list[dict[str, Any]] = []
    for expected_index, item in enumerate(layout):
        if not isinstance(item, dict):
            raise BlockError("分片布局项必须是对象", "protocol.shard_layout")
        index = item.get("index")
        offset = item.get("offset")
        byte_size = item.get("byte_size")
        if (
            index != expected_index
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset != cursor
            or not is_positive_int(byte_size)
        ):
            raise BlockError(
                "分片布局必须连续且 index 从 0 开始",
                f"protocol.shard_layout[{expected_index}]",
            )
        end = cursor + int(byte_size)
        if end > len(data):
            raise BlockError(
                "分片布局越过分块边界",
                f"protocol.shard_layout[{expected_index}].byte_size",
            )
        chunk = data[cursor:end]
        result.append(
            {
                "index": expected_index,
                "byte_size": len(chunk),
                "digest": sha256_hex(chunk),
            }
        )
        cursor = end
    if cursor != len(data):
        raise BlockError(
            "分片布局未覆盖整个块",
            "protocol.shard_layout",
            details={"covered": cursor, "byte_size": len(data)},
        )
    return result
