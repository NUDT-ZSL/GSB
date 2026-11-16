from __future__ import annotations

from pathlib import Path
from typing import Any

from .atomic import atomic_write_json
from .config import InputBundle
from .errors import WorkbenchError


SNAPSHOT_SCHEMA = "instruction-workbench-snapshot-v1"
SNAPSHOT_VERSION = 1


def initial_state(bundle: InputBundle) -> dict[str, Any]:
    return {
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_stage": "generation",
        "status": "running",
        "config_hash": bundle.config_hash,
        "scoring_params_hash": bundle.scoring_params_hash,
        "injection_hash": bundle.injection_hash,
        "input_fingerprint": bundle.input_fingerprint,
        "generation_fingerprint": bundle.generation_fingerprint,
        "precision_mode": bundle.precision_mode,
        "cursor": {"generation": 0, "scoring": 0, "dedupe": 0},
        "generation": {"candidates": [], "skips": [], "plans": []},
        "scoring": {"scored": []},
        "dedupe": {
            "similarities": [],
            "length_boundary": [],
            "similarity_boundary": [],
            "groups": [],
            "removals": [],
        },
        "failures": [],
        "injection_hits": {},
        "stage_timings": {"generation": 0.0, "scoring": 0.0, "dedupe": 0.0},
        "resume_count": 0,
    }


def result_summary(state: dict[str, Any]) -> dict[str, Any]:
    scored = state["scoring"]["scored"]
    removed = state["dedupe"]["removals"]
    returns = {
        "stage": state["snapshot_stage"],
        "generated_candidates": len(state["generation"]["candidates"]),
        "scored_candidates": len(scored),
        "failures": len(state["failures"]),
        "precision_sensitive": sum(1 for item in scored if item["precision_sensitive"]),
        "groups": len(state["dedupe"]["groups"]),
        "retained": len(scored) - len(removed) if state["snapshot_stage"] == "complete" else None,
        "removed": len(removed),
        "similarity_pairs_completed": len(state["dedupe"]["similarities"]),
    }
    return returns


def save_snapshot(path: Path, state: dict[str, Any]) -> None:
    state["result_summary"] = result_summary(state)
    atomic_write_json(path, state)


def load_snapshot(path: Path, bundle: InputBundle) -> dict[str, Any] | None:
    if not path.exists():
        return None
    import json

    try:
        with path.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError("E_SNAPSHOT_INVALID", f"cannot read snapshot: {exc}", "snapshot") from exc
    if not isinstance(state, dict):
        raise WorkbenchError("E_SNAPSHOT_INVALID", "snapshot root must be an object", "snapshot")
    if state.get("snapshot_schema") != SNAPSHOT_SCHEMA:
        raise WorkbenchError(
            "E_SNAPSHOT_VERSION",
            f"snapshot schema mismatch: {state.get('snapshot_schema')!r}",
            "snapshot",
        )
    if state.get("snapshot_version") != SNAPSHOT_VERSION:
        raise WorkbenchError(
            "E_SNAPSHOT_VERSION",
            f"snapshot version mismatch: {state.get('snapshot_version')!r}",
            "snapshot",
        )
    expected = {
        "config_hash": bundle.config_hash,
        "scoring_params_hash": bundle.scoring_params_hash,
        "injection_hash": bundle.injection_hash,
        "input_fingerprint": bundle.input_fingerprint,
        "precision_mode": bundle.precision_mode,
    }
    mismatches = {
        key: {"snapshot": state.get(key), "current": value}
        for key, value in expected.items()
        if state.get(key) != value
    }
    if mismatches:
        raise WorkbenchError(
            "E_CONFIG_MISMATCH",
            "snapshot hashes or precision do not match current inputs",
            "snapshot",
            details={"mismatches": mismatches},
        )
    state["resume_count"] = int(state.get("resume_count", 0)) + 1
    state["status"] = "running"
    return state

