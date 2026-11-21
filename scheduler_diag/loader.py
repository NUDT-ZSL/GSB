from __future__ import annotations

import json
from pathlib import Path

from .canonical import parse_json_text
from .errors import BusinessError
from .models import Bundle, parse_bundle


REQUIRED_FILES = ("nodes.json", "pods.json", "config.json", "injections.json")


def load_bundle(directory: str | Path) -> Bundle:
    root = Path(directory)
    if not root.is_dir():
        raise BusinessError(
            "E_BAD_BUNDLE",
            "bundle directory does not exist",
            {"directory": str(directory)},
        )
    values: list[object] = []
    for filename in REQUIRED_FILES:
        path = root / filename
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BusinessError(
                "E_BAD_BUNDLE",
                f"cannot read {filename}",
                {"file": str(path), "reason": str(exc)},
            ) from exc
        try:
            values.append(parse_json_text(text))
        except (json.JSONDecodeError, ValueError) as exc:
            raise BusinessError(
                "E_BAD_BUNDLE",
                f"invalid JSON in {filename}",
                {"file": str(path), "reason": str(exc)},
            ) from exc
    return parse_bundle(values[0], values[1], values[2], values[3])


def bundle_from_payload(payload: dict[str, object]) -> Bundle:
    return parse_bundle(
        payload["nodes"],
        payload["pods"],
        payload["config"],
        payload["injections"],
    )
