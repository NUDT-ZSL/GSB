from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def sample_documents() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))
    config = json.loads((SAMPLES / "config.json").read_text(encoding="utf-8"))
    arrivals = json.loads((SAMPLES / "arrivals.json").read_text(encoding="utf-8"))
    return manifest, config, arrivals


def fresh_sample_documents() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest, config, arrivals = sample_documents()
    return copy.deepcopy(manifest), copy.deepcopy(config), copy.deepcopy(arrivals)


def write_sample_directory(
    directory: Path,
    config: dict[str, Any],
    arrivals: dict[str, Any],
    manifest: dict[str, Any] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if manifest is None:
        manifest, _, _ = fresh_sample_documents()
    config_bytes = (
        json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    arrivals_bytes = (
        json.dumps(arrivals, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    (directory / "config.json").write_bytes(config_bytes)
    (directory / "arrivals.json").write_bytes(arrivals_bytes)
    manifest["files"] = {"config": "config.json", "arrivals": "arrivals.json"}
    manifest["sha256"] = {
        "config": hashlib.sha256(config_bytes).hexdigest(),
        "arrivals": hashlib.sha256(arrivals_bytes).hexdigest(),
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path

