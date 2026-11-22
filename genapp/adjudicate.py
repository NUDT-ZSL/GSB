"""首块耗时 / 块划分 / 一致性校验强度的显式裁决.

优先级规则 (从高到低, 全部记录在生成计划中):
  P1 一致性校验强度: 边界耦合态超过阈值时校验档位必须提升到
     全量逐字节 (tier 2), 否则取配置最低档位;
  P2 资源硬上限: 像素总量超限直接 E_RESOURCE_LIMIT;
     单块驻留内存超限的网格不可选;
  P3 首块耗时预算: 在满足 P1/P2 的候选中选择块数最少
     (边界耦合态最少) 且估算首块耗时不超过预算的网格;
  P4 降级: 无候选满足预算时降级到最细网格并标记降级类别;
     最细网格仍违反资源硬上限则 E_RESOURCE_LIMIT.

裁决输出为规范化 JSON 字节, 同一配置下逐字节可复现.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from .blocks import (
    block_ranges,
    block_resident_bytes,
    boundary_coupling_states,
    center_first_order,
    estimate_first_block_ms,
    parse_grid,
)
from .config import GenerationConfig
from .errors import E_RESOURCE_LIMIT, GenError

TIER_NAMES = {1: "boundary-sample", 2: "full-byte"}


def _round6(value: float) -> float:
    return float(f"{value:.6f}")


def _candidate_entry(cfg: GenerationConfig, grid: str) -> Dict[str, object]:
    rows, cols = parse_grid(grid)
    ranges = block_ranges(cfg, rows, cols)
    order = center_first_order(rows, cols)
    first_block = ranges[order[0]]
    est_ms = estimate_first_block_ms(cfg, first_block)
    resident = max(block_resident_bytes(cfg, b) for b in ranges)
    coupling = boundary_coupling_states(cfg, rows, cols)
    return {
        "grid": grid,
        "rows": rows,
        "cols": cols,
        "blocks": len(ranges),
        "est_first_block_ms": est_ms,
        "resident_bytes": resident,
        "coupling_states": coupling,
        "within_budget": est_ms <= cfg.latency_budget_ms,
        "within_memory": resident <= cfg.limits.max_block_bytes,
    }


def _finalize(
    cfg: GenerationConfig,
    selected: Dict[str, object],
    candidates: List[Dict[str, object]],
    degradation: Optional[str],
    rationale: str,
) -> Dict[str, object]:
    coupling = int(selected["coupling_states"])
    if coupling > cfg.coupling_threshold:
        tier = 2
        tier_reason = "P1: coupling states exceed threshold, full-byte required"
    else:
        tier = max(1, cfg.verify_min_tier)
        tier_reason = "P1: configured minimum tier suffices"
    return {
        "schema": "genapp.plan/v1",
        "grid": selected["grid"],
        "blocks": selected["blocks"],
        "est_first_block_ms": _round6(float(selected["est_first_block_ms"])),
        "latency_budget_ms": _round6(cfg.latency_budget_ms),
        "coupling_states": coupling,
        "coupling_threshold": cfg.coupling_threshold,
        "verify_tier": tier,
        "verify_tier_name": TIER_NAMES[tier],
        "verify_tier_reason": tier_reason,
        "degradation": degradation,
        "rationale": rationale,
        "candidates": [
            {
                "grid": c["grid"],
                "blocks": c["blocks"],
                "est_first_block_ms": _round6(float(c["est_first_block_ms"])),
                "resident_bytes": c["resident_bytes"],
                "coupling_states": c["coupling_states"],
                "within_budget": c["within_budget"],
                "within_memory": c["within_memory"],
            }
            for c in candidates
        ],
    }


def adjudicate(cfg: GenerationConfig) -> Dict[str, object]:
    """按显式优先级规则裁决块划分 / 预算 / 校验档位."""
    if cfg.total_pixels > cfg.limits.max_total_pixels:
        raise GenError(
            E_RESOURCE_LIMIT,
            "total pixels exceed limit",
            total_pixels=cfg.total_pixels,
            max_total_pixels=cfg.limits.max_total_pixels,
        )
    candidates: List[Dict[str, object]] = [
        _candidate_entry(cfg, grid) for grid in cfg.grid_candidates
    ]
    candidates.sort(key=lambda c: int(c["blocks"]))

    degradation: Optional[str] = None
    selected: Optional[Dict[str, object]] = None
    rationale: str
    for cand in candidates:
        if not cand["within_memory"]:
            continue
        if cand["within_budget"]:
            selected = cand
            rationale = (
                "P3: coarsest grid within latency budget and memory limit"
            )
            break
    if selected is None:
        feasible = [c for c in candidates if c["within_memory"]]
        if not feasible:
            raise GenError(
                E_RESOURCE_LIMIT,
                "no grid satisfies block memory limit",
                max_block_bytes=cfg.limits.max_block_bytes,
            )
        selected = feasible[-1]
        degradation = "LATENCY_BUDGET"
        rationale = (
            "P4: no grid within latency budget; degraded to finest "
            "memory-feasible grid"
        )

    return _finalize(cfg, selected, candidates, degradation, rationale)


def override_plan(cfg: GenerationConfig, grid: str) -> Dict[str, object]:
    """手动切换块网格: 同一套 P1 校验档位规则, 标记为人工覆写."""
    selected = _candidate_entry(cfg, grid)
    if not selected["within_memory"]:
        raise GenError(
            E_RESOURCE_LIMIT,
            "overridden grid exceeds block memory limit",
            grid=grid,
            resident_bytes=selected["resident_bytes"],
            max_block_bytes=cfg.limits.max_block_bytes,
        )
    candidates = [_candidate_entry(cfg, g) for g in cfg.grid_candidates]
    candidates.sort(key=lambda c: int(c["blocks"]))
    return _finalize(
        cfg,
        selected,
        candidates,
        degradation=None,
        rationale="manual grid override; P1 tier rule still applied",
    )


def canonical_bytes(payload: Dict[str, object]) -> bytes:
    """规范化序列化: 键排序 + 紧凑分隔符, 保证逐字节可复现."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
