from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

import numpy as np

from .config import CLASS_IDS, SegmentationConfig
from .model import FusionResult, TileMeta, TileResult
from .util import sha256_json


@dataclass(frozen=True)
class RegionPlan:
    row: int
    col: int
    bounds: tuple[int, int, int, int]
    participants: tuple[TileMeta, ...]

    @property
    def id(self) -> str:
        return f"r{self.row}_c{self.col}"


def _intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def build_region_plans(metas: list[TileMeta]) -> dict[str, RegionPlan]:
    """One cacheable fusion region per tile, using that tile's bounds.

    Regions may overlap, but every completed region is computed from the same
    participant set and therefore contains identical values in overlap pixels.
    Global assembly writes stale regions first and converged regions last.
    """
    ordered = sorted(metas, key=lambda m: m.stable_key)
    plans: dict[str, RegionPlan] = {}
    for meta in ordered:
        participants = tuple(m for m in ordered if _intersects(meta.bounds, m.bounds))
        plan = RegionPlan(meta.row, meta.col, meta.bounds, participants)
        plans[plan.id] = plan
    return plans


def region_dependency(
    plan: RegionPlan,
    tile_keys: dict[str, str],
    strategy: str,
) -> str:
    participants = []
    for meta in plan.participants:
        participants.append(
            {
                "row": meta.row,
                "col": meta.col,
                "cache_key": tile_keys[meta.id],
                "adjacency_digest": meta.adjacency_digest,
            }
        )
    return sha256_json(
        {
            "schema": 1,
            "region": plan.id,
            "bounds": list(plan.bounds),
            "strategy": strategy,
            "participants": participants,
        }
    )


def _distance_weight(height: int, width: int) -> np.ndarray:
    ys = np.minimum(np.arange(height, dtype=np.int64) + 1, height - np.arange(height, dtype=np.int64))
    xs = np.minimum(np.arange(width, dtype=np.int64) + 1, width - np.arange(width, dtype=np.int64))
    return np.minimum(ys[:, None], xs[None, :])


def fuse_region(
    plan: RegionPlan,
    results: dict[str, TileResult],
    config: SegmentationConfig,
    dep_fingerprint: str,
) -> FusionResult:
    x0, y0, x1, y1 = plan.bounds
    height, width = y1 - y0, x1 - x0
    votes = np.zeros((5, height, width), dtype=np.int64)
    candidates = sorted(plan.participants, key=lambda m: m.stable_key)
    for meta in candidates:
        result = results[meta.id]
        lx0 = max(x0, meta.bounds[0])
        ly0 = max(y0, meta.bounds[1])
        lx1 = min(x1, meta.bounds[2])
        ly1 = min(y1, meta.bounds[3])
        if lx0 >= lx1 or ly0 >= ly1:
            continue
        tile_classes = result.classes[
            ly0 - meta.bounds[1] : ly1 - meta.bounds[1],
            lx0 - meta.bounds[0] : lx1 - meta.bounds[0],
        ]
        out = votes[:, ly0 - y0 : ly1 - y0, lx0 - x0 : lx1 - x0]
        if config.fusion_strategy == "distance_weighted_vote":
            full_weight = _distance_weight(meta.height, meta.width)
            weight = full_weight[
                ly0 - meta.bounds[1] : ly1 - meta.bounds[1],
                lx0 - meta.bounds[0] : lx1 - meta.bounds[0],
            ]
        else:
            weight = np.ones(tile_classes.shape, dtype=np.int64)
        for class_id in CLASS_IDS:
            out[class_id] += np.where(tile_classes == class_id, weight, 0)

    best_votes = votes.max(axis=0)
    winners = np.zeros((height, width), dtype=np.uint8)
    winner_rank = np.full((height, width), np.iinfo(np.int64).max, dtype=np.int64)
    for rank, meta in enumerate(candidates):
        result = results[meta.id]
        lx0 = max(x0, meta.bounds[0])
        ly0 = max(y0, meta.bounds[1])
        lx1 = min(x1, meta.bounds[2])
        ly1 = min(y1, meta.bounds[3])
        sub_classes = result.classes[
            ly0 - meta.bounds[1] : ly1 - meta.bounds[1],
            lx0 - meta.bounds[0] : lx1 - meta.bounds[0],
        ]
        out_view = votes[:, ly0 - y0 : ly1 - y0, lx0 - x0 : lx1 - x0]
        best_view = best_votes[ly0 - y0 : ly1 - y0, lx0 - x0 : lx1 - x0]
        winner_view = winners[ly0 - y0 : ly1 - y0, lx0 - x0 : lx1 - x0]
        rank_view = winner_rank[ly0 - y0 : ly1 - y0, lx0 - x0 : lx1 - x0]
        # A class tied with the maximum vote is resolved by the earliest tile
        # in lexicographic (row, col) order that voted for that class.
        voted_for_winner = out_view[sub_classes, np.arange(sub_classes.shape[0])[:, None], np.arange(sub_classes.shape[1])[None, :]]
        take = (voted_for_winner == best_view) & (rank_view > rank)
        winner_view[take] = sub_classes[take]
        rank_view[take] = rank

    participant_keys = tuple((m.id, results[m.id].cache_key) for m in candidates)
    return FusionResult(
        row=plan.row,
        col=plan.col,
        dep_fingerprint=dep_fingerprint,
        classes=winners,
        participant_keys=participant_keys,
    )


def postprocess_classes(
    classes: np.ndarray,
    minimum_area: int,
) -> tuple[np.ndarray, int]:
    if minimum_area <= 0:
        return classes.copy(), 0
    output = classes.copy()
    height, width = output.shape
    visited = np.zeros_like(output, dtype=bool)
    replacements: list[tuple[np.ndarray, int]] = []
    for y in range(height):
        for x in range(width):
            class_id = int(output[y, x])
            if class_id == 0 or visited[y, x]:
                continue
            component: list[tuple[int, int]] = []
            queue: deque[tuple[int, int]] = deque([(y, x)])
            visited[y, x] = True
            while queue:
                cy, cx = queue.popleft()
                component.append((cy, cx))
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < height and 0 <= nx < width and not visited[ny, nx] and int(output[ny, nx]) == class_id:
                        visited[ny, nx] = True
                        queue.append((ny, nx))
            if len(component) >= minimum_area:
                continue
            neighbor_counts: Counter[int] = Counter()
            component_set = set(component)
            for cy, cx in component:
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if (ny, nx) not in component_set and 0 <= ny < height and 0 <= nx < width:
                        neighbor_counts[int(output[ny, nx])] += 1
            if neighbor_counts:
                max_count = max(neighbor_counts.values())
                target = min(c for c, count in neighbor_counts.items() if count == max_count)
            else:
                target = 0
            ys = np.fromiter((p[0] for p in component), dtype=np.int64, count=len(component))
            xs = np.fromiter((p[1] for p in component), dtype=np.int64, count=len(component))
            replacements.append((np.stack([ys, xs], axis=1), target))
    for coords, target in replacements:
        output[coords[:, 0], coords[:, 1]] = target
    return output, len(replacements)
