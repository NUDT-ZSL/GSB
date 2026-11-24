"""分块级持久化与中断恢复.

按逻辑时刻分块, 把合并视图、采纳裁决轨迹与版本标识(由会话清单和
事件流规范化摘要构成)原子写入状态目录: 先写同目录临时文件再整体替换。
每个分块自带内容摘要; 恢复时仅采纳摘要有效且版本标识一致的最长前缀。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .canon import canonical, sha256_obj
from .errors import DiagError, E_INTERRUPT, E_SCHEMA

CHUNK_RE = re.compile(r"^chunk_(\d{5})\.json$")
INTERRUPT_MARKER = "_interrupted.json"


def version_id_of(manifest: dict, events: list[dict]) -> str:
    """由会话清单与事件流的规范化摘要构成版本标识."""
    return sha256_obj({"manifest": manifest, "events": events})


def atomic_write(path: Path, payload: dict) -> None:
    """先写同目录临时文件再整体替换(原子生效)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(canonical(payload))
    os.replace(tmp, path)


class ChunkWriter:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def write_chunk(self, index: int, version_id: str, body: dict) -> Path:
        envelope = {
            "format": "castdiag.chunk",
            "version_id": version_id,
            "index": index,
            "digest": sha256_obj(body),
            "body": body,
        }
        path = self.state_dir / f"chunk_{index:05d}.json"
        atomic_write(path, envelope)
        return path

    def write_interrupted(self, version_id: str, next_index: int, reason: str) -> Path:
        marker = {
            "format": "castdiag.interrupted",
            "version_id": version_id,
            "next_chunk_index": next_index,
            "reason": reason,
            "code": E_INTERRUPT,
        }
        path = self.state_dir / INTERRUPT_MARKER
        atomic_write(path, marker)
        return path


def recover(state_dir, version_id: str) -> list[dict]:
    """恢复已持久化且摘要有效的最长连续分块前缀, 返回各分块 body."""
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return []
    chunks = []
    for name in sorted(os.listdir(state_dir)):
        match = CHUNK_RE.match(name)
        if not match:
            continue
        chunks.append((int(match.group(1)), state_dir / name))
    chunks.sort()
    bodies: list[dict] = []
    for expect, (index, path) in enumerate(chunks):
        if index != expect:
            break  # 分块不连续, 截断到最长有效前缀
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            break  # 强制终止留下的截断文件: 仅采纳此前已持久化的分块
        if envelope.get("format") != "castdiag.chunk":
            raise DiagError(E_SCHEMA, "分块格式标识非法", {"file": path.name})
        if envelope.get("version_id") != version_id:
            break  # 版本标识不一致: 不属于当前输入, 停止采纳
        body = envelope.get("body")
        if sha256_obj(body) != envelope.get("digest"):
            break  # 摘要无效: 拒绝该分块及其后全部内容
        bodies.append(body)
    return bodies
