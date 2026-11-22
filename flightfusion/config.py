"""Workspace loading and local sample location helpers."""

from __future__ import annotations

import json
from pathlib import Path

from .formats import parse_description_file
from .generator import write_samples
from .models import FlightDataset, FlightState, FusionConfig
from .serialization import content_hash


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    cwd_data = Path.cwd() / "data"
    if cwd_data.exists():
        return cwd_data
    return PROJECT_ROOT / "data"


def load_default_workspace(data_dir: str | Path | None = None) -> FlightState:
    base = Path(data_dir) if data_dir is not None else default_data_dir()
    if not (base / "flight_imu.json").exists():
        sample_root = base.parent if base.name == "data" else base
        write_samples(sample_root)
        base = sample_root / "data" if base.name != "data" else base

    dataset = FlightDataset.from_dict(
        json.loads((base / "flight_imu.json").read_text(encoding="utf-8"))
    )
    descriptions_by_format = {}
    views = {}
    files = {
        1: base / "formats" / "sensors.v1.txt",
        2: base / "formats" / "sensors.v2.csv",
        3: base / "formats" / "sensors.v3.json",
    }
    for version, path in files.items():
        parsed_version, descriptions = parse_description_file(path)
        if parsed_version != version:
            raise RuntimeError(f"{path} dispatched as v{parsed_version}, expected v{version}")
        descriptions_by_format[version] = descriptions
        views[f"v{version}"] = path.read_text(encoding="utf-8")

    active_formats = {"gyro": 1, "accel": 2, "mag": 3}
    descriptions = {
        sensor_id: descriptions_by_format[version][sensor_id]
        for sensor_id, version in active_formats.items()
    }
    config_raw = json.loads(
        (base / "fusion_config.json").read_text(encoding="utf-8")
    )
    config = FusionConfig.from_dict(config_raw)
    state = FlightState(
        dataset=dataset,
        descriptions=descriptions,
        fusion_config=config,
        description_views=views,
        active_formats=active_formats,
        snapshot_version=1,
        snapshot_checksum="",
    )
    return FlightState(
        dataset=state.dataset,
        descriptions=state.descriptions,
        fusion_config=state.fusion_config,
        description_views=state.description_views,
        active_formats=state.active_formats,
        snapshot_version=1,
        snapshot_checksum=content_hash(state.snapshot_payload()),
    )

