from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .concurrency import OrderedMemoryGate
from .digest import actual_shard_receiver_count, verify_file_digest
from .errors import E_SCHEMA, ScreenError


def _shard_order_key(entry: dict[str, Any]) -> tuple[str, str]:
    receiver_ids = entry.get("receiver_ids")
    if isinstance(receiver_ids, list) and receiver_ids:
        return str(min(map(str, receiver_ids))), str(entry.get("path", ""))
    return "~", str(entry.get("path", ""))


def load_shards_concurrent(
    snapshot_dir: str | Path,
    file_entries: list[dict[str, Any]],
    *,
    max_workers: int = 4,
    budget_bytes: int = 64 * 1024 * 1024,
) -> dict[str, dict[str, Any]]:
    """按接收端顺序申请预算，使用有界线程池并发校验并加载 JSON 分片。"""

    root = Path(snapshot_dir)
    shard_entries = [
        entry
        for entry in file_entries
        if isinstance(entry, dict)
        and (entry.get("kind") == "shard" or "shard" in Path(str(entry.get("path", ""))).name)
    ]
    ordered_entries = sorted(shard_entries, key=_shard_order_key)
    keys = [str(entry["path"]) for entry in ordered_entries]
    gate = OrderedMemoryGate(budget_bytes, keys)
    cancel_event = {"cancel": False}
    results: dict[str, dict[str, Any]] = {}
    errors: list[tuple[tuple[str, str], ScreenError]] = []

    def load_one(entry: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        relative_path = str(entry["path"])
        path = root / relative_path
        try:
            size = path.stat().st_size
        except OSError:
            size = int(entry.get("size_bytes", 0) or 0)
        need_bytes = max(1, size * 2 + 4096)
        gate.acquire(relative_path, need_bytes)
        try:
            if cancel_event["cancel"]:
                return None
            verified = verify_file_digest(root, entry)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ScreenError(E_SCHEMA, "快照分片不可解析", {"file": relative_path, "cause": str(exc)}) from exc
            declared = entry.get("declared_receiver_count")
            actual = actual_shard_receiver_count(path, payload)
            if declared is not None and int(declared) != actual:
                from .errors import E_DIGEST

                raise ScreenError(
                    E_DIGEST,
                    "并发加载时分片接收端数核对失败",
                    {"file": relative_path, "declared": declared, "actual": actual},
                )
            if not isinstance(payload, dict):
                raise ScreenError(E_SCHEMA, "分片根节点必须是对象", {"file": relative_path})
            payload["_verified_size_bytes"] = verified["size_bytes"]
            return relative_path, payload
        finally:
            gate.release(relative_path, need_bytes)

    workers = max(1, min(int(max_workers), max(1, len(ordered_entries))))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(load_one, entry): entry for entry in ordered_entries}
        for future in as_completed(future_map):
            entry = future_map[future]
            order_key = _shard_order_key(entry)
            try:
                outcome = future.result()
            except CancelledError:
                continue
            except ScreenError as exc:
                errors.append((order_key, exc))
                cancel_event["cancel"] = True
                for other in future_map:
                    other.cancel()
            except Exception as exc:  # deterministic business error envelope
                error = ScreenError(E_SCHEMA, "分片加载失败", {"cause": str(exc), "type": exc.__class__.__name__})
                errors.append((order_key, error))
                cancel_event["cancel"] = True
                for other in future_map:
                    other.cancel()
            else:
                if outcome is not None:
                    results[outcome[0]] = outcome[1]

    if errors:
        errors.sort(key=lambda item: item[0])
        primary = errors[0][1]
        primary.details.setdefault("all_error_count", len(errors))
        primary.details["ordered_errors"] = [
            {"order_key": list(key), "code": err.code, "message": err.message, "details": err.details}
            for key, err in errors
        ]
        raise primary
    return dict(sorted(results.items()))


def open_metadata_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise ScreenError(E_SCHEMA, "v2 SQLite 元数据库不存在", {"path": str(path)})
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise ScreenError(E_SCHEMA, "无法打开 SQLite 元数据库", {"path": str(path), "cause": str(exc)}) from exc
