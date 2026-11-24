from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .errors import HapticError, canonical_json

DEFAULT_SEED = 20260214


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def generate_documents(seed: int = DEFAULT_SEED) -> tuple[dict, dict, dict]:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise HapticError("E_CONFIG", "seed must be an integer", {"seed": seed})
    rng = random.Random(seed)
    profiles = [
        {
            "id": "dev_glove_left",
            "sample_rate_hz": 1000,
            "frame_duration_ms": 10,
            "strength_min": 0.0,
            "strength_max": 255.0,
            "quantization_levels": 256,
            "max_commands_per_frame": 2,
        },
        {
            "id": "dev_glove_right",
            "sample_rate_hz": 800,
            "frame_duration_ms": 10,
            "strength_min": 0.0,
            "strength_max": 100.0,
            "quantization_levels": 16,
            "max_commands_per_frame": 2,
        },
        {
            "id": "dev_vest_core",
            "sample_rate_hz": 500,
            "frame_duration_ms": 20,
            "strength_min": 0.0,
            "strength_max": 1.0,
            "quantization_levels": 8,
            "max_commands_per_frame": 2,
        },
        {
            "id": "dev_wrist_band",
            "sample_rate_hz": 2000,
            "frame_duration_ms": 5,
            "strength_min": 0.0,
            "strength_max": 127.0,
            "quantization_levels": 32,
            "max_commands_per_frame": 3,
        },
    ]
    for profile in profiles:
        profile["strength_max"] = round(
            profile["strength_max"] * rng.uniform(0.95, 1.05), 6
        )

    devices_doc = {
        "seed": seed,
        "devices": sorted(profiles, key=lambda item: item["id"]),
    }

    marker_starts = [20, 60, 100]
    events: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    device_ids = [profile["id"] for profile in sorted(profiles, key=lambda item: item["id"])]
    for marker_index, start_ms in enumerate(marker_starts):
        marker = f"sync_{marker_index}"
        anchors.append(
            {
                "id": f"anchor_{marker_index}",
                "marker": marker,
                "reference_device": "dev_glove_left",
                "devices": device_ids,
                "tolerance_ms": 0.01,
            }
        )
        for profile in sorted(profiles, key=lambda item: item["id"]):
            device_id = profile["id"]
            duration_ms = 16 + 2 * rng.randrange(0, 3)
            if marker_index == 1:
                step_ms = 2 if profile["frame_duration_ms"] >= 10 else 1
            else:
                step_ms = max(2, profile["frame_duration_ms"] // 3)
            points: list[dict[str, float]] = []
            offset = 0
            high = True
            while offset <= duration_ms:
                if marker_index == 1:
                    ratio = 1.0 if high else 0.0
                else:
                    ratio = 0.2 + 0.65 * ((offset / max(duration_ms, 1)) ** (marker_index + 1))
                strength = profile["strength_min"] + ratio * (
                    profile["strength_max"] - profile["strength_min"]
                )
                points.append({"offset_ms": float(offset), "strength": round(strength, 6)})
                offset += step_ms
                high = not high
            if points[-1]["offset_ms"] != float(duration_ms):
                ratio = 0.25 if marker_index == 0 else 0.75
                points.append(
                    {
                        "offset_ms": float(duration_ms),
                        "strength": round(
                            profile["strength_min"]
                            + ratio * (profile["strength_max"] - profile["strength_min"]),
                            6,
                        ),
                    }
                )
            events.append(
                {
                    "id": f"{device_id}_{marker}",
                    "type": ["impact", "texture", "pulse"][marker_index],
                    "start_ms": start_ms,
                    "duration_ms": duration_ms,
                    "target_device": device_id,
                    "anchor": marker,
                    "envelope": points,
                }
            )
    events.sort(key=lambda item: (item["start_ms"], item["target_device"], item["id"]))
    events_doc = {"seed": seed, "events": events, "anchors": anchors}

    config_doc = {
        "schema_version": 1,
        "seed": seed,
        "global_concurrency": 3,
        "inflight_command_limit": 4,
        "alignment_tolerance_ms": 0.01,
        "state_dir": "state",
        "export_dir": "export",
    }
    return devices_doc, events_doc, config_doc


def generate_workspace(
    workspace: Path | str,
    seed: int = DEFAULT_SEED,
    force: bool = False,
) -> dict[str, Path]:
    workspace = Path(workspace).resolve()
    devices_doc, events_doc, config_doc = generate_documents(seed)
    targets = {
        "devices": workspace / "devices.json",
        "events": workspace / "events.json",
        "config": workspace / "config.json",
    }
    existing = [path for path in targets.values() if path.exists()]
    if existing and not force:
        raise HapticError(
            "E_INPUT",
            "sample files already exist; use force to overwrite",
            {"existing": [str(path) for path in existing]},
        )
    _write_json(targets["devices"], devices_doc)
    _write_json(targets["events"], events_doc)
    _write_json(targets["config"], config_doc)
    return targets


def documents_digest(devices_doc: dict, events_doc: dict) -> str:
    import hashlib

    payload = {
        "devices": sorted(devices_doc.get("devices", []), key=lambda item: item["id"]),
        "events": sorted(
            events_doc.get("events", []),
            key=lambda item: (item["start_ms"], item["target_device"], item["id"]),
        ),
        "anchors": sorted(events_doc.get("anchors", []), key=lambda item: item["id"]),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
