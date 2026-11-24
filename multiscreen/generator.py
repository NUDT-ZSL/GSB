from __future__ import annotations

import json
import random
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .canon import canonical, canonical_text, sha256_file, sha256_obj
from .replay import run_replay
from .validate import normalize_events, normalize_manifest
from .verify import build_manifest_initial_states

DEFAULT_SEED = 394


class EventBuilder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def frame(
        self,
        *,
        session_id: str,
        receiver_id: str,
        frame_seq: int,
        sample_ms: int,
        frame_type: str = "delta",
        generation: int = 1,
        width: int = 1920,
        height: int = 1080,
        payload_length: int = 12000,
    ) -> None:
        self.events.append(
            {
                "event_seq": len(self.events) + 1,
                "session_id": session_id,
                "receiver_id": receiver_id,
                "frame_seq": frame_seq,
                "sample_ms": sample_ms,
                "generation": generation,
                "frame_type": frame_type,
                "keyframe": frame_type == "key",
                "coded_width_px": width,
                "coded_height_px": height,
                "payload_length": payload_length,
            }
        )

    def switch(
        self,
        *,
        session_id: str,
        receiver_id: str,
        sample_ms: int,
        to_generation: int = 2,
        reason: str = "explicit_marker",
    ) -> None:
        self.events.append(
            {
                "event_seq": len(self.events) + 1,
                "record_type": "switch",
                "session_id": session_id,
                "receiver_id": receiver_id,
                "sample_ms": sample_ms,
                "to_generation": to_generation,
                "reason": reason,
            }
        )


def build_manifest(seed: int) -> dict[str, Any]:
    return {
        "format": "multiscreen.manifest",
        "seed": seed,
        "policy": {"tolerance_breach_triggers_switch": True},
        "sessions": [
            {
                "session_id": "sess-room-main",
                "multicast_group": "239.39.4.1",
                "protocol_version": "S2",
                "sender": {"sender_id": "room-host", "role": "sender"},
                "receivers": [
                    {
                        "receiver_id": "rcv-side",
                        "aliases": ["FLAT-SIDE-01", "side-screen"],
                        "role": "side_screen",
                        "tolerance_ms": 260,
                    },
                    {
                        "receiver_id": "rcv-projector",
                        "aliases": ["FLAT-PROJ-02", "remote-projector"],
                        "role": "remote_projection",
                        "tolerance_ms": 220,
                    },
                ],
                "geometry": {"width_px": 3840, "height_px": 2160, "scale": 1.25},
                "start_ms": 1000,
                "switch_tolerance_ms": 400,
                "breach_switch_generation": 2,
            },
            {
                "session_id": "sess-room-aux",
                "multicast_group": "239.39.4.2",
                "protocol_version": "S1",
                "sender": {"sender_id": "room-host", "role": "sender"},
                "receivers": [
                    {
                        "receiver_id": "rcv-panel",
                        "aliases": ["FLAT-PANEL-OLD", "old-panel"],
                        "role": "control_panel",
                        "tolerance_ms": 500,
                    }
                ],
                "geometry": {"width_px": 1920, "height_px": 1080, "scale": 1.0},
                "start_ms": 0,
                "switch_tolerance_ms": 500,
            },
        ],
    }


