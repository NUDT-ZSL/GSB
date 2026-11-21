"""Worker-process entry points.

A process pool is created with the ``spawn`` start method (available on Linux,
macOS and Windows). The read-only :class:`~recall_fusion.data.DataStore` is
installed once through the pool initializer, so every task receives only small
serializable arguments.
"""

from __future__ import annotations

import traceback
from typing import Any

from .config import CHANNEL_ORDER, Config
from .data import DataStore, Query
from .errors import E_WORKER_FAILURE
from .fusion import run_fusion
from .recall import run_channel

_WORKER_STORE: DataStore | None = None
_WORKER_CONFIG: Config | None = None


def init_worker(store: DataStore, config: Config) -> None:
    global _WORKER_STORE, _WORKER_CONFIG
    _WORKER_STORE = store
    _WORKER_CONFIG = config


def _failure(query_id: str, stage: str, exc: BaseException) -> dict[str, Any]:
    return {
        "query_id": query_id,
        "ok": False,
        "status": "failed",
        "error": {
            "code": getattr(exc, "code", E_WORKER_FAILURE),
            "message": str(exc),
            "stage": stage,
            "field": getattr(exc, "field", ""),
            "traceback": traceback.format_exc(limit=8),
        },
    }


def process_query(task: tuple[Query, dict[str, Any]]) -> dict[str, Any]:
    """Execute all four channels plus fusion for one admitted query."""
    query, plan_dict = task
    try:
        assert _WORKER_STORE is not None and _WORKER_CONFIG is not None
        store, config = _WORKER_STORE, _WORKER_CONFIG
        effective_limits = plan_dict["effective_limits"]
        channel_outputs = {}
        for channel in CHANNEL_ORDER:
            if not config.channels[channel].enabled:
                # Disabled channels are still recorded explicitly as empty.
                from .recall import ChannelOutput

                channel_outputs[channel] = ChannelOutput(
                    channel=channel,
                    requested_limit=0,
                    candidates=[],
                    truncated=False,
                    matched_total=0,
                    empty_reason="disabled",
                )
            else:
                channel_outputs[channel] = run_channel(
                    channel, store, query, int(effective_limits[channel])
                )

        fused, comparisons_used, stage_mode = run_fusion(
            channel_outputs, config, bool(plan_dict["two_stage"])
        )
        fused = fused[: query.top_k]
        return {
            "query_id": query.query_id,
            "ok": True,
            "status": plan_dict["status"],
            "plan": plan_dict,
            "filters": dict(query.filters),
            "top_k": query.top_k,
            "stage_mode_actual": stage_mode,
            "comparisons_used": comparisons_used,
            "channel_outputs": {
                channel: output.to_dict()
                for channel, output in channel_outputs.items()
            },
            "fused": [candidate.to_dict() for candidate in fused],
            "fused_total": len(fused),
        }
    except Exception as exc:  # structured failure, never kills the batch
        return _failure(query.query_id, "process_query", exc)
