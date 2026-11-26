from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import E_RESOURCE_LIMIT, WorkbenchError


@dataclass
class ResourceDecision:
    workers: int
    batch_size: int
    estimated_token_cache_bytes: int
    max_token_cache_bytes: int
    degradations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workers": self.workers,
            "batch_size": self.batch_size,
            "estimated_token_cache_bytes": self.estimated_token_cache_bytes,
            "max_token_cache_bytes": self.max_token_cache_bytes,
            "degradations": self.degradations,
        }


def resolve_resources(
    config: dict[str, Any], tasks: list[dict[str, Any]]
) -> ResourceDecision:
    limits = config["limits"]
    requested_workers = int(config["workers"])
    requested_batch = int(config["batch_size"])
    max_workers = int(limits["max_workers"])
    max_batch = int(limits["max_batch_sentences"])
    max_token_cache = int(limits["max_token_cache_bytes"])
    bytes_per_token = int(limits["estimated_bytes_per_token"])
    policy = limits.get("resource_policy", "degrade_serial")

    if requested_workers > max_workers:
        raise WorkbenchError(
            E_RESOURCE_LIMIT,
            "requested worker count exceeds hard limit",
            {
                "priority": 1,
                "resource": "workers",
                "requested": requested_workers,
                "limit": max_workers,
                "policy": policy,
            },
        )
    if requested_batch > max_batch:
        raise WorkbenchError(
            E_RESOURCE_LIMIT,
            "requested batch size exceeds hard limit",
            {
                "priority": 2,
                "resource": "batch_size",
                "requested": requested_batch,
                "limit": max_batch,
                "policy": policy,
            },
        )

    estimated_tokens = sum(max(1, len(str(task["raw_text"]))) for task in tasks)
    estimated_bytes = estimated_tokens * bytes_per_token
    workers = min(requested_workers, max(1, len(tasks)))
    degradation: list[dict[str, Any]] = []
    if estimated_bytes > max_token_cache:
        if policy == "fail":
            raise WorkbenchError(
                E_RESOURCE_LIMIT,
                "estimated resident token cache memory exceeds limit",
                {
                    "priority": 3,
                    "resource": "token_cache_memory",
                    "estimated_bytes": estimated_bytes,
                    "limit": max_token_cache,
                    "policy": policy,
                },
            )
        workers = 1
        degradation.append(
            {
                "category": "serial_due_token_cache_memory",
                "estimated_bytes": estimated_bytes,
                "limit": max_token_cache,
                "action": "workers_reduced_to_1",
            }
        )

    return ResourceDecision(
        workers=workers,
        batch_size=requested_batch,
        estimated_token_cache_bytes=estimated_bytes,
        max_token_cache_bytes=max_token_cache,
        degradations=degradation,
    )