def build_raw_events(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    builder = EventBuilder()
    main = "sess-room-main"
    aux = "sess-room-aux"
    base = 1000
    interval = 33

    def t(frame_seq: int) -> int:
        return base + (frame_seq - 1) * interval

    def payload() -> int:
        return rng.randint(9000, 42000)

    # 侧屏：丢弃帧造成缺口、关键帧恢复、显式代数切换、切换后新关键帧、迟到增量。
    side = "rcv-side"
    for seq, ftype in [
        (1, "key"),
        (2, "delta"),
        (3, "delta"),
        (4, "discard"),
        (5, "delta"),
        (6, "key"),
        (7, "delta"),
        (8, "delta"),
    ]:
        builder.frame(
            session_id=main,
            receiver_id=side,
            frame_seq=seq,
            sample_ms=t(seq),
            frame_type=ftype,
            generation=1,
            width=1920,
            height=1080,
            payload_length=payload(),
        )
    builder.switch(session_id=main, receiver_id=side, sample_ms=t(9), reason="explicit_generation_marker")
    builder.frame(session_id=main, receiver_id=side, frame_seq=9, sample_ms=t(9), generation=2,
                  payload_length=payload())
    builder.frame(session_id=main, receiver_id=side, frame_seq=10, sample_ms=t(10), generation=2,
                  payload_length=payload())
    builder.frame(session_id=main, receiver_id=side, frame_seq=11, sample_ms=t(11), frame_type="key",
                  generation=2, payload_length=payload())
    builder.frame(session_id=main, receiver_id=side, frame_seq=12, sample_ms=t(12), generation=2,
                  payload_length=payload())
    builder.frame(session_id=main, receiver_id=side, frame_seq=13, sample_ms=t(13), generation=2,
                  payload_length=payload())
    builder.frame(session_id=main, receiver_id=side, frame_seq=14, sample_ms=1100, generation=2,
                  payload_length=payload())
    builder.frame(session_id=main, receiver_id=side, frame_seq=15, sample_ms=t(15), generation=2,
                  payload_length=payload())

    # 投影端：第 4 帧关键帧丢失（序号从 3 跳到 5），容忍窗口超限触发代数切换。
    projector = "rcv-projector"
    for seq in (1, 2, 3):
        builder.frame(session_id=main, receiver_id=projector, frame_seq=seq, sample_ms=t(seq),
                      frame_type="key" if seq == 1 else "delta", generation=1, width=1280, height=720,
                      payload_length=payload())
    for seq in (5, 6, 7):
        builder.frame(session_id=main, receiver_id=projector, frame_seq=seq, sample_ms=t(seq), generation=1,
                      width=1280, height=720, payload_length=payload())
    builder.frame(session_id=main, receiver_id=projector, frame_seq=8, sample_ms=1400, generation=1,
                  width=1280, height=720, payload_length=payload())
    builder.frame(session_id=main, receiver_id=projector, frame_seq=9, sample_ms=t(9) + 120, generation=2,
                  width=1280, height=720, payload_length=payload())
    builder.frame(session_id=main, receiver_id=projector, frame_seq=10, sample_ms=t(10) + 120,
                  frame_type="key", generation=2, width=1280, height=720, payload_length=payload())
    builder.frame(session_id=main, receiver_id=projector, frame_seq=11, sample_ms=t(11) + 120, generation=2,
                  width=1280, height=720, payload_length=payload())
    builder.frame(session_id=main, receiver_id=projector, frame_seq=12, sample_ms=t(12) + 120, generation=2,
                  width=1280, height=720, payload_length=payload())

    # 旧控制面板：短流，v2 中打墓碑。
    panel = "rcv-panel"
    builder.frame(session_id=aux, receiver_id=panel, frame_seq=1, sample_ms=0, frame_type="key",
                  generation=1, width=1280, height=720, payload_length=payload())
    builder.frame(session_id=aux, receiver_id=panel, frame_seq=2, sample_ms=33, generation=1,
                  width=1280, height=720, payload_length=payload())
    return builder.events


def _trace_state_at_frame(replay: dict[str, Any], receiver_id: str, frame_seq: int) -> dict[str, Any]:
    for item in replay["results"][receiver_id]["trace"]:
        if item.get("frame_seq") == frame_seq:
            return item["state"]
    raise RuntimeError(f"missing trace frame {receiver_id} {frame_seq}")


def build_v1_snapshot(replay: dict[str, Any]) -> dict[str, Any]:
    side_state = _trace_state_at_frame(replay, "rcv-side", 8)
    projector_state = _trace_state_at_frame(replay, "rcv-projector", 3)
    panel_state = _trace_state_at_frame(replay, "rcv-panel", 1)

    def relative_seconds(state: dict[str, Any], start_ms: int) -> float:
        return round((state["playback_position_ms"] - start_ms) / 1000, 3)

    return {
        "format": "multiscreen.v1",
        "snapshot_id": "v1-flat-seed394",
        "generation": 1,
        "time_basis": "session.start_ms + relative_play_seconds * 1000",
        "records": {
            "FLAT-SIDE-01": {
                "session_id": "sess-room-main",
                "relative_play_seconds": relative_seconds(side_state, 1000),
                "flat_cursor_seq": side_state["adoption_cursor_seq"],
                "keyframe_seq": side_state["keyframe_seq"],
                "play_state": "playing",
            },
            "FLAT-PROJ-02": {
                "session_id": "sess-room-main",
                "relative_play_seconds": relative_seconds(projector_state, 1000),
                "flat_cursor_seq": projector_state["adoption_cursor_seq"],
                "keyframe_seq": projector_state["keyframe_seq"],
                "play_state": "waiting_keyframe",
            },
            "FLAT-PANEL-OLD": {
                "session_id": "sess-room-aux",
                "relative_play_seconds": relative_seconds(panel_state, 0),
                "flat_cursor_seq": panel_state["adoption_cursor_seq"],
                "keyframe_seq": panel_state["keyframe_seq"],
                "play_state": "stopped",
            },
        },
    }


def snapshot_record_from_state(state: dict[str, Any], *, tombstone: bool = False) -> dict[str, Any]:
    return {
        "canonical_receiver_id": state["receiver_id"],
        "source_receiver_id": state["receiver_id"],
        "session_id": state.get("session_id"),
        "generation": state["generation"],
        "logical_ts_ms": state["playback_position_ms"],
        "adoption_cursor_seq": state["adoption_cursor_seq"],
        "playback_position_ms": state["playback_position_ms"],
        "keyframe_seq": state["keyframe_seq"],
        "tombstone": tombstone,
        "wait_gap": state.get("open_gap"),
        "gap_intervals": state.get("gap_intervals", []),
    }


def write_v2_set(v2_dir: Path, records: list[dict[str, Any]], set_id: str) -> list[dict[str, Any]]:
    if v2_dir.exists():
        shutil.rmtree(v2_dir)
    v2_dir.mkdir(parents=True, exist_ok=True)
    file_entries: list[dict[str, Any]] = []

    for index, record in enumerate(records):
        relative = f"{v2_dir.name}/shard_{index:03d}.json"
        shard_path = v2_dir / f"shard_{index:03d}.json"
        payload = {
            "format": "multiscreen.v2.shard",
            "shard_id": f"{set_id}-{index:03d}",
            "receiver_ids": [record["canonical_receiver_id"]],
            "records": [{**record, "source_shard": relative}],
        }
        shard_path.write_bytes(canonical(payload))
        file_entries.append(
            {
                "path": relative,
                "kind": "shard",
                "declared_receiver_count": 1,
                "receiver_ids": [record["canonical_receiver_id"]],
            }
        )

    db_relative = f"{v2_dir.name}/meta.sqlite3"
    db_path = v2_dir / "meta.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE receiver_metadata (
              canonical_receiver_id TEXT PRIMARY KEY,
              source_receiver_id TEXT NOT NULL,
              session_id TEXT,
              source_shard TEXT NOT NULL,
              logical_ts_ms INTEGER NOT NULL,
              generation INTEGER NOT NULL,
              adoption_cursor_seq INTEGER NOT NULL,
              playback_position_ms INTEGER NOT NULL,
              keyframe_seq INTEGER,
              tombstone INTEGER NOT NULL DEFAULT 0,
              wait_gap_json TEXT,
              gap_intervals_json TEXT
            )
            """
        )
        for index, record in enumerate(records):
            source_shard = f"{v2_dir.name}/shard_{index:03d}.json"
            connection.execute(
                """
                INSERT INTO receiver_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["canonical_receiver_id"],
                    record["source_receiver_id"],
                    record.get("session_id"),
                    source_shard,
                    record["logical_ts_ms"],
                    record["generation"],
                    record["adoption_cursor_seq"],
                    record["playback_position_ms"],
                    record.get("keyframe_seq"),
                    1 if record["tombstone"] else 0,
                    canonical_text(record.get("wait_gap")) if record.get("wait_gap") else None,
                    canonical_text(record.get("gap_intervals", [])),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    file_entries.append({"path": db_relative, "kind": "sqlite"})
    return file_entries


