from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any


def _fragment(rng: random.Random, size: int) -> str:
    return bytes(rng.randrange(0, 256) for _ in range(size)).hex()


def build_sample_scenario(seed: int = 20261120) -> dict[str, Any]:
    rng = random.Random(seed)
    ping_payload = bytes(rng.randrange(0, 256) for _ in range(8)).hex()

    streams = [
        {
            "expected_final_state": "closed",
            "initial_recv_window": 20000,
            "initial_send_window": 12000,
            "stream_id": 1,
            "weight": 16,
        },
        {
            "expected_final_state": "closed",
            "initial_recv_window": 20000,
            "initial_send_window": 4000,
            "stream_id": 3,
            "weight": 8,
        },
        {
            "expected_final_state": "closed",
            "initial_recv_window": 20000,
            "initial_send_window": 6000,
            "stream_id": 5,
            "weight": 4,
        },
        {
            "expected_final_state": "closed",
            "initial_recv_window": 20000,
            "initial_send_window": 5000,
            "stream_id": 7,
            "weight": 32,
        },
        {
            "expected_final_state": "closed",
            "initial_recv_window": 20000,
            "initial_send_window": 3000,
            "stream_id": 9,
            "weight": 8,
        },
    ]

    events: list[dict[str, Any]] = []

    def add(at_ms: int, **event: Any) -> None:
        item = {
            "at_ms": at_ms,
            "seq": len(events) + 1,
        }
        item.update(event)
        events.append(item)

    add(
        0,
        ack=False,
        direction="inbound",
        settings={"MAX_CONCURRENT_STREAMS": 8, "MAX_FRAME_SIZE": 16384},
        type="SETTINGS",
    )
    add(1, ack=True, direction="outbound", settings={}, type="SETTINGS")
    add(
        2,
        ack=False,
        direction="inbound",
        payload_hex=ping_payload,
        type="PING",
    )
    add(
        3,
        ack=True,
        direction="outbound",
        payload_hex=ping_payload,
        type="PING",
    )

    add(
        10,
        direction="outbound",
        end_headers=False,
        end_stream=False,
        fragment_hex=_fragment(rng, 19),
        stream_id=1,
        type="HEADERS",
    )
    add(
        11,
        direction="outbound",
        end_headers=True,
        fragment_hex=_fragment(rng, 23),
        stream_id=1,
        type="CONTINUATION",
    )
    for stream_id in (3, 5):
        add(
            12 + stream_id,
            direction="outbound",
            end_headers=True,
            end_stream=False,
            fragment_hex=_fragment(rng, 17),
            stream_id=stream_id,
            type="HEADERS",
        )

    add(
        20,
        direction="outbound",
        end_stream=True,
        length=9000,
        stream_id=1,
        type="DATA",
    )
    add(
        21,
        direction="outbound",
        end_stream=True,
        length=7000,
        stream_id=3,
        type="DATA",
    )
    add(
        22,
        direction="outbound",
        end_stream=True,
        length=5000,
        stream_id=5,
        type="DATA",
    )

    add(
        30,
        direction="inbound",
        increment=6000,
        level="connection",
        type="WINDOW_UPDATE",
    )
    add(
        31,
        direction="inbound",
        increment=3000,
        level="stream",
        stream_id=3,
        type="WINDOW_UPDATE",
    )
    add(
        32,
        direction="inbound",
        increment=12000,
        level="connection",
        type="WINDOW_UPDATE",
    )
    add(
        33,
        direction="inbound",
        increment=8000,
        level="connection",
        type="WINDOW_UPDATE",
    )

    add(
        40,
        direction="inbound",
        end_stream=True,
        length=1024,
        stream_id=1,
        type="DATA",
    )
    for stream_id in (3, 5):
        add(
            40 + stream_id,
            direction="inbound",
            end_headers=True,
            end_stream=True,
            fragment_hex=_fragment(rng, 11),
            stream_id=stream_id,
            type="HEADERS",
        )

    add(
        60,
        direction="outbound",
        end_headers=True,
        end_stream=False,
        fragment_hex=_fragment(rng, 13),
        stream_id=7,
        type="HEADERS",
    )
    add(
        61,
        direction="outbound",
        end_stream=False,
        length=2500,
        stream_id=7,
        type="DATA",
    )
    add(
        62,
        direction="inbound",
        increment=2000,
        level="stream",
        stream_id=7,
        type="WINDOW_UPDATE",
    )
    add(
        63,
        direction="inbound",
        error_code=8,
        stream_id=7,
        type="RST_STREAM",
    )

    add(
        70,
        direction="outbound",
        end_headers=True,
        end_stream=False,
        fragment_hex=_fragment(rng, 15),
        stream_id=9,
        type="HEADERS",
    )
    add(
        71,
        direction="outbound",
        end_stream=True,
        length=1000,
        stream_id=9,
        type="DATA",
    )
    add(
        80,
        debug_hex="",
        direction="inbound",
        error_code=0,
        last_stream_id=9,
        type="GOAWAY",
    )
    add(
        90,
        direction="inbound",
        end_headers=True,
        end_stream=True,
        fragment_hex=_fragment(rng, 9),
        stream_id=9,
        type="HEADERS",
    )

    return {
        "connection": {
            "max_concurrent_streams": 8,
            "max_window": 2147483647,
            "recv_window": 20000,
            "send_window": 3000,
        },
        "events": events,
        "initiator": "client",
        "scenario_id": f"h2-mux-recovery-{seed}",
        "schema_version": 1,
        "streams": streams,
    }


def write_sample_scenario(path: str | Path, seed: int = 20261120) -> dict[str, Any]:
    scenario = build_sample_scenario(seed)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            scenario,
            handle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    return copy.deepcopy(scenario)

