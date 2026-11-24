from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .hashing import sha256_hex
from .replay import adjudicate_stream, state_at_logic_ms
from .validation import parse_events, parse_manifest


SEED = 384
LATE_TOLERANCE_MS = 1200
CHUNK_INTERVAL_MS = 2000


def _on_rm_error(func: Any, path: str, exc_info: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except Exception:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _session_specs() -> List[Dict[str, Any]]:
    return [
        {
            "session_id": "sess-home-living-s1",
            "multicast_group": "239.10.38.1:5000",
            "protocol": "S1",
            "sender": {"id": "sender-phone-384", "role": "sender_phone"},
            "start_logic_ms": 1000,
            "screens": [
                {
                    "screen_id": "screen-living-big",
                    "canonical_id": "canon:display:living-room-big",
                    "role": "living_room_big_screen",
                    "width_px": 3840,
                    "height_px": 2160,
                    "scale": 2.0,
                }
            ],
        },
        {
            "session_id": "sess-meeting-secondary-s2",
            "multicast_group": "239.10.38.2:5000",
            "protocol": "S2",
            "sender": {"id": "sender-phone-384", "role": "sender_phone"},
            "start_logic_ms": 1000,
            "screens": [
                {
                    "screen_id": "screen-meeting-secondary",
                    "canonical_id": "canon:display:meeting-room-secondary",
                    "role": "meeting_room_secondary_screen",
                    "width_px": 1920,
                    "height_px": 1080,
                    "scale": 1.0,
                }
            ],
        },
    ]


def _frame_specs(seed: int) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    offsets = list(range(0, 6500, 500))
    for index, offset_ms in enumerate(offsets):
        frame_seq = index + 1
        specs.append(
            {
                "frame_seq": frame_seq,
                "offset_ms": offset_ms,
                "generation": 1 if frame_seq <= 6 else 2,
                "is_keyframe": frame_seq in {1, 5, 7, 9, 13},
                "kind": "normal",
            }
        )
    specs.extend(
        [
            {
                "frame_seq": 14,
                "offset_ms": 1000,
                "generation": 2,
                "is_keyframe": False,
                "kind": "late_dropped",
            },
            {
                "frame_seq": 15,
                "offset_ms": 6500,
                "generation": 1,
                "is_keyframe": False,
                "kind": "generation_rollback",
            },
            {
                "frame_seq": 16,
                "offset_ms": 7000,
                "generation": 1,
                "is_keyframe": False,
                "kind": "post_rollback_adopted",
            },
        ]
    )
    return specs


def _build_events(seed: int, sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs = _frame_specs(seed)
    events: List[Dict[str, Any]] = []
    event_seq = 1
    for spec in specs:
        for session in sessions:
            screen = session["screens"][0]
            screen_id = screen["screen_id"]
            sampled_at_ms = session["start_logic_ms"] + spec["offset_ms"]
            payload_digest = sha256_hex(
                f"{seed}|{session['session_id']}|{screen_id}|{spec['frame_seq']}|{spec['offset_ms']}"
            )
            events.append(
                {
                    "event_seq": event_seq,
                    "session_id": session["session_id"],
                    "screen_id": screen_id,
                    "frame_seq": spec["frame_seq"],
                    "sampled_at_ms": sampled_at_ms,
                    "generation": spec["generation"],
                    "is_keyframe": spec["is_keyframe"],
                    "payload_digest": payload_digest,
                    "scenario_kind": spec["kind"],
                }
            )
            event_seq += 1
    return events


def _build_v1_records(manifest: Any, replay: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    stale_overrides = {
        4000: {"keyframe_seq": 5, "decoded_frame_seq": 8, "play_position_ms": 3900, "status": "stalled"},
        6000: {"keyframe_seq": 5, "decoded_frame_seq": 12, "play_position_ms": 5900, "status": "stalled"},
    }
    for offset_ms in (2000, 4000, 6000):
        for session in manifest.sessions:
            screen = session.screens[0]
            logic_ms = session.start_logic_ms + offset_ms
            state = state_at_logic_ms(manifest, replay, screen.screen_id, logic_ms)
            record = {
                "screen_id": screen.screen_id,
                "relative_second": offset_ms // 1000,
                "status": "playing",
                "keyframe_seq": state["keyframe_seq"],
                "decoded_frame_seq": state["decoded_frame_seq"],
                "play_position_ms": state["play_position_ms"],
            }
            if offset_ms in stale_overrides:
                record.update(stale_overrides[offset_ms])
            records.append(record)
    return records


def _write_v2(
    v2_dir: Path,
    manifest: Any,
    replay: Any,
) -> None:
    shards_dir = v2_dir / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    db_path = v2_dir / "metadata.sqlite"
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE shards (
            shard_id TEXT PRIMARY KEY,
            relative_path TEXT NOT NULL,
            logic_ms INTEGER NOT NULL,
            declared_screen_count INTEGER NOT NULL,
            digest TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE screen_records (
            canonical_screen_id TEXT NOT NULL,
            logic_ms INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            keyframe_seq INTEGER,
            decoded_frame_seq INTEGER,
            play_position_ms INTEGER,
            tombstone INTEGER NOT NULL DEFAULT 0,
            shard_id TEXT NOT NULL REFERENCES shards(shard_id),
            PRIMARY KEY (canonical_screen_id, logic_ms, shard_id)
        )
        """
    )

    checkpoint_offsets = (2000, 4000, 6000)
    for checkpoint_index, offset_ms in enumerate(checkpoint_offsets):
        shard_id = f"shard-{checkpoint_index:03d}-cp{offset_ms // 1000}s"
        logic_ms = 1000 + offset_ms
        records: List[Dict[str, Any]] = []
        for session in manifest.sessions:
            screen = session.screens[0]
            state = state_at_logic_ms(manifest, replay, screen.screen_id, logic_ms)
            records.append(
                {
                    "canonical_screen_id": screen.canonical_id,
                    "logic_ms": logic_ms,
                    "generation": state["generation"],
                    "keyframe_seq": state["keyframe_seq"],
                    "decoded_frame_seq": state["decoded_frame_seq"],
                    "play_position_ms": state["play_position_ms"],
                    "tombstone": False,
                }
            )
        doc = {"shard_id": shard_id, "logic_ms": logic_ms, "records": records}
        digest = sha256_hex(doc)
        relative_path = f"shards/{shard_id}.json"
        _write_json(v2_dir / relative_path, doc)
        connection.execute(
            "INSERT INTO shards VALUES (?, ?, ?, ?, ?)",
            (shard_id, relative_path, logic_ms, len(manifest.sessions), digest),
        )
        for record in records:
            connection.execute(
                """
                INSERT INTO screen_records
                (canonical_screen_id, logic_ms, generation, keyframe_seq,
                 decoded_frame_seq, play_position_ms, tombstone, shard_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["canonical_screen_id"],
                    record["logic_ms"],
                    record["generation"],
                    record["keyframe_seq"],
                    record["decoded_frame_seq"],
                    record["play_position_ms"],
                    0,
                    shard_id,
                ),
            )

    post_rollback_shard_id = "shard-003-post-rollback"
    tombstone_logic_ms = 8000
    post_rollback_records: List[Dict[str, Any]] = []
    for session in manifest.sessions:
        screen = session.screens[0]
        state = state_at_logic_ms(manifest, replay, screen.screen_id, tombstone_logic_ms)
        post_rollback_records.append(
            {
                "canonical_screen_id": screen.canonical_id,
                "logic_ms": tombstone_logic_ms,
                "generation": state["generation"],
                "keyframe_seq": state["keyframe_seq"],
                "decoded_frame_seq": state["decoded_frame_seq"],
                "play_position_ms": state["play_position_ms"],
                "tombstone": False,
            }
        )
    post_rollback_doc = {
        "shard_id": post_rollback_shard_id,
        "logic_ms": tombstone_logic_ms,
        "records": post_rollback_records,
    }
    post_rollback_digest = sha256_hex(post_rollback_doc)
    post_rollback_relative = f"shards/{post_rollback_shard_id}.json"
    _write_json(v2_dir / post_rollback_relative, post_rollback_doc)
    connection.execute(
        "INSERT INTO shards VALUES (?, ?, ?, ?, ?)",
        (
            post_rollback_shard_id,
            post_rollback_relative,
            tombstone_logic_ms,
            len(post_rollback_records),
            post_rollback_digest,
        ),
    )
    for record in post_rollback_records:
        connection.execute(
            """
            INSERT INTO screen_records
            (canonical_screen_id, logic_ms, generation, keyframe_seq,
             decoded_frame_seq, play_position_ms, tombstone, shard_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["canonical_screen_id"],
                record["logic_ms"],
                record["generation"],
                record["keyframe_seq"],
                record["decoded_frame_seq"],
                record["play_position_ms"],
                0,
                post_rollback_shard_id,
            ),
        )

    tombstone_shard_id = "shard-004-tombstone"
    tombstone_record = {
        "canonical_screen_id": "canon:display:living-room-big",
        "logic_ms": tombstone_logic_ms,
        "generation": 3,
        "keyframe_seq": None,
        "decoded_frame_seq": None,
        "play_position_ms": None,
        "tombstone": True,
    }
    tombstone_doc = {
        "shard_id": tombstone_shard_id,
        "logic_ms": tombstone_logic_ms,
        "records": [tombstone_record],
    }
    tombstone_digest = sha256_hex(tombstone_doc)
    tombstone_relative = f"shards/{tombstone_shard_id}.json"
    _write_json(v2_dir / tombstone_relative, tombstone_doc)
    connection.execute(
        "INSERT INTO shards VALUES (?, ?, ?, ?, ?)",
        (tombstone_shard_id, tombstone_relative, tombstone_logic_ms, 1, tombstone_digest),
    )
    connection.execute(
        """
        INSERT INTO screen_records
        (canonical_screen_id, logic_ms, generation, keyframe_seq,
         decoded_frame_seq, play_position_ms, tombstone, shard_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tombstone_record["canonical_screen_id"],
            tombstone_logic_ms,
            tombstone_record["generation"],
            None,
            None,
            None,
            1,
            tombstone_shard_id,
        ),
    )
    connection.commit()
    connection.close()


def _injection_manifest() -> Dict[str, Any]:
    return {
        "manifest_schema": "screen-diag-injections/v1",
        "read_only_input": True,
        "note": "Pass --inject <case_id>; the runner materializes a mutable copy and never edits samples/.",
        "cases": [
            {
                "case_id": "corrupt_digest",
                "title": "Corrupt one v2 shard while leaving SQLite digest unchanged",
                "target": "snapshots/v2/shards/shard-001-cp4s.json",
                "expect_code": "E_DIGEST",
            },
            {
                "case_id": "screen_count_mismatch",
                "title": "Declare one extra receiver screen in SQLite",
                "target_database": "snapshots/v2/metadata.sqlite",
                "target_shard_id": "shard-001-cp4s",
                "expect_code": "E_DIGEST",
            },
            {
                "case_id": "time_out_of_bounds",
                "title": "Move event_seq=1 beyond logic_bounds.max_ms",
                "target": "frame_events.json",
                "event_seq": 1,
                "sampled_at_ms": 999999,
                "expect_code": "E_SCHEMA",
            },
            {
                "case_id": "replay_drift",
                "title": "Rewrite and re-digest an early keyframe so digest checks pass but replay fails",
                "target": "snapshots/v2/shards/shard-000-cp2s.json",
                "canonical_screen_id": "canon:display:living-room-big",
                "field": "keyframe_seq",
                "new_value": 4,
                "expect_code": "E_REPLAY",
            },
        ],
    }


def generate_sample_tree(out_dir: Path | str, seed: int = SEED) -> Path:
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir, onerror=_on_rm_error)
    out_dir.mkdir(parents=True, exist_ok=True)

    sessions = _session_specs()
    manifest_raw = {
        "schema": "screen-diag-manifest/v1",
        "seed": seed,
        "logic_bounds": {"min_ms": 0, "max_ms": 12000},
        "adjudication": {"late_tolerance_ms": LATE_TOLERANCE_MS},
        "persistence": {"chunk_interval_ms": CHUNK_INTERVAL_MS},
        "sessions": sessions,
    }
    _write_json(out_dir / "sessions.json", manifest_raw)

    events_raw = _build_events(seed, sessions)
    _write_json(out_dir / "frame_events.json", {"schema": "screen-diag-events/v1", "events": events_raw})

    manifest = parse_manifest(manifest_raw)
    events = parse_events({"events": events_raw}, manifest)
    replay = adjudicate_stream(manifest, events)

    v1_doc = {
        "snapshot_version": "v1-flat",
        "generated_from_seed": seed,
        "implicit_generation": 1,
        "records": _build_v1_records(manifest, replay),
    }
    _write_json(out_dir / "snapshots" / "v1.json", v1_doc)
    _write_v2(out_dir / "snapshots" / "v2", manifest, replay)

    injection_path = out_dir / "injection_manifest.json"
    _write_json(injection_path, _injection_manifest())
    try:
        os.chmod(injection_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass

    return out_dir
