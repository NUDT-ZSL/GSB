"""Batch-update plan and injection configuration parsing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import read_json, sha256_json
from .config import ConfigError


def load_plan(path: Path) -> dict[str, Any]:
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        raise ConfigError("E_PLAN_INVALID", f"cannot read update plan: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("batches"), list):
        raise ConfigError("E_PLAN_INVALID", "update plan must contain a batches list")
    batches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, batch in enumerate(raw["batches"]):
        if not isinstance(batch, dict):
            raise ConfigError("E_PLAN_INVALID", f"batches[{index}] must be an object")
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ConfigError("E_PLAN_INVALID", f"batches[{index}].batch_id must be non-empty")
        if batch_id in seen:
            raise ConfigError("E_PLAN_INVALID", f"duplicate batch_id: {batch_id}")
        seen.add(batch_id)
        turn_ops = batch.get("turn_ops", batch.get("turns", []))
        vocab_ops = batch.get("vocab_ops", batch.get("vocabulary", []))
        if not isinstance(turn_ops, list):
            raise ConfigError("E_PLAN_INVALID", f"batch {batch_id} turn_ops must be a list")
        if not isinstance(vocab_ops, list):
            raise ConfigError("E_PLAN_INVALID", f"batch {batch_id} vocab_ops must be a list")
        weight_patch = batch.get("weight_patch", {})
        if not isinstance(weight_patch, dict):
            raise ConfigError("E_PLAN_INVALID", f"batch {batch_id} weight_patch must be an object")
        batches.append(
            {
                "batch_id": batch_id,
                "turn_ops": turn_ops,
                "vocab_ops": vocab_ops,
                "weight_patch": dict(weight_patch),
            }
        )
    return {"schema_version": int(raw.get("schema_version", 1)), "batches": batches}


def plan_hash(plan: dict[str, Any]) -> str:
    return sha256_json(plan)


def load_injections(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"schema_version": 1, "injections": [], "force_exceptions": [], "probes": []}
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        raise ConfigError("E_INJECTION_INVALID", f"cannot read injection config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("E_INJECTION_INVALID", "injection config root must be an object")
    injections = raw.get("injections", [])
    force_exceptions = raw.get("force_exceptions", [])
    probes = raw.get("probes", [])
    for key, value in (
        ("injections", injections),
        ("force_exceptions", force_exceptions),
        ("probes", probes),
    ):
        if not isinstance(value, list):
            raise ConfigError("E_INJECTION_INVALID", f"injection config {key} must be a list")
    identifiers: set[str] = set()
    for collection in (injections, force_exceptions, probes):
        for item in collection:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                raise ConfigError("E_INJECTION_INVALID", "every injection item needs a non-empty id")
            if item["id"] in identifiers:
                raise ConfigError("E_INJECTION_INVALID", f"duplicate injection id: {item['id']}")
            identifiers.add(item["id"])
    return {
        "schema_version": int(raw.get("schema_version", 1)),
        "injections": injections,
        "force_exceptions": force_exceptions,
        "probes": probes,
    }


def injection_hash(injections: dict[str, Any]) -> str:
    return sha256_json(injections)


def force_exception_maps(
    injections: dict[str, Any],
) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[tuple[str, int], dict[str, Any]]]:
    turn_map: dict[tuple[str, int], dict[str, Any]] = {}
    window_map: dict[tuple[str, int], dict[str, Any]] = {}
    for item in injections.get("force_exceptions", []):
        kind = item.get("kind")
        session_id = item.get("session_id")
        if kind == "turn_feature" and isinstance(session_id, str):
            turn_no = item.get("turn_no")
            if isinstance(turn_no, int) and not isinstance(turn_no, bool):
                turn_map[(session_id, turn_no)] = item
        elif kind == "window_score" and isinstance(session_id, str):
            start_turn = item.get("start_turn")
            if isinstance(start_turn, int) and not isinstance(start_turn, bool):
                window_map[(session_id, start_turn)] = item
    return turn_map, window_map


def affected_start_keys(
    session_id: str,
    turn_no: int,
    window_size: int,
    current_turns: set[int],
    existing_window_starts: set[int],
) -> set[tuple[str, int]]:
    lowest = max(1, turn_no - window_size + 1)
    candidates = set(range(lowest, turn_no + 1))
    result = {(session_id, start) for start in candidates if start in current_turns}
    result.update(
        (session_id, start) for start in candidates if start in existing_window_starts
    )
    return result

