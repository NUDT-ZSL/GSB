from __future__ import annotations

import os
from pathlib import Path

from .errors import ResourceLimit


def resident_memory_bytes() -> int:
    status = Path("/proc/self/status")
    if not status.is_file():
        return 0
    try:
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                value = line.split()[1]
                return int(value) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


class ResourceGuard:
    def __init__(self, memory_limit_mb: int, *, worker_count: int) -> None:
        self.memory_limit_bytes = int(memory_limit_mb) * 1024 * 1024
        self.worker_count = int(worker_count)
        self.stage_estimates: dict[str, int] = {}

    def check(self, stage: str, estimated_payload_bytes: int = 0) -> None:
        rss = resident_memory_bytes()
        projected = rss + max(0, int(estimated_payload_bytes))
        self.stage_estimates[stage] = projected
        if self.memory_limit_bytes <= 0:
            raise ResourceLimit(
                "memory_limit_mb must be positive", detail={"stage": stage}
            )
        if projected > self.memory_limit_bytes:
            raise ResourceLimit(
                "resident memory limit exceeded",
                detail={
                    "stage": stage,
                    "resident_bytes": rss,
                    "estimated_payload_bytes": int(estimated_payload_bytes),
                    "projected_bytes": projected,
                    "limit_bytes": self.memory_limit_bytes,
                },
            )

    def to_dict(self) -> dict[str, int]:
        return {
            "memory_limit_bytes": self.memory_limit_bytes,
            "worker_count": self.worker_count,
            "final_resident_bytes": resident_memory_bytes(),
            **{
                f"{stage}_projected_bytes": value
                for stage, value in sorted(self.stage_estimates.items())
            },
        }


def cpu_count() -> int:
    return os.cpu_count() or 1

