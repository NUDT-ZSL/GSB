"""Block grid planning and the explicit latency/consistency trade-off verdict.

The adjudication is a pure deterministic function of the task, configuration
and declared priority order, so the same inputs always reproduce the same
chosen grid, strength and rationale.  Real wall-clock timings are recorded
separately as *observations* and never feed back into the deterministic plan.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from .clipmodel import ClipTask, blocks_of_keyframes
from .hashing import hash_json


PRIORITY_DIMENSIONS = ("block_grid", "first_frame_budget", "diffusion_strength")


def describe_blocks(keyframe_count: int, block_grid: int) -> List[Dict[str, Any]]:
    ordinals = list(range(keyframe_count))
    chunks = [ordinals[i : i + block_grid] for i in range(0, keyframe_count, block_grid)]
    descriptors: List[Dict[str, Any]] = []
    for block_index, members in enumerate(chunks):
        descriptors.append(
            {
                "block_index": block_index,
                "ordinals": members,
                "size": len(members),
                "is_first": block_index == 0,
                "is_last": block_index == len(chunks) - 1,
            }
        )
    return descriptors


def consistency_risk(keyframe_count: int, block_grid: int, config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = config
    risk_limit = float(cfg["consistency_risk_limit"])
    weight = float(cfg["boundary_risk_weight"])
    coupling_width = int(cfg.get("coupling_width", 1))
    blocks = describe_blocks(keyframe_count, block_grid)
    boundary_count = max(0, len(blocks) - 1)
    coupling_states = boundary_count * 2 * coupling_width
    risk = round(boundary_count * weight * coupling_width, 6)
    return {
        "block_grid": int(block_grid),
        "block_count": len(blocks),
        "boundary_count": boundary_count,
        "boundary_coupling_states": coupling_states,
        "risk": risk,
        "risk_limit": risk_limit,
        "risk_ok": risk <= risk_limit,
        "risk_violation": round(max(0.0, risk - risk_limit), 6),
    }


def first_block_coupling_links(keyframe_count: int, block_grid: int) -> int:
    """Internal links plus the right-edge halo link for the first block."""
    first_size = min(int(block_grid), keyframe_count)
    block_count = math.ceil(keyframe_count / max(1, block_grid))
    internal_links = max(0, first_size - 1)
    halo_links = 1 if block_count > 1 else 0
    return internal_links + halo_links


def effective_steps_for(diffusion_steps: int, strength: float) -> int:
    return int(max(2, round(diffusion_steps * strength)))


def estimate_first_block_ms(
    task: ClipTask,
    block_grid: int,
    effective_steps: int,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    model = config["latency_model"]
    first_size = min(int(block_grid), task.keyframe_count)
    pixels = task.pixels_per_frame
    pixel_step_units = first_size * pixels * effective_steps
    links = first_block_coupling_links(task.keyframe_count, block_grid)
    base = float(model["base_ms"])
    keyframe_term = float(model.get("coef_keyframe_ms", 0.0)) * first_size
    pixel_term = float(model["coef_pixel_step_ms"]) * pixel_step_units
    coupling_term = float(model["coef_coupling_link_ms"]) * links
    estimate = base + keyframe_term + pixel_term + coupling_term
    budget = float(config["first_frame_budget_ms"])
    return {
        "block_grid": int(block_grid),
        "first_block_keyframes": first_size,
        "pixels_per_keyframe": pixels,
        "effective_steps": int(effective_steps),
        "pixel_step_units": int(pixel_step_units),
        "coupling_links": links,
        "base_ms": round(base, 5),
        "keyframe_overhead_term_ms": round(keyframe_term, 5),
        "pixel_step_term_ms": round(pixel_term, 5),
        "coupling_term_ms": round(coupling_term, 5),
        "estimated_first_query_ms": round(estimate, 5),
        "budget_ms": budget,
        "budget_ok": estimate <= budget,
        "budget_violation": round(max(0.0, estimate / budget - 1.0), 6),
    }


def _candidate_grids(task: ClipTask, config: Dict[str, Any], grid_override: Optional[int]) -> List[int]:
    grids = {int(g) for g in config["block_grid_candidates"] if 1 <= int(g) <= task.keyframe_count}
    requested = int(grid_override if grid_override is not None else task.block_grid)
    grids.add(max(1, min(requested, task.keyframe_count)))
    return sorted(grids)


def adjudicate(
    task: ClipTask,
    config: Dict[str, Any],
    *,
    grid_override: Optional[int] = None,
    budget_ms_override: Optional[float] = None,
    priority_override: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Choose ``(block_grid, strength)`` via priority-ordered satisfaction."""
    eval_config = dict(config)
    if budget_ms_override is not None:
        eval_config = dict(config)
        eval_config["first_frame_budget_ms"] = float(budget_ms_override)

    priority_order = list(priority_override or config["priority_order"])
    if sorted(priority_order) != sorted(PRIORITY_DIMENSIONS) or len(priority_order) != 3:
        raise ValueError(f"priority_order must be a permutation of {PRIORITY_DIMENSIONS}")

    requested_grid = int(grid_override if grid_override is not None else task.block_grid)
    budget = float(eval_config["first_frame_budget_ms"])
    levels = [float(v) for v in config["approximation_levels"]]
    grids = _candidate_grids(task, config, grid_override)

    candidates: List[Dict[str, Any]] = []
    for grid in grids:
        risk_info = consistency_risk(task.keyframe_count, grid, eval_config)
        for strength in levels:
            eff_steps = effective_steps_for(task.diffusion_steps, strength)
            latency = estimate_first_block_ms(task, grid, eff_steps, eval_config)
            losses = {
                "block_grid": risk_info["risk_violation"],
                "first_frame_budget": latency["budget_violation"],
                "diffusion_strength": round(1.0 - strength, 6),
            }
            priority_key = tuple(round(float(losses[dim]), 9) for dim in priority_order)
            tie_break = (
                abs(grid - requested_grid),
                -grid,
                -strength,
            )
            candidates.append(
                {
                    "block_grid": grid,
                    "strength": strength,
                    "effective_steps": eff_steps,
                    "risk": risk_info,
                    "latency": latency,
                    "losses": losses,
                    "priority_key": list(priority_key),
                    "selection_key": list(priority_key) + list(tie_break),
                    "_tie": tie_break,
                }
            )

    selected = min(candidates, key=lambda c: (c["priority_key"], c["_tie"]))

    requested_risk = consistency_risk(task.keyframe_count, requested_grid, eval_config)
    requested_latency_full = estimate_first_block_ms(
        task, requested_grid, effective_steps_for(task.diffusion_steps, 1.0), eval_config
    )

    residual: List[str] = []
    if not selected["risk"]["risk_ok"]:
        residual.append(
            f"consistency risk {selected['risk']['risk']} exceeds limit "
            f"{selected['risk']['risk_limit']} even at the safest candidate grid"
        )
    if not selected["latency"]["budget_ok"]:
        residual.append(
            f"estimated first-query latency {selected['latency']['estimated_first_query_ms']} ms "
            f"exceeds budget {budget} ms; priority order {priority_order} protects other "
            "dimensions, so the budget breach is recorded rather than resolved"
        )

    reasons = _build_reasons(
        task=task,
        priority_order=priority_order,
        requested_grid=requested_grid,
        requested_risk=requested_risk,
        requested_latency=requested_latency_full,
        selected=selected,
        budget=budget,
    )

    plan = {
        "plan_type": "keyframe_block_generation",
        "clip_id": task.clip_id,
        "revision": task.revision,
        "priority_order": priority_order,
        "requested_block_grid": requested_grid,
        "selected_block_grid": selected["block_grid"],
        "selected_strength": selected["strength"],
        "selected_effective_steps": selected["effective_steps"],
        "budget_ms": budget,
        "status": "infeasible" if residual else ("approximated" if selected["strength"] < 1.0 else "feasible"),
        "residual_violations": residual,
        "reasons": reasons,
        "risk_summary": {k: selected["risk"][k] for k in
                         ("block_count", "boundary_count", "boundary_coupling_states", "risk", "risk_limit", "risk_ok")},
        "estimate": selected["latency"],
        "candidates": [
            {k: v for k, v in candidate.items() if not k.startswith("_")}
            for candidate in candidates
        ],
        "requested_assessment": {
            "risk": requested_risk,
            "latency_full_strength": requested_latency_full,
        },
    }
    plan["plan_hash"] = hash_json(_hashable_plan(plan))
    return plan


