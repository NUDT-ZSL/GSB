"""内容寻址缓存、增量失效传播与版本化快照提交。

缓存键全部由输入内容的 SHA-256 派生，配置、词项权重或文档批次
任何字节级变化都会改变对应键，进而沿模块依赖链传播失效。

SQLite 承担三类表：

- ``cache_entries``: 内容寻址的上游产物（按模块/键存校验和与载荷）
- ``snapshots``: 每类统计快照的单调版本号、校验和与提交序号
- ``meta``: 全局提交序号，保证同一时刻只有一个快照提交生效

所有写入在单个事务内完成，提交冲突返回 E_VERSION_CONFLICT / E_SNAPSHOT_BUSY。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    module TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    checksum TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (module, cache_key)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    committed_seq INTEGER NOT NULL,
    PRIMARY KEY (snapshot_type, version)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def content_checksum(payload: Any) -> str:
    """对任意 JSON 可序列化对象计算稳定 SHA-256。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def content_key(parts: dict[str, Any]) -> str:
    """由命名输入片段派生内容寻址缓存键。"""
    return hashlib.sha256(
        json.dumps(
            parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


@dataclass
class InvalidationPlan:
    """增量失效与复用裁决结果。"""

    changed_modules: list[str]
    affected_queries: set[str]
    recompute_queries: set[str]
    reuse_queries: set[str]
    module_keys: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_modules": self.changed_modules,
            "affected_queries": sorted(self.affected_queries),
            "recompute_queries": sorted(self.recompute_queries),
            "reuse_queries": sorted(self.reuse_queries),
            "module_keys": self.module_keys,
        }


# 模块依赖链：上游变化会使下游全部失效
DEPENDENCY_CHAIN = (
    "docs",
    "term_weights",
    "index",
    "route_config",
    "recall",
    "fusion_weights",
    "fusion",
)

# 每个模块变化直接影响的查询范围口径
_QUERY_SCOPED_MODULES = {"recall", "fusion_weights", "fusion"}


class SnapshotStore:
    """SQLite 支撑的缓存与版本化快照存储。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('commit_seq', 0)"
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- 内容寻址缓存 ----
    def get_entry(self, module: str, cache_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT checksum, payload_json, version FROM cache_entries "
            "WHERE module=? AND cache_key=?",
            (module, cache_key),
        ).fetchone()
        if row is None:
            return None
        checksum, payload_json, version = row
        payload = json.loads(payload_json)
        actual = content_checksum(payload)
        if actual != checksum:
            raise BusinessError(
                ErrorCode.CHECKSUM_MISMATCH,
                f"模块 {module} 缓存载荷校验和损坏",
                field_path=f"cache:{module}:{cache_key[:12]}",
            )
        return {"payload": payload, "checksum": checksum, "version": version}

    def put_entry(
        self, module: str, cache_key: str, payload: Any
    ) -> dict[str, Any]:
        checksum = content_checksum(payload)
        version = self._next_module_version(module)
        self._conn.execute(
            "INSERT INTO cache_entries(module, cache_key, checksum, payload_json, "
            "version, created_at) VALUES(?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(module, cache_key) DO UPDATE SET "
            "checksum=excluded.checksum, payload_json=excluded.payload_json, "
            "version=excluded.version, created_at=excluded.created_at",
            (module, cache_key, checksum, json.dumps(payload, ensure_ascii=False), version),
        )
        return {"cache_key": cache_key, "checksum": checksum, "version": version}

    def _next_module_version(self, module: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM cache_entries WHERE module=?",
            (module,),
        ).fetchone()
        return int(row[0]) + 1

    def list_keys(self, module: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT cache_key FROM cache_entries WHERE module=?", (module,)
        ).fetchall()
        return {row[0] for row in rows}

    # ---- 版本化快照与并发提交 ----
    def latest_snapshot(self, snapshot_type: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT version, checksum, payload_json, committed_seq FROM snapshots "
            "WHERE snapshot_type=? ORDER BY version DESC LIMIT 1",
            (snapshot_type,),
        ).fetchone()
        if row is None:
            return None
        version, checksum, payload_json, committed_seq = row
        payload = json.loads(payload_json)
        if content_checksum(payload) != checksum:
            raise BusinessError(
                ErrorCode.CHECKSUM_MISMATCH,
                f"快照 {snapshot_type} 校验和不匹配",
                field_path=f"snapshot:{snapshot_type}",
            )
        return {
            "version": version,
            "checksum": checksum,
            "payload": payload,
            "committed_seq": committed_seq,
        }

    def commit_snapshot(
        self,
        snapshot_type: str,
        payload: Any,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """单事务内整体替换旧快照并单调推进版本号。

        ``expected_version`` 做乐观并发控制：调用方若基于旧版本读取，
        提交时版本已被他人推进则拒绝；全局 commit_seq 保证串行生效。
        """
        with self._conn:  # 单事务
            self._conn.execute("BEGIN IMMEDIATE")
            current = self.latest_snapshot(snapshot_type)
            current_version = current["version"] if current else 0
            if expected_version is not None and expected_version != current_version:
                raise BusinessError(
                    ErrorCode.VERSION_CONFLICT,
                    (
                        f"快照 {snapshot_type} 版本冲突: 期望基于 "
                        f"{expected_version}，当前 {current_version}"
                    ),
                    field_path=f"snapshot:{snapshot_type}",
                )
            checksum = content_checksum(payload)
            if current and current["checksum"] == checksum:
                # 内容无变化，保持幂等，不推进版本
                return {
                    "version": current_version,
                    "checksum": checksum,
                    "committed_seq": current["committed_seq"],
                    "replaced": False,
                }
            next_version = current_version + 1
            seq_row = self._conn.execute(
                "SELECT value FROM meta WHERE key='commit_seq'"
            ).fetchone()
            next_seq = int(seq_row[0]) + 1
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='commit_seq'", (next_seq,)
            )
            self._conn.execute(
                "INSERT INTO snapshots(snapshot_type, version, checksum, "
                "payload_json, committed_seq) VALUES(?,?,?,?,?)",
                (
                    snapshot_type,
                    next_version,
                    checksum,
                    json.dumps(payload, ensure_ascii=False),
                    next_seq,
                ),
            )
        return {
            "version": next_version,
            "checksum": checksum,
            "committed_seq": next_seq,
            "replaced": True,
        }

    def snapshot_history(self, snapshot_type: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT version, checksum, committed_seq FROM snapshots "
            "WHERE snapshot_type=? ORDER BY version",
            (snapshot_type,),
        ).fetchall()
        return [
            {"version": row[0], "checksum": row[1], "committed_seq": row[2]}
            for row in rows
        ]


def build_module_keys(
    docs_checksum: str,
    weights_checksum: str,
    route_config_checksum: str,
    fusion_config_checksum: str,
    precision_checksum: str,
) -> dict[str, str]:
    """按依赖链为每个模块派生内容键。

    下游键包含上游键，因此任意上游修订都会级联改变下游键。
    """
    docs_key = content_key({"docs": docs_checksum})
    weights_key = content_key({"term_weights": weights_checksum})
    index_key = content_key(
        {"docs": docs_key, "term_weights": weights_key}
    )
    route_key = content_key({"route_config": route_config_checksum})
    recall_key = content_key(
        {"index": index_key, "route_config": route_key}
    )
    fusion_weight_key = content_key(
        {"fusion_config": fusion_config_checksum, "precision": precision_checksum}
    )
    fusion_key = content_key(
        {
            "recall": recall_key,
            "fusion_weights": fusion_weight_key,
        }
    )
    return {
        "docs": docs_key,
        "term_weights": weights_key,
        "index": index_key,
        "route_config": route_key,
        "recall": recall_key,
        "fusion_weights": fusion_weight_key,
        "fusion": fusion_key,
    }


def plan_invalidation(
    old_keys: dict[str, str],
    new_keys: dict[str, str],
    all_queries: list[str],
    query_affinity: dict[str, set[str]] | None = None,
) -> InvalidationPlan:
    """对比新旧模块键，决定受影响查询与重算/复用集合。

    ``query_affinity`` 给出查询级模块（如 recall）到查询集合的映射；
    上游公共模块（docs/index 等）变化视为影响全部查询。
    """
    changed_modules = [
        module
        for module in DEPENDENCY_CHAIN
        if old_keys.get(module) != new_keys.get(module)
    ]
    all_set = set(all_queries)
    if not changed_modules:
        # 输入完全未变：全部查询走复用，不重算
        return InvalidationPlan(
            changed_modules=[],
            affected_queries=set(),
            recompute_queries=set(),
            reuse_queries=set(all_set),
            module_keys=new_keys,
        )
    affected: set[str] = set()
    for module in changed_modules:
        if module in _QUERY_SCOPED_MODULES and query_affinity is not None:
            affected |= query_affinity.get(module, set(all_queries))
        else:
            affected |= set(all_queries)
    reuse = all_set - affected
    return InvalidationPlan(
        changed_modules=changed_modules,
        affected_queries=affected,
        recompute_queries=set(affected),
        reuse_queries=reuse,
        module_keys=new_keys,
    )
