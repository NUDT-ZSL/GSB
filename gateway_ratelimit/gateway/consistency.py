"""Cross-path consistency comparison and deterministic boundary attribution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import E_OK, E_LIMIT
from .limiters import PathDecision
from .models import (
    LIMIT_PATHS,
    PATH_TOKEN_BUCKET,
    PATH_WINDOW_COUNTER,
    PATH_WINDOW_LOG,
)

REFILL_TRUNCATION = "refill_truncation"
WINDOW_ALIGNMENT = "window_alignment"
LOG_RETENTION_SPAN = "log_retention_span"


@dataclass(frozen=True)
class Divergence:
    seq: int
    ts_ms: int
    client_id: str
    route: str
    decisions: dict[str, str]
    remaining: dict[str, int]
    boundary: str
    attribution: str


def classify_boundary(
    decisions: dict[str, PathDecision], route_window_ms: int, ts_ms: int
) -> tuple[str, str]:
    """Pure function: same inputs always produce the same attribution."""
    allows = {path: decision.allow for path, decision in decisions.items()}
    if allows.get(PATH_TOKEN_BUCKET) != allows.get(PATH_WINDOW_LOG):
        bucket = decisions[PATH_TOKEN_BUCKET]
        return (
            REFILL_TRUNCATION,
            "token bucket floors elapsed*rate/1000 toward zero while the exact "
            "log counts whole-window requests; fractional refill lost at the "
            f"boundary ({bucket.detail})",
        )
    if allows.get(PATH_WINDOW_COUNTER) != allows.get(PATH_WINDOW_LOG):
        counter = decisions[PATH_WINDOW_COUNTER]
        return (
            WINDOW_ALIGNMENT,
            "epoch-aligned weighted counter approximates the exact window: "
            f"offset={ts_ms % route_window_ms} drives floor-weighted previous "
            f"bucket ({counter.detail})",
        )
    return (
        LOG_RETENTION_SPAN,
        "boundary timestamps age out of the exact log (t-w, t] while bucket "
        "refill keeps fractional capacity; retention span differs",
    )


def compare_traces(
    trace_rows: list[dict[str, Any]], routes_by_name: dict[str, Any]
) -> dict[str, Any]:
    """Compare per-request path decisions; only authenticated rows are compared."""
    divergences: list[Divergence] = []
    compared = 0
    for row in trace_rows:
        if row["auth"]["code"] != E_OK:
            continue
        compared += 1
        path_rows: dict[str, dict[str, Any]] = row["paths"]
        decisions = {path: path_rows[path]["allow"] for path in LIMIT_PATHS}
        if len(set(decisions.values())) == 1:
            continue
        path_decisions = {
            path: PathDecision(
                path=path,
                allow=path_rows[path]["allow"],
                remaining=path_rows[path]["remaining"],
                reason=path_rows[path]["reason"],
                detail=path_rows[path]["detail"],
            )
            for path in LIMIT_PATHS
        }
        route = routes_by_name[row["route"]]
        boundary, attribution = classify_boundary(path_decisions, route.window_ms, row["ts_ms"])
        divergences.append(
            Divergence(
                seq=row["seq"],
                ts_ms=row["ts_ms"],
                client_id=row["client_id"],
                route=row["route"],
                decisions={path: (E_OK if allow else E_LIMIT) for path, allow in decisions.items()},
                remaining={path: path_rows[path]["remaining"] for path in LIMIT_PATHS},
                boundary=boundary,
                attribution=attribution,
            )
        )

    consistent = not divergences
    return {
        "consistent": consistent,
        "compared_requests": compared,
        "divergence_count": len(divergences),
        "divergences": [
            {
                "seq": item.seq,
                "ts_ms": item.ts_ms,
                "client_id": item.client_id,
                "route": item.route,
                "decisions": item.decisions,
                "remaining": item.remaining,
                "boundary": item.boundary,
                "attribution": item.attribution,
            }
            for # deterministic order: request sequence order, paths in fixed order
            item in sorted(divergences, key=lambda d: d.seq)
        ],
    }
