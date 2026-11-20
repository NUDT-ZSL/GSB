"""Controlled, opt-in fault injection loaded from a local manifest."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import digest_value
from .errors import EngineError


@dataclass(frozen=True)
class Injection:
    id: str
    stage: str
    kind: str
    enabled: bool
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "kind": self.kind,
            "enabled": self.enabled,
            "parameters": self.parameters,
        }


class InjectionController:
    def __init__(self, items: list[Injection]) -> None:
        self.items = sorted(items, key=lambda item: item.id)

    @classmethod
    def empty(cls) -> "InjectionController":
        return cls([])

    @classmethod
    def load(cls, path: str | Path | None) -> "InjectionController":
        if path is None:
            return cls.empty()
        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EngineError(
                "E_IO",
                "unable to load injection manifest",
                stage="parse",
                details={"path": str(source), "reason": str(exc)},
            ) from exc
        raw_items = payload.get("injections") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            raise EngineError(
                "E_BAD_MANIFEST",
                "injection manifest must contain an injections list",
                stage="parse",
                details={"path": str(source)},
            )
        items: list[Injection] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise EngineError(
                    "E_BAD_MANIFEST",
                    "injection entry must be an object",
                    stage="parse",
                    details={"path": str(source), "index": index},
                )
            required = {"id", "stage", "kind", "enabled"}
            missing = sorted(required - raw.keys())
            if missing:
                raise EngineError(
                    "E_BAD_MANIFEST",
                    "injection entry is missing required fields",
                    stage="parse",
                    details={"path": str(source), "index": index, "missing": missing},
                )
            if not isinstance(raw["enabled"], bool):
                raise EngineError(
                    "E_BAD_MANIFEST",
                    "injection enabled must be boolean",
                    stage="parse",
                    details={"path": str(source), "index": index},
                )
            parameters = raw.get("parameters", {})
            if not isinstance(parameters, dict):
                raise EngineError(
                    "E_BAD_MANIFEST",
                    "injection parameters must be an object",
                    stage="parse",
                    details={"path": str(source), "index": index},
                )
            items.append(
                Injection(
                    id=str(raw["id"]),
                    stage=str(raw["stage"]),
                    kind=str(raw["kind"]),
                    enabled=raw["enabled"],
                    parameters=parameters,
                )
            )
        if len({item.id for item in items}) != len(items):
            raise EngineError(
                "E_BAD_MANIFEST",
                "injection ids must be unique",
                stage="parse",
                details={"path": str(source)},
            )
        return cls(items)

    def find(self, stage: str, kind: str) -> Injection | None:
        for item in self.items:
            if item.enabled and item.stage == stage and item.kind == kind:
                return item
        return None

    def enabled(self) -> list[Injection]:
        return [item for item in self.items if item.enabled]

    def digest(self) -> str:
        return digest_value([item.to_dict() for item in self.enabled()])
