from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json
from .deps import edit_changed_keys
from .errors import ValidationError


def apply_edit_step(documents: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    """Apply one ordered edit to raw JSON documents and return new documents."""

    result = copy.deepcopy(documents)
    change_type = step.get("change_type")
    op = step.get("op", "set")
    if op != "set" and op != "set_drop_weight":
        raise ValidationError(
            f"unsupported edit operation {op!r}",
            code="E_EDIT_OP",
            details={"step": step},
        )

    if change_type == "unit_attr":
        units = result["balance_numbers.json"]["units"]
        target_id = step["id"]
        for unit in units:
            if unit.get("id") == target_id:
                unit[step["field"]] = step["value"]
                return result
        raise ValidationError(
            f"edit references missing unit {target_id}",
            code="E_EDIT_TARGET",
            details={"step": step},
        )

    if change_type == "skill_param":
        skills = result["balance_numbers.json"]["skills"]
        target_id = step["id"]
        for skill in skills:
            if skill.get("id") == target_id:
                skill[step["field"]] = step["value"]
                return result
        raise ValidationError(
            f"edit references missing skill {target_id}",
            code="E_EDIT_TARGET",
            details={"step": step},
        )

    if change_type == "drop_weight":
        tables = result["balance_numbers.json"]["drop_tables"]
        for table in tables:
            if table.get("id") != step["table_id"]:
                continue
            for entry in table.get("entries", []):
                if entry.get("channel") == step["channel"]:
                    entry["weight"] = step["value"]
                    return result
            raise ValidationError(
                f"drop channel {step['channel']} not found in {step['table_id']}",
                code="E_EDIT_TARGET",
                details={"step": step},
            )
        raise ValidationError(
            f"drop table {step['table_id']} not found",
            code="E_EDIT_TARGET",
            details={"step": step},
        )

    if change_type == "criterion":
        criteria = result["levels.json"]["criteria"]
        criteria_id = step["criteria_id"]
        if criteria_id not in criteria:
            raise ValidationError(
                f"criteria {criteria_id} not found",
                code="E_EDIT_TARGET",
                details={"step": step},
            )
        criteria[criteria_id][step["field"]] = step["value"]
        return result

    raise ValidationError(
        f"unsupported edit change_type {change_type!r}",
        code="E_EDIT_TYPE",
        details={"step": step},
    )


def materialize_documents(output_dir: str | Path, documents: dict[str, Any]) -> None:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for filename, value in documents.items():
        atomic_write_json(output / filename, value)


def step_dependency_keys(step: dict[str, Any]) -> list[str]:
    return edit_changed_keys(step)
