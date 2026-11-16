from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import InputBundle
from .dedupe import build_groups_and_removals, candidate_pairs, compute_similarity_pair
from .errors import WorkbenchError, failure_from_exception
from .generation import (
    GenerationPlan,
    build_plan,
    generate_candidate,
    planned_sample_id,
)
from .injection import missing_hits
from .scoring import score_candidate
from .signals import StopController
from .snapshot import initial_state, load_snapshot, save_snapshot
from .tokenizer import SubwordTokenizer, load_tokenizer


@dataclass
class RunOutcome:
    state: dict[str, Any]
    exit_code: int
    interrupted: bool
    resumed: bool
    checkpoints: int


def _sort_failures(failures: list[dict[str, Any]]) -> None:
    failures.sort(
        key=lambda item: (
            str(item.get("sample_id", "")),
            str(item.get("stage", "")),
            str(item.get("business_code", "")),
            str(item.get("exception_type", "")),
        )
    )


def _sort_injection_hits(hits: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [hits[key] for key in sorted(hits)]


def _enrich_injection_ids(state: dict[str, Any]) -> None:
    by_sample: dict[str, list[str]] = {}
    for hit in state["injection_hits"].values():
        sample_id = hit.get("sample_id")
        if sample_id:
            by_sample.setdefault(sample_id, []).append(str(hit["injection_id"]))
    for sample_id, injection_ids in by_sample.items():
        by_sample[sample_id] = sorted(injection_ids)
    for item in state["scoring"]["scored"]:
        item["injection_ids"] = by_sample.get(item["sample_id"], [])
    for failure in state["failures"]:
        failure["injection_ids"] = by_sample.get(str(failure.get("sample_id")), [])


def run_workbench(
    bundle: InputBundle,
    output_dir: Path,
    *,
    controller: StopController,
    workers_override: int | None = None,
    checkpoint_delay_ms: float = 0.0,
    stop_after_checkpoints: int | None = None,
) -> RunOutcome:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir = output_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = state_dir / "latest.json"

    state = load_snapshot(snapshot_path, bundle)
    resumed = state is not None
    if state is None:
        state = initial_state(bundle)

    workers = workers_override if workers_override is not None else bundle.concurrency
    if workers < 1:
        raise WorkbenchError("E_INPUT_INVALID", "workers must be >= 1", "runtime")
    checkpoints = 0
    tokenizer = load_tokenizer(bundle.root, bundle.vocab, bundle.rules)
    seeds_by_task: dict[str, list[dict[str, Any]]] = {}
    for index, seed in enumerate(bundle.seeds):
        seed_copy = dict(seed)
        seed_copy.setdefault("seed_id", f"seed-{index + 1:04d}")
        seeds_by_task.setdefault(str(seed["task_type"]), []).append(seed_copy)

    plans, skips = build_plan(bundle)
    plan_rows = [
        {
            "task_type": plan.task_type,
            "generation_index": plan.generation_index,
            "global_index": plan.global_index,
        }
        for plan in plans
    ]
    if state["generation"].get("plans") and state["generation"]["plans"] != plan_rows:
        raise WorkbenchError(
            "E_CONFIG_MISMATCH", "generation plan differs from snapshot", "snapshot"
        )
    state["generation"]["plans"] = plan_rows
    state["generation"]["skips"] = sorted(
        skips,
        key=lambda item: (
            str(item["task_type"]),
            str(item["reason_code"]),
            str(item.get("template_variant_id") or ""),
        ),
    )

    stage_started = time.perf_counter()
    stage_name = state["snapshot_stage"]
    timing_base = float(state["stage_timings"].get(stage_name, 0.0))

    def update_timing() -> None:
        if stage_name in state["stage_timings"]:
            state["stage_timings"][stage_name] = round(
                timing_base + time.perf_counter() - stage_started, 9
            )

    def begin_stage(name: str) -> None:
        nonlocal stage_started, stage_name, timing_base
        stage_name = name
        stage_started = time.perf_counter()
        timing_base = float(state["stage_timings"].get(name, 0.0))

    def checkpoint() -> bool:
        nonlocal checkpoints
        update_timing()
        save_snapshot(snapshot_path, state)
        checkpoints += 1
        if checkpoint_delay_ms > 0:
            time.sleep(checkpoint_delay_ms / 1000.0)
        if stop_after_checkpoints is not None and checkpoints >= stop_after_checkpoints:
            controller.request("checkpoint_limit")
        return controller.requested

    def interrupt() -> RunOutcome:
        state["status"] = "interrupted"
        update_timing()
        save_snapshot(snapshot_path, state)
        return RunOutcome(
            state=state,
            exit_code=0,
            interrupted=True,
            resumed=resumed,
            checkpoints=checkpoints,
        )

    if controller.requested:
        return interrupt()

    if state["snapshot_stage"] == "complete":
        _enrich_injection_ids(state)
        return RunOutcome(
            state=state,
            exit_code=0,
            interrupted=False,
            resumed=resumed,
            checkpoints=checkpoints,
        )

    if state["snapshot_stage"] == "generation":
        cursor = int(state["cursor"]["generation"])
        if cursor > len(plans):
            raise WorkbenchError("E_SNAPSHOT_INVALID", "generation cursor is out of range", "snapshot")
        seed_bytes = bytes.fromhex(bundle.generation_fingerprint)
        injection_items = bundle.injections.get("items", [])
        for position in range(cursor, len(plans)):
            plan = plans[position]
            try:
                candidate = generate_candidate(
                    bundle,
                    plan,
                    seed=seed_bytes,
                    seeds_by_task=seeds_by_task,
                    injection_items=injection_items,
                    injection_hits=state["injection_hits"],
                )
            except BaseException as exc:
                sample_id = planned_sample_id(plan.task_type, plan.generation_index)
                state["failures"].append(
                    failure_from_exception(
                        exc,
                        stage="generation",
                        sample_id=sample_id,
                        fallback_code="E_GENERATION_FAILED",
                    )
                )
            else:
                state["generation"]["candidates"].append(candidate)
            state["cursor"]["generation"] = position + 1
            if checkpoint():
                return interrupt()

        state["generation"]["candidates"].sort(key=lambda item: item["sample_id"])
        for missed in missing_hits(injection_items, state["injection_hits"]):
            state["injection_hits"][str(missed["injection_id"])] = missed
        _enrich_injection_ids(state)
        state["snapshot_stage"] = "scoring"
        state["cursor"]["scoring"] = 0
        begin_stage("scoring")
        save_snapshot(snapshot_path, state)

    if state["snapshot_stage"] == "scoring":
        candidates = state["generation"]["candidates"]
        candidates.sort(key=lambda item: item["sample_id"])
        cursor = int(state["cursor"]["scoring"])
        if cursor > len(candidates):
            raise WorkbenchError("E_SNAPSHOT_INVALID", "scoring cursor is out of range", "snapshot")
        remaining = candidates[cursor:]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="score") as executor:
            futures = [
                executor.submit(score_candidate, bundle, candidate, tokenizer)
                for candidate in remaining
            ]
            for offset, future in enumerate(futures):
                candidate = remaining[offset]
                try:
                    score_report, boundary = future.result()
                except BaseException as exc:
                    state["failures"].append(
                        failure_from_exception(
                            exc,
                            stage="scoring",
                            sample_id=candidate.get("sample_id"),
                            fallback_code="E_SCORING_FAILED",
                        )
                    )
                else:
                    state["scoring"]["scored"].append(score_report)
                    state["dedupe"]["length_boundary"].extend(boundary)
                state["cursor"]["scoring"] = cursor + offset + 1
                if checkpoint():
                    return interrupt()
        state["scoring"]["scored"].sort(key=lambda item: item["sample_id"])
        state["dedupe"]["length_boundary"].sort(key=lambda item: item["diagnostic_id"])
        _enrich_injection_ids(state)
        state["snapshot_stage"] = "dedupe"
        state["cursor"]["dedupe"] = 0
        state["dedupe"]["similarities"] = []
        state["dedupe"]["similarity_boundary"] = []
        begin_stage("dedupe")
        save_snapshot(snapshot_path, state)

    if state["snapshot_stage"] == "dedupe":
        scored = state["scoring"]["scored"]
        scored.sort(key=lambda item: item["sample_id"])
        pairs = candidate_pairs(scored)
        cursor = int(state["cursor"]["dedupe"])
        if cursor > len(pairs):
            raise WorkbenchError("E_SNAPSHOT_INVALID", "dedupe cursor is out of range", "snapshot")
        subwords = {
            item["sample_id"]: set(item["fp64"]["subwords"])
            for item in scored
        }
        remaining = pairs[cursor:]
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="similarity") as executor:
            futures = [
                executor.submit(
                    compute_similarity_pair,
                    bundle,
                    left_id,
                    right_id,
                    subwords,
                )
                for left_id, right_id in remaining
            ]
            for offset, future in enumerate(futures):
                left_id, right_id = remaining[offset]
                try:
                    pair_result, diagnostic = future.result()
                except BaseException as exc:
                    state["failures"].append(
                        failure_from_exception(
                            exc,
                            stage="dedupe",
                            sample_id=left_id,
                            fallback_code="E_SIMILARITY_FAILED",
                        )
                    )
                    pair_result = {
                        "pair_key": f"{left_id}|{right_id}|similarity",
                        "candidate_ids": [left_id, right_id],
                        "fp32_similarity": 0.0,
                        "fp64_reference_similarity": 0.0,
                        "similarity_delta": 0.0,
                        "threshold": bundle.config["dedupe_similarity_threshold"],
                        "fp32_duplicate": False,
                        "fp64_duplicate": False,
                        "decision_precision": "float64",
                        "computation_error": True,
                    }
                    diagnostic = None
                state["dedupe"]["similarities"].append(pair_result)
                if diagnostic is not None:
                    state["dedupe"]["similarity_boundary"].append(diagnostic)
                state["cursor"]["dedupe"] = cursor + offset + 1
                if checkpoint():
                    return interrupt()
        state["dedupe"]["similarities"].sort(key=lambda item: item["pair_key"])
        state["dedupe"]["similarity_boundary"].sort(key=lambda item: item["diagnostic_id"])
        groups, removals, _decisions = build_groups_and_removals(
            bundle, scored, state["dedupe"]["similarities"]
        )
        state["dedupe"]["groups"] = groups
        state["dedupe"]["removals"] = removals
        _sort_failures(state["failures"])
        _enrich_injection_ids(state)
        state["snapshot_stage"] = "complete"
        state["status"] = "completed"
        update_timing()
        save_snapshot(snapshot_path, state)
        checkpoints += 1

    return RunOutcome(
        state=state,
        exit_code=0,
        interrupted=False,
        resumed=resumed,
        checkpoints=checkpoints,
    )

