"""Generate the deterministic on-disk sample configurations and batch stream."""

from __future__ import annotations

import os
from typing import List

from .config_migration import serialize_v2, serialize_v3, upgrade_to_v3
from .config_reader import read_config
from .data import DriftInjection, generate_stream, write_batches


V1_TEXT = """# DRIFT_MONITOR_CONFIG v1
# name|type|threshold|window|enabled
income_score|numeric|1.20|4|true
session_length|numeric|1.10|4|true
browse_depth|numeric|1.50|4|true
cart_value|numeric|1.30|4|true
device_class|categorical|0.18|4|true
region|categorical|0.20|4|true
membership_tier|categorical|0.22|4|true
"""


def generate_samples(base_dir: str, batch_count: int = 12, sample_count: int = 200) -> List[str]:
    config_dir = os.path.join(base_dir, "samples")
    data_dir = os.path.join(config_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    v1_path = os.path.join(config_dir, "monitor_v1.cfg")
    with open(v1_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(V1_TEXT)

    v1 = read_config(V1_TEXT, config_id="demo-monitor")
    v3 = upgrade_to_v3(v1)
    v2_path = os.path.join(config_dir, "monitor_v2.cfg")
    v3_path = os.path.join(config_dir, "monitor_v3.json")
    with open(v2_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialize_v2(v3))
    with open(v3_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialize_v3(v3))

    # A deliberately strict v3 configuration used by the resource/error demos.
    strict_path = os.path.join(config_dir, "monitor_v3_strict.json")
    strict = serialize_v3(v3).replace("1.2", "0.25").replace("0.18", "0.05")
    with open(strict_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(strict)

    injections = [
        DriftInjection(batch_id=6, mean_shift=18.0, variance_multiplier=2.5),
        DriftInjection(
            batch_id=7,
            mean_shift=18.0,
            variance_multiplier=2.5,
            category_shift=True,
            missing_rate=0.12,
        ),
    ]
    batches = generate_stream(
        batch_count=batch_count,
        sample_count=sample_count,
        master_seed=20260101,
        injections=injections,
    )
    stream_path = os.path.join(data_dir, "batch_stream.json")
    write_batches(batches, stream_path)
    return [v1_path, v2_path, v3_path, strict_path, stream_path]
