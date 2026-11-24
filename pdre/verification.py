"""Deterministic order-independence and consistency verification helpers."""

from __future__ import annotations

import random
from typing import Any

from .config import EngineConfig
from .engine import analyze, compare_semantic_reports, semantic_digest
from .incremental import IncrementalEngine
from .pipeline import InputBundle


def run_shuffled_batches(
    bundle: InputBundle,
    *,
    iterations: int = 20,
    seed: int = 3820382,
    config: EngineConfig | None = None,
    verify_each_batch: bool = False,
) -> dict[str, Any]:
    config = config or EngineConfig()
    full_report = analyze(
        bundle.events,
        bundle.lineage,
        bundle.policies,
        config,
        rejections=bundle.rejections,
        read_errors=bundle.read_errors,
    )
    expected_digest = semantic_digest(full_report)
    tagged: list[tuple[str, Any]] = (
        [("event", item) for item in bundle.events]
        + [("lineage", item) for item in bundle.lineage]
        + [("policy", item) for item in bundle.policies]
    )

    failures: list[dict[str, Any]] = []
    rng = random.Random(seed)
    digests: list[str] = []

    for iteration in range(iterations):
        order = list(tagged)
        rng.shuffle(order)
        engine = IncrementalEngine(
            config=config,
            rejections=list(bundle.rejections),
            read_errors=list(bundle.read_errors),
        )
        position = 0
        batch_index = 0
        while position < len(order):
            size = rng.randint(1, 7)
            chunk = order[position : position + size]
            position += size
            events = [record for kind, record in chunk if kind == "event"]
            lineage = [record for kind, record in chunk if kind == "lineage"]
            policies = [record for kind, record in chunk if kind == "policy"]
            report = engine.apply_batch(
                batch_id=f"shuffle-{iteration:03d}-{batch_index:03d}",
                events=events,
                lineage=lineage,
                policies=policies,
                verify=verify_each_batch,
            )
            batch_index += 1
            if verify_each_batch and report.get("summary", {}).get("rejudge_mismatch_count"):
                failures.append(
                    {"iteration": iteration, "batch": batch_index, "differences": report["incremental"]["differences"]}
                )

        differences = compare_semantic_reports(engine.last_report, full_report)
        digest = semantic_digest(engine.last_report)
        digests.append(digest)
        if differences:
            failures.append({"iteration": iteration, "differences": differences})

    return {
        "iterations": iterations,
        "record_count": len(tagged),
        "expected_semantic_digest": expected_digest,
        "unique_observed_digests": sorted(set(digests)),
        "all_matched": not failures and len(set(digests)) == 1,
        "failure_count": len(failures),
        "failures": failures[:10],
    }
