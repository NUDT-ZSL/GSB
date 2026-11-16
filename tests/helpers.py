from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sparse_spmm.runner import prepare_case


ROOT = Path(__file__).resolve().parents[1]


def load_config(name: str) -> dict[str, Any]:
    with (ROOT / "config" / name).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def test_case() -> Any:
    return prepare_case(load_config("sample_test.json"), load_config("strategies.json"))

