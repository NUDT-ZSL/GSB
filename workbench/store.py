"""分片写盘、原子提交、指纹计算、清单与进度记录。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .codec import MAGIC as BLOCK_MAGIC, encode_varint, decode_varint
from .errors import WorkbenchError

INDEX_SHARD_MAGIC = b"IDXSH1"
MANIFEST_NAME = "manifest.json"
PROGRESS_NAME = "progress.json"


@dataclass(frozen=True)
class IndexConfig:
    vocab_path: str
    doc_shard_size: int
    block_size: int
    compression_level: int
    store_positions: bool
    query_concurrency: int
    latency_budget_ms: int
    low_freq_threshold: int = 2

    def validate(self) -> None:
        if self.compression_level not in (1, 2):
            raise WorkbenchError(
                f"非法压缩档位: {self.compression_level}",
                stage="config",
            )
        if self.doc_shard_size <= 0 or self.block_size <= 0:
            raise WorkbenchError(
                "分片大小与块大小必须为正整数",
                stage="config",
            )
        if self.query_concurrency <= 0 or self.latency_budget_ms < 0:
            raise WorkbenchError(
                "并发数必须为正整数，延迟预算必须为非负整数",
                stage="config",
            )
        if self.low_freq_threshold <= 0:
            raise WorkbenchError(
                "低频阈值必须为正整数",
                stage="config",
            )

    def structural_dict(self) -> dict[str, Any]:
        """参与配置指纹的结构性参数（不含档位与位置开关）。"""
        data = asdict(self)
        data.pop("compression_level")
        data.pop("store_positions")
        return data

    def with_format(self, level: int, store_positions: bool) -> "IndexConfig":
        return replace(
            self,
            compression_level=level,
            store_positions=store_positions,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IndexConfig":
        known = {field for field in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise WorkbenchError(
                f"配置存在未知字段: {sorted(unknown)}",
                stage="config",
            )
        config = cls(**data)
        config.validate()
        return config


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def vocab_fingerprint(vocab_path: str | os.PathLike[str]) -> str:
    with open(vocab_path, "r", encoding="utf-8") as handle:
        terms = [line.strip() for line in handle if line.strip()]
    return sha256_bytes(canonical_json(sorted(terms)))


def config_fingerprint(config: IndexConfig) -> str:
    return sha256_bytes(canonical_json(config.structural_dict()))


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """同目录临时文件 + fsync + os.replace 原子替换。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with open(tmp_path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, target)


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


class IndexLayout:
    """索引输出目录的路径约定。"""

    def __init__(self, base_dir: str | os.PathLike[str],
                 snapshot_dir: str | os.PathLike[str] | None = None):
        self.base_dir = Path(base_dir)
        self.doc_dir = self.base_dir / "docs"
        self.index_dir = self.base_dir / "index"
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else self.base_dir / "snapshots"

    def ensure(self) -> None:
        for directory in (self.base_dir, self.doc_dir, self.index_dir, self.snapshot_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def doc_shard_path(self, shard_id: int) -> Path:
        return self.doc_dir / f"doc_shard_{shard_id:05d}.jsonl"

    def index_shard_path(self, shard_id: int) -> Path:
        return self.index_dir / f"idx_shard_{shard_id:05d}.bin"

    @property
    def manifest_path(self) -> Path:
        return self.base_dir / MANIFEST_NAME

    @property
    def progress_path(self) -> Path:
        return self.base_dir / PROGRESS_NAME


def write_doc_shard(path: str | os.PathLike[str], raw_lines: list[str]) -> str:
    payload = ("\n".join(line.rstrip("\n") for line in raw_lines) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)
    return sha256_bytes(payload)


def write_index_shard(
    path: str | os.PathLike[str],
    shard_id: int,
    blocks: list[tuple[str, bytes]],
) -> str:
    out = bytearray(INDEX_SHARD_MAGIC)
    out += encode_varint(shard_id)
    out += encode_varint(len(blocks))
    for key, blob in blocks:
        out += encode_varint(len(blob))
        out += blob
        out += encode_varint(len(key.encode("utf-8")))
        out += key.encode("utf-8")
    atomic_write_bytes(path, bytes(out))
    return sha256_bytes(bytes(out))


def read_index_shard(path: str | os.PathLike[str]) -> dict[str, bytes]:
    with open(path, "rb") as handle:
        data = handle.read()
    if not data.startswith(INDEX_SHARD_MAGIC):
        raise WorkbenchError(
            f"倒排分片魔数不匹配: {path}",
            stage="decode",
        )
    offset = len(INDEX_SHARD_MAGIC)
    shard_id, offset = decode_varint(data, offset)
    count, offset = decode_varint(data, offset)
    result: dict[str, bytes] = {}
    for _ in range(count):
        blob_len, offset = decode_varint(data, offset)
        blob = data[offset : offset + blob_len]
        offset += blob_len
        key_len, offset = decode_varint(data, offset)
        key = data[offset : offset + key_len].decode("utf-8")
        offset += key_len
        if not blob.startswith(BLOCK_MAGIC):
            raise WorkbenchError(
                f"分片 {path} 内块 {key} 魔数不匹配",
                stage="decode",
            )
        result[key] = blob
    if offset != len(data):
        raise WorkbenchError(
            f"倒排分片 {path} 存在冗余字节",
            stage="decode",
        )
    return result


def version_fingerprint(
    vocab_fp: str,
    cfg_fp: str,
    level: int,
    store_positions: bool,
    shard_digests: list[tuple[str, str]],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"index-version:v1\n")
    digest.update(vocab_fp.encode("ascii"))
    digest.update(b"\n")
    digest.update(cfg_fp.encode("ascii"))
    digest.update(b"\n")
    digest.update(str(level).encode("ascii"))
    digest.update(b"\n")
    digest.update(("pos" if store_positions else "nopos").encode("ascii"))
    digest.update(b"\n")
    for name, shard_digest in sorted(shard_digests):
        digest.update(name.encode("utf-8"))
        digest.update(b":")
        digest.update(shard_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def dump_json(path: str | os.PathLike[str], data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2,
                                       sort_keys=True))


def load_json(path: str | os.PathLike[str]) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def snapshot_manifest(layout: IndexLayout, manifest: dict[str, Any]) -> Path:
    layout.ensure()
    name = (
        f"manifest-L{manifest['compression_level']}"
        f"-{'pos' if manifest['store_positions'] else 'nopos'}"
        f"-n{manifest['doc_count']}.json"
    )
    target = layout.snapshot_dir / name
    dump_json(target, manifest)
    return target


def remove_path(path: Path) -> None:
    if path.exists():
        path.unlink()


def reset_dirs(layout: IndexLayout) -> None:
    """运行前清空输出目录与快照目录。"""
    if layout.base_dir.exists():
        shutil.rmtree(layout.base_dir)
    if layout.snapshot_dir.exists() and layout.snapshot_dir != layout.base_dir / "snapshots":
        shutil.rmtree(layout.snapshot_dir)
    layout.ensure()
