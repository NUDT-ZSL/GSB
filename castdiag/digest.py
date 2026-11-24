"""快照防篡改与版本识别.

对每份快照及其分片计算 SHA-256 内容摘要并与 digests.json 记录值比对;
摘要不匹配、文件缺失或声明接收屏数与实际不符即判定失效(E_DIGEST)。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .canon import sha256_file
from .errors import DiagError, E_DIGEST, E_SCHEMA


def load_digest_manifest(snapshot_dir) -> dict:
    path = Path(snapshot_dir) / "digests.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagError(E_DIGEST, "摘要清单缺失或不可解析",
                        {"path": str(path), "cause": str(exc)})
    if not isinstance(data, dict) or not isinstance(data.get("generations"), dict):
        raise DiagError(E_SCHEMA, "摘要清单结构非法", {"path": str(path)})
    return data


def verify_generation(snapshot_dir, manifest: dict, generation: str) -> None:
    """校验某一代快照的全部文件摘要与声明接收屏数, 失败抛 E_DIGEST."""
    snap = Path(snapshot_dir)
    gens = manifest.get("generations", {})
    if generation not in gens:
        raise DiagError(E_DIGEST, "摘要清单缺少该代快照记录", {"generation": generation})
    entry = gens[generation]
    files = entry.get("files")
    if not isinstance(files, dict) or not files:
        raise DiagError(E_DIGEST, "摘要记录缺失", {"generation": generation})
    for rel, recorded in sorted(files.items()):
        path = snap / rel
        if not path.exists():
            raise DiagError(E_DIGEST, "快照文件缺失",
                            {"generation": generation, "file": rel})
        actual = sha256_file(path)
        if actual != recorded:
            raise DiagError(E_DIGEST, "快照摘要不匹配, 判定失效",
                            {"generation": generation, "file": rel,
                             "recorded": recorded, "actual": actual})
    declared = entry.get("declared_screens")
    if not isinstance(declared, int) or declared < 0:
        raise DiagError(E_DIGEST, "声明接收屏数非法",
                        {"generation": generation, "declared_screens": declared})
    actual_count = count_screens(snap, generation)
    if actual_count != declared:
        raise DiagError(E_DIGEST, "声明接收屏数与实际不符, 判定失效",
                        {"generation": generation, "declared": declared,
                         "actual": actual_count})


def count_screens(snapshot_dir: Path, generation: str) -> int:
    if generation == "v1":
        data = json.loads((snapshot_dir / "v1" / "snapshot_v1.json").read_text(encoding="utf-8"))
        return len(data.get("screens", {}))
    if generation == "v2":
        conn = sqlite3.connect(snapshot_dir / "v2" / "meta.db")
        try:
            return conn.execute("SELECT COUNT(*) FROM screens").fetchone()[0]
        finally:
            conn.close()
    raise DiagError(E_SCHEMA, "未知快照代数", {"generation": generation})


def select_valid_generations(snapshot_dir, generations=("v2", "v1")):
    """按新到旧顺序返回 (可用代列表, 失效记录列表).

    合并与恢复只使用通过摘要校验的代; 失效代被显式记录并拒绝,
    回退到最近一份有效快照, 禁止静默使用。
    """
    manifest = load_digest_manifest(snapshot_dir)
    valid, rejected = [], []
    for gen in generations:
        try:
            verify_generation(snapshot_dir, manifest, gen)
            valid.append(gen)
        except DiagError as exc:
            rejected.append({"generation": gen, "code": exc.code,
                             "message": exc.message, "details": exc.details})
    if not valid:
        raise DiagError(E_DIGEST, "全部快照均失效, 无有效快照可回退",
                        {"rejected": rejected})
    return valid, rejected
