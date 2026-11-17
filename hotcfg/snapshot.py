"""会话快照: 持久化与版本识别后的整体替换恢复。"""
from __future__ import annotations

import json
from pathlib import Path

from .center import ConfigCenter
from .errors import BizError, E_CONFIG
from .latency import STATS_SPEC_VERSION

SNAPSHOT_FORMAT = "hotcfg-snapshot-v1"


def save_snapshot(center: ConfigCenter, results: list[dict], path) -> dict:
    state = center.export_state()
    try:
        latency_stats = center.observer.stats_json(group_version=False)
    except BizError:
        latency_stats = None
    snap = {
        "format": SNAPSHOT_FORMAT,
        "stats_spec_version": STATS_SPEC_VERSION,
        **state,
        "latency_stats": latency_stats,
        "results": results,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=2, sort_keys=True),
                 encoding="utf-8")
    return snap


def load_snapshot(path) -> dict:
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BizError(E_CONFIG, f"cannot load snapshot {p}: {exc}",
                       {"position": "$", "path": str(p)}) from exc
    if not isinstance(doc, dict) or doc.get("format") != SNAPSHOT_FORMAT:
        raise BizError(E_CONFIG, f"unsupported snapshot format in {p}",
                       {"position": "$.format", "path": str(p)})
    return doc


def restore_session(path, current_center: ConfigCenter | None = None, logger=None):
    """重开快照。

    比较快照版本链摘要与当前会话: 一致则复用当前会话;
    不一致则整体替换配置、订阅与结果状态, 不做增量合并。
    返回 (center, results, replaced)。
    """
    snap = load_snapshot(path)
    snap_digest = snap["chain_digest"]
    if current_center is not None and current_center.chain_digest() == snap_digest:
        return current_center, snap.get("results", []), False
    center = ConfigCenter.from_state(snap, logger=logger)
    return center, snap.get("results", []), True