def generate_sample(out_dir: str | Path = "task", seed: int = DEFAULT_SEED) -> dict[str, Any]:
    root = Path(out_dir)
    snapshot_root = root / "snapshots"
    v1_dir = snapshot_root / "v1"
    v2_dir = snapshot_root / "v2"
    v2_backup_dir = snapshot_root / "v2-backup"
    for directory in (root, snapshot_root, v1_dir):
        directory.mkdir(parents=True, exist_ok=True)

    raw_manifest = build_manifest(seed)
    manifest = normalize_manifest(raw_manifest)
    raw_events = build_raw_events(seed)
    events = normalize_events(raw_events, manifest)
    initial_states = build_manifest_initial_states(manifest)
    replay = run_replay(events, initial_states, workers=1, auto_switch_on_breach=True)

    (root / "sessions.json").write_bytes(canonical(raw_manifest) + b"\n")
    with (root / "frames.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    v1 = build_v1_snapshot(replay)
    v1_path = v1_dir / "snapshot_v1.json"
    v1_path.write_bytes(canonical(v1) + b"\n")

    records = []
    for receiver_id in ("rcv-side", "rcv-projector", "rcv-panel"):
        state = replay["results"][receiver_id]["state"]
        records.append(snapshot_record_from_state(state, tombstone=receiver_id == "rcv-panel"))

    v2_files = write_v2_set(v2_dir, records, "v2-current")
    backup_files = write_v2_set(v2_backup_dir, records, "v2-backup")

    snapshot_sets = [
        {
            "set_id": "v2-current",
            "kind": "v2",
            "generation": 2,
            "logical_ms": max(record["logical_ts_ms"] for record in records),
            "preference": 100,
            "declared_receiver_count": len(records),
            "files": [
                {**entry, "digest": sha256_file(snapshot_root / entry["path"])} for entry in v2_files
            ],
        },
        {
            "set_id": "v2-backup",
            "kind": "v2",
            "generation": 2,
            "logical_ms": max(record["logical_ts_ms"] for record in records),
            "preference": 90,
            "declared_receiver_count": len(records),
            "files": [
                {**entry, "digest": sha256_file(snapshot_root / entry["path"])} for entry in backup_files
            ],
        },
        {
            "set_id": "v1",
            "kind": "v1",
            "generation": 1,
            "logical_ms": 1231,
            "preference": 10,
            "declared_receiver_count": 3,
            "files": [
                {
                    "path": "v1/snapshot_v1.json",
                    "kind": "v1_json",
                    "digest": sha256_file(v1_path),
                }
            ],
        },
    ]
    digest_manifest = {
        "format": "multiscreen.digests",
        "algorithm": "sha256",
        "seed": seed,
        "snapshot_sets": snapshot_sets,
        "manifest_digest": sha256_obj(raw_manifest),
        "events_digest": sha256_obj(events),
    }
    (snapshot_root / "digests.json").write_bytes(canonical(digest_manifest) + b"\n")
    return {
        "out_dir": str(root),
        "seed": seed,
        "sessions": len(raw_manifest["sessions"]),
        "events": len(events),
        "receivers": len(records),
        "version_preview": sha256_obj({"manifest": manifest["sessions"], "events": events}),
    }
