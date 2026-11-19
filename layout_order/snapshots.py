"""快照版本化、整体替换与资源台账。

三类快照（layout_stats / cost_weights / order_result）各自维护单调递增版本号
与内容校验和（SHA-256，基于规范化 JSON）。检测到内容变化时写入新版本并整体
替换旧快照；读取时校验信封与载荷校验和，不符返回 ``E_SNAPSHOT_CORRUPT``。

缓存台账记录候选边复用与重算条目，自检时核对复用/重算数量是否对应。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .errors import E_SNAPSHOT_CORRUPT, LayoutOrderError

SNAPSHOT_KINDS = ("layout_stats", "cost_weights", "order_result")


def canonical_json(payload: dict) -> str:
    """规范化 JSON 序列化：键排序、无空白、固定换行风格，保证逐字节一致。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def checksum_of(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass
class SnapshotEnvelope:
    kind: str
    version: int
    checksum: str
    payload: dict

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "payload": self.payload,
        }


class SnapshotStore:
    """基于 JSON 文件的确定性快照存储。"""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, kind: str, key: str) -> str:
        safe_key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.directory, f"{kind}__{safe_key}.json")

    def put(self, kind: str, key: str, payload: dict) -> SnapshotEnvelope:
        if kind not in SNAPSHOT_KINDS:
            raise LayoutOrderError(
                E_SNAPSHOT_CORRUPT,
                f"未知快照类型 {kind}",
                {"category": "unknown_snapshot_kind", "kind": kind},
            )
        new_checksum = checksum_of(payload)
        existing = self._load_if_exists(kind, key)
        if existing is not None and existing.checksum == new_checksum:
            return existing
        next_version = 1 if existing is None else existing.version + 1
        envelope = SnapshotEnvelope(
            kind=kind,
            version=next_version,
            checksum=new_checksum,
            payload=payload,
        )
        # 整体替换：先写临时文件再原子改名，绝不做增量修补。
        path = self._path(kind, key)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(envelope.to_dict()))
        os.replace(tmp_path, path)
        return envelope

    def get(self, kind: str, key: str) -> SnapshotEnvelope | None:
        return self._load_if_exists(kind, key)

    def _load_if_exists(self, kind: str, key: str) -> SnapshotEnvelope | None:
        path = self._path(kind, key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                envelope_data = json.load(handle)
            envelope = SnapshotEnvelope(
                kind=envelope_data["kind"],
                version=envelope_data["version"],
                checksum=envelope_data["checksum"],
                payload=envelope_data["payload"],
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LayoutOrderError(
                E_SNAPSHOT_CORRUPT,
                f"快照文件损坏: {os.path.basename(path)}",
                {"category": "malformed_envelope", "path": path},
            ) from exc

        if envelope.kind != kind:
            raise LayoutOrderError(
                E_SNAPSHOT_CORRUPT,
                "快照类型与文件名不匹配",
                {"category": "kind_mismatch", "path": path, "expected": kind},
            )
        if not isinstance(envelope.version, int) or envelope.version < 1:
            raise LayoutOrderError(
                E_SNAPSHOT_CORRUPT,
                "快照版本号非法",
                {"category": "bad_version", "path": path},
            )
        actual_checksum = checksum_of(envelope.payload)
        if actual_checksum != envelope.checksum:
            raise LayoutOrderError(
                E_SNAPSHOT_CORRUPT,
                "快照内容校验和不符，拒绝增量修补",
                {
                    "category": "checksum_mismatch",
                    "path": path,
                    "expected": envelope.checksum,
                    "actual": actual_checksum,
                },
            )
        return envelope


class CacheLedger:
    """候选边缓存条目与复用/重算台账。"""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, "cache_ledger.json")
        self.entries: dict[str, dict] = {}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as handle:
                self.entries = json.load(handle)

    def key_for(self, page_id: str, config_checksum: str) -> str:
        return f"{page_id}|{config_checksum}"

    def get_or_build(
        self,
        page_id: str,
        config_checksum: str,
        edge_checksum: str,
        builder,
    ) -> tuple[object, dict]:
        """命中则复用，未命中则调用 builder 并记录一次重算。"""
        key = self.key_for(page_id, config_checksum)
        entry = self.entries.get(key)
        if entry is not None:
            if entry.get("edge_checksum") != edge_checksum:
                raise LayoutOrderError(
                    E_SNAPSHOT_CORRUPT,
                    "缓存条目校验和不符",
                    {
                        "category": "cache_checksum_mismatch",
                        "page_id": page_id,
                        "key": key,
                    },
                )
            entry["hits"] = int(entry.get("hits", 0)) + 1
            value = entry["payload"]
            record = {"action": "reuse", "key": key}
        else:
            value = builder()
            self.entries[key] = {
                "edge_checksum": edge_checksum,
                "hits": 0,
                "builds": 1,
                "payload": value,
            }
            record = {"action": "build", "key": key}
        self.flush()
        return value, record

    def record_recompute(self, page_id: str, config_checksum: str) -> dict:
        key = self.key_for(page_id, config_checksum)
        entry = self.entries.get(key)
        if entry is None:
            raise LayoutOrderError(
                E_SNAPSHOT_CORRUPT,
                "对不存在的缓存条目记录重算",
                {"category": "ledger_missing_entry", "key": key},
            )
        entry["recomputes"] = int(entry.get("recomputes", 0)) + 1
        self.flush()
        return {"action": "recompute", "key": key}

    def flush(self) -> None:
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(self.entries))
        os.replace(tmp_path, self.path)

    def summary(self) -> dict:
        builds = sum(int(entry.get("builds", 0)) for entry in self.entries.values())
        hits = sum(int(entry.get("hits", 0)) for entry in self.entries.values())
        recomputes = sum(
            int(entry.get("recomputes", 0)) for entry in self.entries.values()
        )
        return {
            "entries": len(self.entries),
            "builds": builds,
            "reuses": hits,
            "recomputes": recomputes,
        }