def _hashable_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in plan.items() if k != "plan_hash"}


def _build_reasons(
    *,
    task: ClipTask,
    priority_order: Sequence[str],
    requested_grid: int,
    requested_risk: Dict[str, Any],
    requested_latency: Dict[str, Any],
    selected: Dict[str, Any],
    budget: float,
) -> List[Dict[str, str]]:
    reasons: List[Dict[str, str]] = []
    reasons.append({
        "code": "PRIORITY_ORDER",
        "detail": "adjudication lexicographic priority is " + " > ".join(priority_order),
    })
    if not requested_risk["risk_ok"]:
        reasons.append({
            "code": "GRID_TOO_SMALL_RISK",
            "detail": (
                f"requested block_grid={requested_grid} creates "
                f"{requested_risk['boundary_count']} boundaries / "
                f"{requested_risk['boundary_coupling_states']} coupling states, "
                f"risk={requested_risk['risk']} > limit {requested_risk['risk_limit']}"
            ),
        })
    if not requested_latency["budget_ok"]:
        reasons.append({
            "code": "GRID_TOO_LARGE_LATENCY",
            "detail": (
                f"requested full-strength first block estimates "
                f"{requested_latency['estimated_first_query_ms']} ms > budget {budget} ms"
            ),
        })
    if selected["block_grid"] != requested_grid:
        direction = "enlarged" if selected["block_grid"] > requested_grid else "shrunk"
        reasons.append({
            "code": "GRID_SELECTED",
            "detail": f"block grid {direction} to {selected['block_grid']} by priority adjudication",
        })
    if selected["strength"] < 1.0:
        reasons.append({
            "code": "DIFFUSION_APPROXIMATED",
            "detail": (
                f"diffusion strength reduced to {selected['strength']} "
                f"({selected['effective_steps']} of {task.diffusion_steps} steps) "
                "to recover the first-query budget"
            ),
        })
    if not reasons[1:]:
        reasons.append({
            "code": "REQUESTED_GRID_ACCEPTED",
            "detail": (
                f"requested block_grid={requested_grid} satisfies risk limit and the "
                f"{budget} ms first-query budget at full diffusion strength"
            ),
        })
    return reasons


def timing_observation(estimate: Dict[str, Any], observed_first_block_ms: float) -> Dict[str, Any]:
    budget = float(estimate["budget_ms"])
    predicted = float(
        estimate.get("estimated_first_query_ms", estimate.get("estimated_first_block_ms", 0.0))
    )
    return {
        "observed_first_block_ms": round(float(observed_first_block_ms), 5),
        "estimated_first_block_ms": predicted,
        "budget_ms": budget,
        "within_budget": float(observed_first_block_ms) <= budget,
        "estimate_error_ms": round(float(observed_first_block_ms) - predicted, 5),
        "estimate_error_ratio": round((float(observed_first_block_ms) - predicted) / max(predicted, 1e-9), 4),
    }
