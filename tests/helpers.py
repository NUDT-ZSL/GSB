from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dialogue_risk.dataio import load_inputs
from dialogue_risk.util import canonical_json

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load_fixture() -> dict[str, Any]:
    return load_inputs(DATA)


def canonical(value: Any) -> str:
    return canonical_json(value)


def single_message_conversation(text: str, *, weight: float = 1.0) -> dict[str, Any]:
    return {
        "conv_id": "test-conv",
        "locale": "zh-CN",
        "annotator_group": "T",
        "messages": [
            {
                "conv_id": "test-conv",
                "msg_no": 1,
                "speaker": "customer",
                "text": text,
                "locale": "zh-CN",
                "annotator_group": "T",
                "weight": weight,
            }
        ],
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

