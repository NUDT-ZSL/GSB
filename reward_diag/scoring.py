from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .io_utils import stable_task_key


def score_from_vector(vector: list[int], weights: list[float], precision: int) -> float:
    if len(vector) != len(weights):
        raise ValueError(
            f"feature vector dimension {len(vector)} does not match weights {len(weights)}"
        )
    terms = [weights[index] * float(vector[index]) for index in range(len(weights))]
    return round(math.fsum(terms), precision)


def score_pair(feature: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    precision = config["score_precision"]
    chosen_score = score_from_vector(
        feature["vectors"]["chosen"], config["weights"], precision
    )
    rejected_score = score_from_vector(
        feature["vectors"]["rejected"], config["weights"], precision
    )
    reference_delta = chosen_score - rejected_score
    reward_delta = round(reference_delta, precision)
    threshold = float(config["reward_delta_threshold"])
    absolute_delta = abs(reward_delta)
    is_boundary = absolute_delta < threshold
    if absolute_delta < threshold:
        threshold_relation = "below"
    elif absolute_delta == threshold:
        threshold_relation = "equal"
    else:
        threshold_relation = "above"

    return {
        "pair_id": feature["pair_id"],
        "chosen_score": chosen_score,
        "rejected_score": rejected_score,
        "reward_delta": reward_delta,
        "reference_delta_double": reference_delta,
        "threshold": threshold,
        "threshold_relation": threshold_relation,
        "is_boundary": is_boundary,
        "decision": "boundary" if is_boundary else "clear",
        "score_precision": precision,
        "feature_cache_key": feature["cache_key"],
        "task_sort_key": stable_task_key(feature["pair_id"], "scoring"),
    }


def _failure(pair_id: str, batch_id: str, stage: str, code: str, exc: BaseException) -> dict[str, Any]:
    return {
        "pair_id": pair_id,
        "batch_id": batch_id,
        "stage": stage,
        "business_code": code,
        "exception_type": type(exc).__name__,
        "reason": str(exc),
        "source": None,
    }


def score_many(
    features: dict[str, dict[str, Any]],
    config: dict[str, Any],
    batch_id: str,
    fault_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    fault_map = fault_map or {}
    results: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []

    def worker(pair_id: str) -> tuple[str, dict[str, Any] | None, BaseException | None]:
        try:
            if fault_map.get(pair_id) == "scoring":
                raise RuntimeError("injected scoring-stage exception")
            result = score_pair(features[pair_id], config)
            if fault_map.get(pair_id) == "discrimination":
                raise RuntimeError("injected discrimination-stage exception")
            return pair_id, result, None
        except BaseException as exc:
            return pair_id, None, exc

    ordered_ids = sorted(features)
    if config["concurrency"] == 1 or len(ordered_ids) <= 1:
        completed = [worker(pair_id) for pair_id in ordered_ids]
    else:
        completed = []
        with ThreadPoolExecutor(max_workers=config["concurrency"]) as executor:
            futures = {
                executor.submit(worker, pair_id): pair_id for pair_id in ordered_ids
            }
            for future in as_completed(futures):
                completed.append(future.result())

    for pair_id, result, error in sorted(completed, key=lambda item: item[0]):
        if error is not None:
            stage = fault_map.get(pair_id)
            if stage not in ("scoring", "discrimination"):
                stage = "scoring"
            code = "E_DISCRIMINATION_ERROR" if stage == "discrimination" else "E_SCORING_ERROR"
            failures.append(_failure(pair_id, batch_id, stage, code, error))
        elif result is not None:
            results[pair_id] = result

    return {
        "entries": dict(sorted(results.items())),
        "failures": sorted(
            failures,
            key=lambda item: (item["pair_id"], item["stage"], item["business_code"]),
        ),
    }
