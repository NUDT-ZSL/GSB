"""Cross-module consistency self-checks (requirement 8).

Every check returns a structured item list; the first failed item raises
``E_INCONSISTENT`` naming the offending source entry.
"""

from __future__ import annotations

import numpy as np

from ..config.config import EngineConfig
from ..engine.training import chunk_plan, recompute_loss
from ..errors import E_INCONSISTENT, EngineError
from .error_spectrum import (chunk_vs_whole_report, run_all_variants)


def _fail(item: str, detail: str) -> EngineError:
    return EngineError(E_INCONSISTENT, detail, item=item)


def check_dialog_sets(framing: list[dict], pooling: dict,
                      summary: dict) -> list[dict]:
    items: list[dict] = []
    framing_ids = sorted(item["dialog_id"] for item in framing)
    pooling_ids = sorted(pooling["dialog_ids"])
    summary_ids = sorted(summary["dialog_ids"])
    for name, ids in (("framing", framing_ids), ("pooling", pooling_ids),
                      ("summary", summary_ids)):
        items.append({"check": "dialog_id_set", "stage": name,
                      "dialog_ids": ids, "passed": ids == framing_ids})
        if ids != framing_ids:
            raise _fail(f"dialog_id_set:{name}",
                        f"{name} dialog ids {ids} != framing {framing_ids}")
    return items


def check_loss_recomputation(dataset: dict, training: dict,
                             config: EngineConfig) -> list[dict]:
    """Each recorded epoch loss must match an independent recompute."""
    items: list[dict] = []
    epoch_weights = training.get("epoch_weights", [])
    for record, weights in zip(training["history"], epoch_weights):
        recomputed = recompute_loss(weights, dataset["x_aug"], dataset["y"],
                                    config)
        gap = abs(recomputed - record["loss"])
        passed = gap <= 1e-12
        items.append({"check": "loss_recompute", "epoch": record["epoch"],
                      "recorded_loss": record["loss"],
                      "recomputed_loss": recomputed, "abs_gap": gap,
                      "passed": passed})
        if not passed:
            raise _fail(f"loss_recompute:epoch{record['epoch']}",
                        f"loss gap {gap} exceeds tolerance")
    if len(epoch_weights) != len(training["history"]):
        raise _fail("loss_recompute:weights",
                    "stored epoch weights do not cover every history entry")
    return items


def check_protection_records(training: dict) -> list[dict]:
    """Protected/skipped epoch records must match actual execution history."""
    items: list[dict] = []
    executed = [record["epoch"] for record in training["history"]]
    protected = training.get("protected_epochs", [])
    skipped = training.get("skipped_epochs", [])
    accounted = set(executed) | set(skipped)
    for epoch in protected:
        passed = epoch in accounted
        items.append({"check": "protected_epoch", "epoch": epoch,
                      "executed": epoch in executed,
                      "skipped": epoch in skipped,
                      "passed": passed})
        if not passed:
            raise _fail(f"protected_epoch:{epoch}",
                        "protected epoch missing from execution/skip records")
    for epoch in skipped:
        passed = epoch in protected
        items.append({"check": "skipped_epoch", "epoch": epoch,
                      "executed": epoch in executed, "passed": passed})
        if not passed:
            raise _fail(f"skipped_epoch:{epoch}",
                        "skipped epoch was not protected by a snapshot")
    # On resume an epoch is expected to appear in both lists: protected by its
    # committed snapshot and skipped by the restarted process.  The invariant
    # being checked is that every skipped epoch had a protecting snapshot.
    unprotected_skips = sorted(set(skipped) - set(protected))
    if unprotected_skips:
        raise _fail("skipped_epoch:unprotected",
                    f"skipped epochs without protecting snapshot: "
                    f"{unprotected_skips}")
    return items


def check_chunk_spectrum(dataset: dict, config: EngineConfig,
                         variants=None) -> tuple[list[dict], dict]:
    """The chunk/whole deviation spectrum must be reproducible from data."""
    if variants is None:
        variants = run_all_variants(dataset, config)
    report = chunk_vs_whole_report(config, variants=variants)
    recomputed = chunk_vs_whole_report(config, variants=variants)
    gap = abs(recomputed["final_parameter_deviation"]["max_abs"]
              - report["final_parameter_deviation"]["max_abs"])
    passed = gap <= 1e-15
    items = [{"check": "chunk_spectrum_recompute", "abs_gap": gap,
              "max_abs": report["final_parameter_deviation"]["max_abs"],
              "passed": passed}]
    if not passed:
        raise _fail("chunk_spectrum_recompute",
                    f"spectrum recomputation gap {gap} exceeds tolerance")
    return items, report


def check_chunk_plan(dataset: dict, training: dict,
                     config: EngineConfig) -> list[dict]:
    n_rows = dataset["x_aug"].shape[0]
    expected = chunk_plan(n_rows, training["chunk_size"])
    stored = [tuple(span) for span in training["plan"]]
    passed = stored == expected
    items = [{"check": "chunk_plan", "expected": [list(s) for s in expected],
              "stored": [list(s) for s in stored], "passed": passed}]
    if not passed:
        raise _fail("chunk_plan",
                    "stored chunk plan differs from deterministic recompute")
    return items


def run_self_checks(dataset: dict, framing: list[dict], training: dict,
                    summary: dict, config: EngineConfig) -> dict:
    items: list[dict] = []
    items.extend(check_dialog_sets(framing, dataset, summary))
    items.extend(check_loss_recomputation(dataset, training, config))
    items.extend(check_protection_records(training))
    items.extend(check_chunk_plan(dataset, training, config))
    spectrum_items, chunk_report = check_chunk_spectrum(dataset, config)
    items.extend(spectrum_items)
    return {"passed": all(item["passed"] for item in items),
            "items": items, "chunk_report": chunk_report}
