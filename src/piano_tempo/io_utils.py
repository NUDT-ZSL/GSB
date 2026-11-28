"""配置读取与 JSON 结果落盘辅助。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


def load_config(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class NumpyJSONEncoder(json.JSONEncoder):
    def default(self, value: Any) -> Any:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (np.floating, np.integer)):
            return value.item()
        if isinstance(value, (np.bool_,)):
            return bool(value)
        return super().default(value)


def write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, cls=NumpyJSONEncoder),
        encoding="utf-8",
    )
    return target
