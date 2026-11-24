from __future__ import annotations

from pathlib import Path
from typing import Any

from .canon import sha256_file
from .errors import E_DIGEST, E_SCHEMA, ScreenError
from .validate import load_json


def load_digest_manifest(snapshot_dir: str | Path) -> dict[str, Any]:
    path = Path(snapshot_dir) / "digests.json"
    data = load_json(path, code=E_DIGEST, what="快照摘要清单")
    if not isinstance(data, dict):
        raise ScreenError(E_DIGEST, "快照摘要清单根节点必须是对象", {"path": str(path)})
    if "snapshot_sets" not in data and "generations" not in data:
        raise ScreenError(E_DIGEST, "摘要清单缺少 snapshot_sets 或 generations", {"path": str(path)})
    return data


def normalized_snapshot_sets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """支持本项目 schema，也兼容 generations 形式的摘要清单。"""

    if isinstance(manifest.get("snapshot_sets"), list):
        sets = manifest["snapshot_sets"]
        return [item for item in sets if isinstance(item, dict)]

    generations = manifest.get("generations", {})
    sets = []
    if isinstance(generations, dict):
        for set_id, entry in generations.items():
            if not isinstance(entry, dict):
                continue
            files = entry.get("files", {})
            file_entries = []
            if isinstance(files, dict):
                for path, digest in files.items():
                    file_entries.append({"path": path, "digest": digest})
            sets.append(
                {
                    "set_id": str(set_id),
                    "generation": 2 if str(set_id) == "v2" else 1,
                    "logical_ms": entry.get("logical_ms", 0),
                    "preference": entry.get("preference", 100 if str(set_id) == "v2" else 10),
                    "declared_receiver_count": entry.get("declared_receivers", entry.get("declared_screens")),
                    "files": file_entries,
                }
            )
    return sets


def verify_file_digest(snapshot_dir: Path, file_entry: dict[str, Any]) -> dict[str, Any]:
    relative_path = file_entry.get("path")
    recorded = file_entry.get("digest")
    if not isinstance(relative_path, str) or not relative_path:
        raise ScreenError(E_DIGEST, "摘要记录缺少文件路径", {"entry": file_entry})
    if not isinstance(recorded, str) or not recorded:
        raise ScreenError(E_DIGEST, "摘要记录缺失", {"file": relative_path})
    path = snapshot_dir / relative_path
    if not path.is_file():
        raise ScreenError(E_DIGEST, "快照文件或分片缺失", {"file": relative_path})
    actual = sha256_file(path)
    if actual != recorded:
        raise ScreenError(
            E_DIGEST,
            "快照内容摘要不匹配，分片失效",
            {"file": relative_path, "recorded_digest": recorded, "actual_digest": actual},
        )
    return {"path": relative_path, "actual_digest": actual, "size_bytes": path.stat().st_size}


def actual_shard_receiver_count(path: Path, payload: Any | None = None) -> int:
    if payload is None:
        payload = load_json(path, code=E_DIGEST, what="快照分片")
    if not isinstance(payload, dict):
        raise ScreenError(E_DIGEST, "分片根节点必须是对象", {"path": str(path)})
    if isinstance(payload.get("records"), list):
        ids = {
            str(item.get("canonical_receiver_id") or item.get("receiver_id"))
            for item in payload["records"]
            if isinstance(item, dict)
        }
        return len(ids)
    receiver_ids = payload.get("receiver_ids")
    if isinstance(receiver_ids, list):
        return len(set(receiver_ids))
    receivers = payload.get("receivers")
    if isinstance(receivers, dict):
        return len(receivers)
    if isinstance(receivers, list):
        return len(receivers)
    raise ScreenError(E_DIGEST, "无法核对分片接收端数", {"path": str(path)})


def verify_snapshot_set(snapshot_dir: str | Path, set_entry: dict[str, Any]) -> dict[str, Any]:
    root = Path(snapshot_dir)
    set_id = str(set_entry.get("set_id", "unknown"))
    files = set_entry.get("files")
    if not isinstance(files, list) or not files:
        raise ScreenError(E_DIGEST, "快照集合缺少文件摘要记录", {"set_id": set_id})

    verified_files = []
    shard_receiver_total: set[str] = set()
    for file_entry in files:
        if not isinstance(file_entry, dict):
            raise ScreenError(E_DIGEST, "文件摘要项必须是对象", {"set_id": set_id})
        verified = verify_file_digest(root, file_entry)
        verified_files.append(verified)
        relative = verified["path"]
        is_shard = file_entry.get("kind") == "shard" or (
            relative.endswith(".json") and ("shard" in Path(relative).name or file_entry.get("role") == "shard")
        )
        if is_shard:
            path = root / relative
            payload = load_json(path, code=E_DIGEST, what="快照分片")
            declared = file_entry.get("declared_receiver_count")
            actual = actual_shard_receiver_count(path, payload)
            if declared is not None and declared != actual:
                raise ScreenError(
                    E_DIGEST,
                    "分片声明接收端数与实际不符，分片失效",
                    {"set_id": set_id, "file": relative, "declared": declared, "actual": actual},
                )
            if isinstance(payload.get("records"), list):
                shard_receiver_total.update(
                    str(item.get("canonical_receiver_id") or item.get("receiver_id"))
                    for item in payload["records"]
                    if isinstance(item, dict)
                )
            elif isinstance(payload.get("receiver_ids"), list):
                shard_receiver_total.update(map(str, payload["receiver_ids"]))

    declared_total = set_entry.get("declared_receiver_count")
    if declared_total is not None:
        try:
            declared_total_int = int(declared_total)
        except (TypeError, ValueError) as exc:
            raise ScreenError(E_DIGEST, "快照集合声明接收端数非法", {"set_id": set_id}) from exc
        if shard_receiver_total and declared_total_int != len(shard_receiver_total):
            raise ScreenError(
                E_DIGEST,
                "快照声明接收端数与分片实际合集不符",
                {"set_id": set_id, "declared": declared_total_int, "actual": len(shard_receiver_total)},
            )
    return {"set_id": set_id, "verified_files": verified_files}


def select_snapshot_set(snapshot_dir: str | Path, preferred_set_id: str | None = None) -> dict[str, Any]:
    root = Path(snapshot_dir)
    manifest = load_digest_manifest(root)
    sets = normalized_snapshot_sets(manifest)
    if preferred_set_id:
        sets.sort(key=lambda item: 0 if item.get("set_id") == preferred_set_id else 1)
    else:
        sets.sort(
            key=lambda item: (
                -int(item.get("preference", 0) or 0),
                -int(item.get("logical_ms", 0) or 0),
                str(item.get("set_id", "")),
            )
        )

    rejected: list[dict[str, Any]] = []
    for index, set_entry in enumerate(sets):
        try:
            verify_snapshot_set(root, set_entry)
            previous = sets[index - 1] if index > 0 else None
            return {
                "selected": set_entry,
                "rejected": rejected,
                "fallback": None
                if index == 0
                else {
                    "from_set_id": sets[0].get("set_id"),
                    "to_set_id": set_entry.get("set_id"),
                    "reason": "earlier_snapshot_set_failed_digest_validation",
                    "rejected_count": len(rejected),
                },
            }
        except ScreenError as exc:
            rejected.append(
                {
                    "set_id": set_entry.get("set_id"),
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            )
    raise ScreenError(E_DIGEST, "没有任何摘要有效的快照或分片可用于恢复", {"rejected": rejected})
