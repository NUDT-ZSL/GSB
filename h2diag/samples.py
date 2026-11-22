from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from .canonical import canonical_json, digest_json, digest_without_fields
from .engine import Engine
from .validation import normalize_scenario, validate_patch_document


def _main_scenario() -> dict[str, Any]:
    pseudo_1 = b":method GET\x82:scheme http\x84:path /alpha"
    pseudo_3 = b":method GET\x82:scheme http\x84:path /beta"
    pseudo_5 = b":method POST\x82:scheme http\x84:path /gamma"
    return {
        "format": "h2diag.scenario/v1",
        "scenario_id": "multiplex-window-demo",
        "connection_id": "demo-connection",
        "local_role": "client",
        "connection": {
            "initial_send_window": 600,
            "initial_recv_window": 600,
            "max_send_window": 4096,
            "max_recv_window": 4096,
            "settings": {
                "initial_window_size": 240,
                "max_frame_size": 16384,
                "max_concurrent_streams": 8,
                "enable_push": 0,
            },
        },
        "streams": [
            {
                "stream_id": 1,
                "initiator": "client",
                "initial_send_window": 240,
                "initial_recv_window": 240,
                "max_send_window": 4096,
                "max_recv_window": 4096,
                "weight": 32,
                "expected_final_state": "closed",
            },
            {
                "stream_id": 3,
                "initiator": "client",
                "initial_send_window": 240,
                "initial_recv_window": 240,
                "max_send_window": 4096,
                "max_recv_window": 4096,
                "weight": 16,
                "expected_final_state": "closed",
            },
            {
                "stream_id": 5,
                "initiator": "client",
                "initial_send_window": 240,
                "initial_recv_window": 240,
                "max_send_window": 4096,
                "max_recv_window": 4096,
                "weight": 8,
                "expected_final_state": "closed",
            },
        ],
        "events": [
            {
                "seq": 1,
                "clock_ms": 0,
                "type": "SETTINGS",
                "direction": "recv",
                "settings": {"initial_window_size": 240, "max_frame_size": 16384},
            },
            {"seq": 2, "clock_ms": 1, "type": "SETTINGS_ACK", "direction": "send"},
            {
                "seq": 3,
                "clock_ms": 2,
                "type": "HEADERS",
                "direction": "send",
                "stream_id": 1,
                "end_headers": False,
                "end_stream": False,
                "fragment_hex": pseudo_1[:11].hex(),
            },
            {
                "seq": 4,
                "clock_ms": 3,
                "type": "CONTINUATION",
                "direction": "send",
                "stream_id": 1,
                "end_headers": True,
                "fragment_hex": pseudo_1[11:].hex(),
            },
            {
                "seq": 5,
                "clock_ms": 4,
                "type": "HEADERS",
                "direction": "send",
                "stream_id": 3,
                "end_headers": True,
                "end_stream": False,
                "fragment_hex": pseudo_3.hex(),
            },
            {
                "seq": 6,
                "clock_ms": 5,
                "type": "HEADERS",
                "direction": "send",
                "stream_id": 5,
                "end_headers": True,
                "end_stream": False,
                "fragment_hex": pseudo_5.hex(),
            },
            {
                "seq": 7,
                "clock_ms": 6,
                "type": "DATA",
                "direction": "send",
                "stream_id": 3,
                "data_hex": ("11" * 32),
            },
            {
                "seq": 8,
                "clock_ms": 7,
                "type": "WINDOW_UPDATE",
                "direction": "recv",
                "level": "connection",
                "window_increment": 20,
            },
            {
                "seq": 9,
                "clock_ms": 8,
                "type": "RST_STREAM",
                "direction": "recv",
                "stream_id": 3,
                "error_code": 8,
            },
            {
                "seq": 10,
                "clock_ms": 9,
                "type": "DATA",
                "direction": "send",
                "stream_id": 1,
                "data_hex": ("22" * 64),
            },
            {
                "seq": 11,
                "clock_ms": 10,
                "type": "DATA",
                "direction": "send",
                "stream_id": 5,
                "data_hex": ("33" * 80),
                "end_stream": True,
            },
            {
                "seq": 12,
                "clock_ms": 11,
                "type": "WINDOW_UPDATE",
                "direction": "recv",
                "level": "stream",
                "stream_id": 1,
                "window_increment": 64,
            },
            {
                "seq": 13,
                "clock_ms": 12,
                "type": "DATA",
                "direction": "recv",
                "stream_id": 1,
                "data_hex": ("44" * 24),
                "end_stream": True,
            },
            {
                "seq": 14,
                "clock_ms": 13,
                "type": "DATA",
                "direction": "recv",
                "stream_id": 5,
                "data_hex": ("55" * 16),
                "end_stream": True,
            },
            {
                "seq": 15,
                "clock_ms": 14,
                "type": "DATA",
                "direction": "send",
                "stream_id": 1,
                "data_hex": ("66" * 16),
                "end_stream": True,
            },
            {
                "seq": 16,
                "clock_ms": 15,
                "type": "PING",
                "direction": "send",
                "payload_hex": "0102030405060708",
            },
            {
                "seq": 17,
                "clock_ms": 16,
                "type": "PING_ACK",
                "direction": "recv",
                "payload_hex": "0102030405060708",
            },
            {
                "seq": 18,
                "clock_ms": 17,
                "type": "GOAWAY",
                "direction": "send",
                "last_stream_id": 5,
            },
        ],
    }


