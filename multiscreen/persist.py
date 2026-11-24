from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .canon import canonical, sha256_obj
from .errors import E_SCHEMA, ScreenError

CHUNK_RE = re.compile(r"^chunk_(\d{5})\.json$")
INTERRUPT_MARKER = "_interrupted.json"
EXIT_INTERRUPTED = 130


def version_id_of(manifest: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """由规范化会话清单和事件流计算版本标识。"""

    return sha256_obj(
        {
            "schema": "multiscreen.version.v1",
            "manifest": manifest.get("sessions", manifest),
            "events": events,
        }
    )


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """同目录临时文件 + fsync + os.replace 的原子 JSON 写入。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    data = canonical(payload)
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    try:
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


class ChunkStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def chunk_path(self, index: int) -> Path:
        return self.state_dir / f"chunk_{index:05d}.json"

    def write_chunk(self, index: int, version_id: str, body: dict[str, Any]) -> Path:
        envelope = {
            "format": "multiscreen.chunk",
            "chunk_format_version": 1,
            "version_id": version_id,
            "index": index,
            "body_digest": sha256_obj(body),
            "body": body,
        }
        path = self.chunk_path(index)
        atomic_write_json(path, envelope)
        return path

    def write_interrupt_marker(self, version_id: str, next_chunk_index: int, reason: str) -> Path:
        marker = {
            "format": "multiscreen.interrupt",
            "code": "E_INTERRUPT",
            "version_id": version_id,
            "next_chunk_index": next_chunk_index,
            "reason": reason,
            "exit_code": EXIT_INTERRUPTED,
        }
        path = self.state_dir / INTERRUPT_MARKER
        atomic_write_json(path, marker)
        return path

    def write_report(self, report: dict[str, Any], name: str = "report.json") -> Path:
        path = self.state_dir / name
        atomic_write_json(path, report)
        return path


def recover_chunks(state_dir: str | Path, version_id: str) -> dict[str, Any]:
    """恢复版本匹配且摘要有效的最长连续分块前缀。"""

    root = Path(state_dir)
    if not root.exists():
        return {"chunks": [], "bodies": [], "rejected": [], "last_state_by_receiver": {}}

    indexed: list[tuple[int, Path]] = []
    for name in sorted(os.listdir(root)):
        match = CHUNK_RE.match(name)
        if match:
            indexed.append((int(match.group(1)), root / name))
    indexed.sort()

    bodies: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for expected_index, (index, path) in enumerate(indexed):
        if index != expected_index:
            rejected.append({"file": path.name, "reason": "non_contiguous_chunk_prefix", "index": index})
            break
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            rejected.append({"file": path.name, "reason": "unparseable_chunk", "cause": str(exc)})
            break
        if not isinstance(envelope, dict) or envelope.get("format") != "multiscreen.chunk":
            rejected.append({"file": path.name, "reason": "bad_format"})
            break
        if envelope.get("version_id") != version_id:
            rejected.append({"file": path.name, "reason": "version_id_mismatch"})
            break
        body = envelope.get("body")
        if not isinstance(body, dict) or sha256_obj(body) != envelope.get("body_digest"):
            rejected.append({"file": path.name, "reason": "digest_mismatch"})
            break
        bodies.append(body)

    last_state: dict[str, Any] = {}
    for body in bodies:
        states = body.get("states")
        if isinstance(states, dict):
            last_state.update(states)
    return {
        "chunks": [f"chunk_{index:05d}.json" for index in range(len(bodies))],
        "bodies": bodies,
        "rejected": rejected,
        "last_state_by_receiver": last_state,
        "chunk_count": len(bodies),
    }


class StateMetadataDB:
    """状态目录中的 SQLite 元数据库；首次运行自动创建。"""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "metadata.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_seq INTEGER PRIMARY KEY AUTOINCREMENT,
              version_id TEXT NOT NULL,
              max_workers INTEGER,
              budget_bytes INTEGER,
              status TEXT NOT NULL,
              interrupt_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
              chunk_index INTEGER PRIMARY KEY,
              version_id TEXT NOT NULL,
              file_path TEXT NOT NULL,
              body_digest TEXT NOT NULL,
              event_start_seq INTEGER,
              event_end_seq INTEGER,
              logical_start_ms INTEGER,
              logical_end_ms INTEGER,
              receiver_count INTEGER
            );
            """
        )
        self.connection.commit()

    def start_run(self, version_id: str, max_workers: int, budget_bytes: int) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(version_id, max_workers, budget_bytes, status) VALUES (?,?,?,?)",
            (version_id, max_workers, budget_bytes, "running"),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def upsert_chunk(self, index: int, version_id: str, path: Path, body: dict[str, Any]) -> None:
        event_range = body.get("event_seq_range") or [None, None]
        logical_range = body.get("logical_ms_range") or [None, None]
        states = body.get("states") or {}
        self.connection.execute(
            """
            INSERT INTO chunks(chunk_index, version_id, file_path, body_digest,
                               event_start_seq, event_end_seq, logical_start_ms,
                               logical_end_ms, receiver_count)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chunk_index) DO UPDATE SET
              version_id=excluded.version_id,
              file_path=excluded.file_path,
              body_digest=excluded.body_digest,
              event_start_seq=excluded.event_start_seq,
              event_end_seq=excluded.event_end_seq,
              logical_start_ms=excluded.logical_start_ms,
              logical_end_ms=excluded.logical_end_ms,
              receiver_count=excluded.receiver_count
            """,
            (
                index,
                version_id,
                str(path),
                sha256_obj(body),
                event_range[0],
                event_range[1],
                logical_range[0],
                logical_range[1],
                len(states),
            ),
        )
        self.connection.commit()

    def finish_run(self, run_id: int, status: str, interrupt_reason: str | None = None) -> None:
        self.connection.execute(
            "UPDATE runs SET status=?, interrupt_reason=? WHERE run_seq=?",
            (status, interrupt_reason, run_id),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def load_interrupt_marker(state_dir: str | Path) -> dict[str, Any] | None:
    path = Path(state_dir) / INTERRUPT_MARKER
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScreenError(E_SCHEMA, "中断标记不可解析", {"path": str(path)}) from exc
    return data
