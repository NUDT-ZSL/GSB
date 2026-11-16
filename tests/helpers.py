from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def write_yaml(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def copy_sample_config_with_overrides(
    destination: Path,
    *,
    optimizer: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> Path:
    source = yaml.safe_load((ROOT / "configs" / "experiment.yaml").read_text(encoding="utf-8"))
    source["data"]["spec_path"] = str((ROOT / "configs" / "data.yaml").resolve())
    if optimizer:
        source["optimizer"].update(optimizer)
    if runtime:
        source["runtime"].update(runtime)
    return write_yaml(destination, source)


def result_core(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "final_coefficients": result["final_coefficients"],
        "loss_trajectory": result["loss_trajectory"],
    }


def find_latest_snapshot_round(state_dir: Path) -> int:
    latest = 0
    for path in (state_dir / "snapshots").glob("*/*.json"):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            latest = max(latest, int(payload.get("round", 0)))
        except Exception:
            continue
    return latest
