"""固定种子确定性样本生成器.

生成: 投屏会话清单(sessions.json)、帧事件流(frames.jsonl)、
v1 单文件快照、v2 分片快照 + SQLite 元数据库、摘要清单(digests.json)。
同一 seed 重复生成的逻辑内容完全一致。
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

from .adjudicate import zero_state
from .canon import canonical, sha256_file
from .replay import replay_events, sort_events

DEFAULT_SEED = 42
FRAME_INTERVAL_MS = 33
KEYFRAME_EVERY = 30
CURRENT_GENERATION = 2

TOLERANCES = {"living-tv": 500, "meeting-sub": 500, "bedroom-tv": 300}


def _build_manifest(seed: int) -> dict:
    return {
        "format": "castdiag.manifest",
        "seed": seed,
        "sessions": [
            {
                "session_id": "sess-living",
                "multicast_group": "239.10.0.1",
                "protocol": "S1",
                "sender": {"device_id": "phone-1", "role": "sender"},
                "receivers": [
                    {"screen_id": "living-tv", "role": "receiver"},
                    {"screen_id": "bedroom-tv", "role": "receiver"},
                ],
                "geometry": {"width_px": 1920, "height_px": 1080, "scale": 1.0},
                "start_ms": 0,
            },
            {
                "session_id": "sess-meeting",
                "multicast_group": "239.10.0.2",
                "protocol": "S2",
                "sender": {"device_id": "phone-1", "role": "sender"},
                "receivers": [{"screen_id": "meeting-sub", "role": "receiver"}],
                "geometry": {"width_px": 1280, "height_px": 720, "scale": 1.5},
                "start_ms": 500,
            },
        ],
    }


def _frame_event(seq, session, screen_id, generation, frame_no, sample_ms, codec, keyframe):
    from .canon import sha256_bytes

    payload = sha256_bytes(
        canonical({"session": session, "screen": screen_id, "frame_no": frame_no})
    )
    return {
        "seq": seq,
        "session_id": session,
        "screen_id": screen_id,
        "generation": generation,
        "frame_no": frame_no,
        "sample_ms": sample_ms,
        "payload_hash": payload,
        "codec": codec,
        "keyframe": keyframe,
    }


def _build_events(manifest: dict, rng: random.Random) -> list[dict]:
    """生成帧事件流: 常规帧 + 迟到重传帧 + 一次代数回退帧."""
    plan = {
        # screen_id: (session_id, start_ms, 帧数, codec)
        "living-tv": ("sess-living", 0, 90, "h264"),
        "bedroom-tv": ("sess-living", 0, 20, "h264"),
        "meeting-sub": ("sess-meeting", 500, 90, "h265"),
    }
    arrivals = []  # (arrival_ms, order, event_dict_without_seq)
    order = 0
    for screen_id, (session_id, start_ms, count, codec) in plan.items():
        for i in range(count):
            sample_ms = start_ms + i * FRAME_INTERVAL_MS
            ev = _frame_event(
                None, session_id, screen_id, CURRENT_GENERATION,
                i + 1, sample_ms, codec, i % KEYFRAME_EVERY == 0,
            )
            arrivals.append((sample_ms, order, ev))
            order += 1

    # 代数回退事件: meeting-sub 收到一代(gen=1)旧帧重传, 触发回退重建
    rollback_ev = _frame_event(
        None, "sess-meeting", "meeting-sub", 1, 46, 500 + 45 * FRAME_INTERVAL_MS,
        "h265", False,
    )
    arrivals.append((500 + 45 * FRAME_INTERVAL_MS, order, rollback_ev))
    order += 1

    # 迟到重传事件: living-tv 末尾到达两条采样时刻远早于播放位置的帧
    for late_sample in (100, 200):
        late_ev = _frame_event(
            None, "sess-living", "living-tv", CURRENT_GENERATION,
            91 + len([a for a in arrivals if a[2]["screen_id"] == "living-tv" and a[2]["frame_no"] > 90]),
            late_sample, "h264", False,
        )
        arrivals.append((10_000 + late_sample, order, late_ev))
        order += 1

    rng.shuffle(arrivals)  # 仅打乱到达顺序, 之后按 (arrival, order) 稳定排序保证确定性
    arrivals.sort(key=lambda item: (item[0], item[1]))
    events = []
    for seq, (_, _, ev) in enumerate(arrivals):
        ev["seq"] = seq
        events.append(ev)
    return events


def _final_states(events: list[dict]) -> dict:
    """用与验收相同的重放逻辑计算每个接收屏的终态(写入 v2 元数据库)."""
    initial = {
        sid: zero_state(sid, CURRENT_GENERATION, tol)
        for sid, tol in TOLERANCES.items()
    }
    states, _trace, _counts = replay_events(sort_events(events), initial)
    return states


def _build_v1_snapshot() -> dict:
    """v1 单文件快照: 相对起始秒数 + 扁平状态标记 + 帧序号(代数 1, 取流中段状态)."""
    return {
        "format": "castdiag.v1",
        "generation": 1,
        "base_epoch_ms": 0,
        "screens": {
            "living-tv": {
                "rel_start_s": 1.485,
                "flags": ["playing", "muted"],
                "frame_no": 45,
                "keyframe_no": 31,
            },
            "meeting-sub": {
                "rel_start_s": 1.82,
                "flags": ["playing"],
                "frame_no": 40,
                "keyframe_no": 31,
            },
            "bedroom-tv": {
                "rel_start_s": 0.495,
                "flags": ["paused"],
                "frame_no": 15,
                "keyframe_no": 1,
            },
        },
    }


def _write_v2(v2_dir: Path, states: dict) -> list[str]:
    """写 v2 分片 JSON 与 SQLite 元数据库, 返回相对路径列表."""
    screens = sorted(states.keys())
    shards = [screens[:2], screens[2:]]
    rel_paths = []
    for idx, shard_screens in enumerate(shards):
        rel = f"v2/shard_{idx}.json"
        payload = {"format": "castdiag.v2.shard", "shard": idx, "screens": shard_screens}
        (v2_dir / f"shard_{idx}.json").write_bytes(canonical(payload))
        rel_paths.append(rel)

    db_path = v2_dir / "meta.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE screens ("
            " screen_id TEXT PRIMARY KEY,"
            " ts_ms INTEGER NOT NULL,"
            " generation INTEGER NOT NULL,"
            " keyframe_seq INTEGER NOT NULL,"
            " play_frame_no INTEGER NOT NULL,"
            " tolerance_ms INTEGER NOT NULL,"
            " tombstone INTEGER NOT NULL DEFAULT 0)"
        )
        for sid in screens:
            st = states[sid]
            tombstone = 1 if sid == "bedroom-tv" else 0
            conn.execute(
                "INSERT INTO screens VALUES (?,?,?,?,?,?,?)",
                (sid, st.position_ms, CURRENT_GENERATION, st.keyframe_seq,
                 st.decoded_frame_no, st.tolerance_ms, tombstone),
            )
        conn.commit()
    finally:
        conn.close()
    rel_paths.append("v2/meta.db")
    return rel_paths


def generate(out_dir, seed: int = DEFAULT_SEED) -> dict:
    """生成全部样本到 out_dir, 返回摘要信息."""
    out = Path(out_dir)
    snap = out / "snapshots"
    v1_dir = snap / "v1"
    v2_dir = snap / "v2"
    for d in (out, v1_dir, v2_dir):
        d.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    manifest = _build_manifest(seed)
    events = _build_events(manifest, rng)
    states = _final_states(events)

    (out / "sessions.json").write_bytes(canonical(manifest))
    with open(out / "frames.jsonl", "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")

    v1 = _build_v1_snapshot()
    (v1_dir / "snapshot_v1.json").write_bytes(canonical(v1))
    v2_rel = _write_v2(v2_dir, states)

    digests = {
        "format": "castdiag.digests",
        "generations": {
            "v1": {
                "files": {"v1/snapshot_v1.json": sha256_file(v1_dir / "snapshot_v1.json")},
                "declared_screens": len(v1["screens"]),
            },
            "v2": {
                "files": {rel: sha256_file(snap / rel) for rel in v2_rel},
                "declared_screens": len(states),
            },
        },
    }
    (snap / "digests.json").write_bytes(canonical(digests))

    return {
        "seed": seed,
        "out_dir": str(out),
        "sessions": len(manifest["sessions"]),
        "events": len(events),
        "screens": sorted(states.keys()),
    }