def _end_stream_scenario() -> dict[str, Any]:
    return {
        "format": "h2diag.scenario/v1",
        "scenario_id": "end-marker-demo",
        "connection_id": "end-marker-connection",
        "local_role": "client",
        "connection": {
            "initial_send_window": 600,
            "initial_recv_window": 600,
            "max_send_window": 4096,
            "max_recv_window": 4096,
            "settings": {
                "initial_window_size": 200,
                "max_frame_size": 16384,
            },
        },
        "streams": [
            {
                "stream_id": 1,
                "initiator": "client",
                "initial_send_window": 200,
                "initial_recv_window": 200,
                "max_send_window": 4096,
                "max_recv_window": 4096,
                "weight": 24,
                "expected_final_state": "open",
            },
            {
                "stream_id": 3,
                "initiator": "client",
                "initial_send_window": 200,
                "initial_recv_window": 200,
                "max_send_window": 4096,
                "max_recv_window": 4096,
                "weight": 12,
                "expected_final_state": "half-closed-local",
            },
        ],
        "events": [
            {
                "seq": 1,
                "clock_ms": 100,
                "type": "HEADERS",
                "direction": "send",
                "stream_id": 1,
                "end_headers": True,
                "fragment_hex": b":path /one".hex(),
            },
            {
                "seq": 2,
                "clock_ms": 101,
                "type": "HEADERS",
                "direction": "send",
                "stream_id": 3,
                "end_headers": True,
                "fragment_hex": b":path /three".hex(),
            },
            {
                "seq": 3,
                "clock_ms": 102,
                "type": "DATA",
                "direction": "send",
                "stream_id": 3,
                "length": 10,
                "end_stream": True,
            },
        ],
    }


def _seal_patch(body: dict[str, Any]) -> dict[str, Any]:
    digest = digest_without_fields(body, ["content_digest"])
    body["content_digest"] = digest
    return body


def _main_patch() -> dict[str, Any]:
    return _seal_patch(
        {
            "format": "h2diag.patch/v1",
            "patch_id": "weights-and-settings-v2",
            "connection_id": "demo-connection",
            "base_version": 1,
            "target_version": 2,
            "mode": "incremental",
            "clock_ms": 11,
            "operations": [
                {"op": "set_weight", "stream_id": 1, "weight": 48},
                {
                    "op": "set_settings",
                    "direction": "recv",
                    "settings": {"initial_window_size": 300},
                },
            ],
        }
    )


def _end_stream_patch() -> dict[str, Any]:
    return _seal_patch(
        {
            "format": "h2diag.patch/v1",
            "patch_id": "end-markers-v2",
            "connection_id": "end-marker-connection",
            "base_version": 1,
            "target_version": 2,
            "mode": "incremental",
            "clock_ms": 102,
            "operations": [
                {"op": "set_stream_end", "stream_id": 1, "side": "local"},
                {"op": "set_stream_end", "stream_id": 3, "side": "remote"},
                {
                    "op": "set_expected_final_state",
                    "stream_id": 1,
                    "expected_final_state": "half-closed-local",
                },
                {
                    "op": "set_expected_final_state",
                    "stream_id": 3,
                    "expected_final_state": "closed",
                },
                {
                    "op": "window_update",
                    "level": "stream",
                    "stream_id": 1,
                    "direction": "recv",
                    "increment": 20,
                },
            ],
        }
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(canonical_json(event) + "\n")


def generate_samples(out_dir: str | Path) -> dict[str, Any]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    main_scenario = normalize_scenario(_main_scenario(), preflight=True)
    end_scenario = normalize_scenario(_end_stream_scenario(), preflight=True)
    main_patch = _main_patch()
    end_patch = _end_stream_patch()
    validate_patch_document(main_patch)
    validate_patch_document(end_patch)

    # The generator fails if the deterministic samples no longer satisfy their
    # declared final states or replay semantics.
    Engine(main_scenario).run_all(check_expected=True)
    Engine(end_scenario).run_all(check_expected=True)

    files: dict[str, Path] = {
        "scenario": root / "scenario.json",
        "end_stream_scenario": root / "end_stream_scenario.json",
        "patch_settings_weight": root / "patches" / "settings-weight-v2.json",
        "patch_end_stream": root / "patches" / "end-stream-v2.json",
        "frames_main": root / "frames" / "main.jsonl",
        "frames_end_stream": root / "frames" / "end_stream.jsonl",
    }
    _write_json(files["scenario"], main_scenario)
    _write_json(files["end_stream_scenario"], end_scenario)
    _write_json(files["patch_settings_weight"], main_patch)
    _write_json(files["patch_end_stream"], end_patch)
    _write_jsonl(files["frames_main"], main_scenario["events"])
    _write_jsonl(files["frames_end_stream"], end_scenario["events"])

    entries: list[dict[str, Any]] = []
    for name, path in sorted(files.items()):
        data = path.read_bytes()
        entries.append(
            {
                "name": name,
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "size": len(data),
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "format": "h2diag.samples/v1",
        "generator": "h2diag.samples.generate_samples",
        "scenario_digest": digest_json(main_scenario),
        "end_stream_scenario_digest": digest_json(end_scenario),
        "patch_settings_weight_digest": main_patch["content_digest"],
        "patch_end_stream_digest": end_patch["content_digest"],
        "files": entries,
    }
    manifest["manifest_digest"] = digest_json(manifest)
    _write_json(root / "manifest.json", manifest)
    return manifest
