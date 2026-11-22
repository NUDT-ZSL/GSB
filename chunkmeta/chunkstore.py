"""内容寻址分片目录：SHA-256 命名，临时文件 + 重命名原子落盘。

相同内容重复写入复用同一摘要文件，不产生重复副本。
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from .errors import E_CHUNK_MISSING, StoreError


class ChunkStore:
    def __init__(self, root):
        self.root = Path(root)
        self.chunks_dir = self.root / "chunks"
        self.tmp_dir = self.root / "tmp"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str) -> Path:
        return self.chunks_dir / digest[:2] / digest

    def put(self, content: bytes) -> str:
        """原子写入分片，返回摘要；相同内容复用已有文件。"""
        digest = hashlib.sha256(content).hexdigest()
        final = self._path_for(digest)
        if final.exists():
            return digest
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp_dir / f"{uuid.uuid4().hex}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, final)  # 已存在则放弃本次写入，复用旧文件
        except FileExistsError:
            pass
        except OSError:
            os.replace(tmp, final)  # 不支持硬链接时退化为原子重命名
            return digest
        tmp.unlink(missing_ok=True)
        return digest

    def get(self, digest: str) -> bytes:
        path = self._path_for(digest)
        if not path.exists():
            raise StoreError(E_CHUNK_MISSING, "chunk file missing",
                             digest=digest, path=str(path))
        return path.read_bytes()

    def exists(self, digest: str) -> bool:
        return self._path_for(digest).exists()

    def remove(self, digest: str) -> bool:
        path = self._path_for(digest)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_digests(self):
        result = []
        if not self.chunks_dir.exists():
            return result
        for sub in sorted(self.chunks_dir.iterdir()):
            if sub.is_dir():
                for item in sorted(sub.iterdir()):
                    if item.is_file():
                        result.append(item.name)
        return result

    def count_files(self) -> int:
        return len(self.list_digests())
