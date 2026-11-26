from __future__ import annotations

import hashlib
import json
from typing import Any

from .canonical import content_digest


def stable_seed(*parts: Any) -> int:
    material = "|".join(
        part if isinstance(part, str) else json.dumps(part, sort_keys=True, separators=(",", ":"))
        for part in parts
    )
    return int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:16], 16)


def quant_digest(quant_config: dict[str, Any]) -> str:
    return content_digest(quant_config)


def schedule_digest(schedule: dict[str, Any]) -> str:
    return content_digest(schedule)


def point_id(tuning_id: str, schedule: dict[str, Any], q_digest: str) -> str:
    raw = {
        "tuning_id": tuning_id,
        "schedule_id": schedule.get("schedule_id"),
        "schedule_revision": schedule.get("schedule_revision", 1),
        "schedule_digest": schedule_digest(schedule),
        "quant_digest": q_digest,
    }
    return "pt-" + hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
