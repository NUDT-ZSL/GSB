from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from poscascade.pipeline import CascadePipeline

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def pipeline(
    temp_dir: Path,
    name: str,
    *,
    dict_name: str = "dict_v1.json",
    labels_name: str = "labels_v1.json",
    config_name: str = "config_default.json",
    crash=None,
    fault_injection: str | None = None,
) -> CascadePipeline:
    run_dir = temp_dir / name
    return CascadePipeline(
        input_path=DATA / "lines.jsonl",
        dict_path=DATA / dict_name,
        labels_path=DATA / labels_name,
        config=DATA / config_name,
        store_path=run_dir / "snapshots.sqlite3",
        output_dir=run_dir / "out",
        crash=crash,
        fault_injection=fault_injection,
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
