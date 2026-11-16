from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from .config import InputBundle
from .errors import WorkbenchError
from .hashing import derive_unit_interval
from .injection import apply_injections


SAFE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class GenerationPlan:
    task_type: str
    generation_index: int
    global_index: int


@dataclass(frozen=True)
class WeightedOption:
    identifier: str
    weight: Decimal
    payload: Any


def _decimal_weight(value: Any, context: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkbenchError("E_INPUT_INVALID", f"{context} must be numeric", "generation")
    try:
        weight = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - guarded by config loading
        raise WorkbenchError("E_INPUT_INVALID", f"invalid weight in {context}", "generation") from exc
    return weight


def planned_sample_id(task_type: str, generation_index: int) -> str:
    safe_task = SAFE_ID_PATTERN.sub("-", task_type).strip("-") or "task"
    return f"smp-{safe_task}-{generation_index:04d}"


def _choose(options: list[WeightedOption], seed: bytes, *parts: str) -> WeightedOption:
    positive = [option for option in options if option.weight > 0]
    if not positive:
        raise WorkbenchError("E_ZERO_WEIGHT", "no positive sampling weight", "generation")
    total = sum((option.weight for option in positive), Decimal(0))
    point = Decimal(str(derive_unit_interval(seed, *parts))) * total
    cumulative = Decimal(0)
    for option in positive:
        cumulative += option.weight
        if point < cumulative:
            return option
    return positive[-1]


def _tasks_by_type(seeds: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for index, seed in enumerate(seeds):
        task_type = str(seed.get("task_type", ""))
        if not task_type:
            raise WorkbenchError("E_INPUT_INVALID", f"seed line {index + 1} has no task_type", "input")
        seed_copy = dict(seed)
        seed_copy.setdefault("seed_id", f"seed-{index + 1:04d}")
        result.setdefault(task_type, []).append(seed_copy)
    return result


def _seed_has_positive_slots(seed: dict[str, Any], task_config: dict[str, Any]) -> bool:
    configured = task_config["slot_source_weights"]
    for slot in seed["input_slots"]:
        slot_name = str(slot.get("name", ""))
        weights = configured.get(slot_name, {})
        if not any(
            _decimal_weight(weights.get(str(source.get("id", "")), 0), "slot weight") > 0
            for source in slot.get("sources", [])
        ):
            return False
    return True


def build_plan(bundle: InputBundle) -> tuple[list[GenerationPlan], list[dict[str, Any]]]:
    tasks = bundle.config["tasks"]
    seeds = _tasks_by_type(bundle.seeds)
    plans: list[GenerationPlan] = []
    skips: list[dict[str, Any]] = []
    global_index = 0
    for task_type in sorted(tasks):
        task_config = tasks[task_type]
        target = int(task_config["target_samples"])
        if target == 0:
            skips.append(
                {
                    "task_type": task_type,
                    "reason_code": "E_ZERO_TARGET",
                    "reason": "target_samples is zero",
                    "template_variant_id": None,
                    "slot_source_id": None,
                }
            )
            continue
        seed_rows = seeds.get(task_type, [])
        if not seed_rows:
            skips.append(
                {
                    "task_type": task_type,
                    "reason_code": "E_SEED_MISSING",
                    "reason": "no seed with this task_type",
                    "template_variant_id": None,
                    "slot_source_id": None,
                }
            )
            continue
        template_weights = task_config["template_variant_weights"]
        positive_template = False
        for seed in seed_rows:
            if not _seed_has_positive_slots(seed, task_config):
                continue
            for variant in seed["template_variants"]:
                variant_id = str(variant.get("id", ""))
                weight = _decimal_weight(template_weights.get(variant_id, 0), "template weight")
                if weight > 0:
                    positive_template = True
        if not positive_template:
            skips.append(
                {
                    "task_type": task_type,
                    "reason_code": "E_ZERO_WEIGHT",
                    "reason": "all template_variant_weights are <= 0",
                    "template_variant_id": None,
                    "slot_source_id": None,
                }
            )
            continue
        for generation_index in range(target):
            plans.append(
                GenerationPlan(
                    task_type=task_type,
                    generation_index=generation_index,
                    global_index=global_index,
                )
            )
            global_index += 1
    return plans, skips


def _seed_options(
    bundle: InputBundle,
    task_type: str,
    seeds_by_task: dict[str, list[dict[str, Any]]],
) -> list[WeightedOption]:
    task_config = bundle.config["tasks"][task_type]
    options: list[WeightedOption] = []
    seen_ids: set[str] = set()
    for seed in seeds_by_task.get(task_type, []):
        if not _seed_has_positive_slots(seed, task_config):
            continue
        for variant in seed["template_variants"]:
            variant_id = str(variant.get("id", ""))
            if not variant_id:
                raise WorkbenchError("E_INPUT_INVALID", "template variant id cannot be empty", "generation")
            if variant_id in seen_ids:
                raise WorkbenchError(
                    "E_INPUT_INVALID", f"duplicate template variant id: {variant_id}", "generation"
                )
            seen_ids.add(variant_id)
            weight = _decimal_weight(
                task_config["template_variant_weights"].get(variant_id, 0),
                f"template weight {variant_id}",
            )
            options.append(
                WeightedOption(
                    identifier=variant_id,
                    weight=weight,
                    payload=(seed, variant),
                )
            )
    return options


def generate_candidate(
    bundle: InputBundle,
    plan: GenerationPlan,
    *,
    seed: bytes,
    seeds_by_task: dict[str, list[dict[str, Any]]],
    injection_items: list[dict[str, Any]],
    injection_hits: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    task_type = plan.task_type
    task_config = bundle.config["tasks"][task_type]
    template_options = _seed_options(bundle, task_type, seeds_by_task)
    chosen_template = _choose(
        template_options,
        seed,
        task_type,
        str(plan.generation_index),
        "template",
    )
    selected_seed, selected_variant = chosen_template.payload

    slot_values: dict[str, str] = {}
    slot_sources: dict[str, str] = {}
    sampling_weight = chosen_template.weight
    for slot in selected_seed["input_slots"]:
        slot_name = str(slot.get("name", ""))
        source_options: list[WeightedOption] = []
        configured_weights = task_config["slot_source_weights"].get(slot_name, {})
        for source in slot.get("sources", []):
            source_id = str(source.get("id", ""))
            weight = _decimal_weight(
                configured_weights.get(source_id, 0),
                f"slot weight {task_type}.{slot_name}.{source_id}",
            )
            source_options.append(
                WeightedOption(
                    identifier=source_id,
                    weight=weight,
                    payload=source,
                )
            )
        chosen_source = _choose(
            source_options,
            seed,
            task_type,
            str(plan.generation_index),
            "slot",
            slot_name,
        )
        slot_values[slot_name] = str(chosen_source.payload.get("text", ""))
        slot_sources[slot_name] = chosen_source.identifier
        sampling_weight *= chosen_source.weight

    template = str(selected_variant.get("template", ""))
    try:
        instruction = template.format_map(slot_values)
    except (KeyError, ValueError) as exc:
        raise WorkbenchError(
            "E_TEMPLATE_RENDER",
            f"cannot render template {chosen_template.identifier}: {exc}",
            "generation",
        ) from exc

    sample_id = planned_sample_id(task_type, plan.generation_index)
    candidate: dict[str, Any] = {
        "sample_id": sample_id,
        "task_type": task_type,
        "seed_id": str(selected_seed.get("seed_id", "")),
        "template_variant_id": chosen_template.identifier,
        "template_text": template,
        "slot_values": slot_values,
        "slot_sources": slot_sources,
        "declared_slot_names": sorted(slot_values),
        "generation_index": plan.generation_index,
        "global_generation_index": plan.global_index,
        "sampling_weight": float(sampling_weight),
        "sampling_weight_decimal": format(sampling_weight.normalize(), "f"),
        "instruction": instruction,
        "reference_answer": str(selected_seed.get("reference_answer", "")),
        "answer_evidence": " ".join(slot_values.values()),
        "stable_task_key": f"{sample_id}|generation",
    }
    apply_injections(candidate, injection_items, injection_hits)
    return candidate
