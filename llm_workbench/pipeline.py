"""End-to-end pipeline orchestration.

Stages: ingest -> validate -> admit -> execute -> snapshot -> selfcheck.
Each stage attributes its failures with business error codes; a simulated
forced termination is resumed from the latest committed checkpoint.
"""

from __future__ import annotations

import json
import os

from .errors import WorkbenchError
from .executor import Executor, SimulatedCrash
from .scheduler import admit
from .selfcheck import run_selfcheck
from .snapshots import SnapshotStore
from .validation import load_tasks_jsonl, load_vocab, validate_config


def run_pipeline(data_dir: str, work_dir: str, crash_after: int | None = None,
                 use_pool: bool = True) -> dict:
    os.makedirs(work_dir, exist_ok=True)
    errors: list[dict] = []

    vocab_path = os.path.join(data_dir, "vocab.json")
    try:
        vocab = load_vocab(vocab_path)
        vocab_size = len(vocab["tokens"])
    except WorkbenchError as err:
        # vocab is a hard dependency: record and stop before any sampling
        return {"ok": False, "errors": [err.to_dict()], "stage_reached": "vocab_load"}

    cfg_raw = {}
    cfg_path = os.path.join(data_dir, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg_raw = json.load(fh)
    try:
        cfg = validate_config(cfg_raw)
    except WorkbenchError as err:
        return {"ok": False, "errors": [err.to_dict()], "stage_reached": "config_validate"}

    outcome = load_tasks_jsonl(os.path.join(data_dir, "tasks.jsonl"))
    errors.extend(e.to_dict() for e in outcome.errors)
    all_input_ids = [r.request_id for r in outcome.requests] + [
        e.context.get("request_id") for e in outcome.errors if e.context.get("request_id")
    ]

    admission = admit(outcome.requests, cfg, vocab_size)

    def make_executor() -> Executor:
        return Executor(admission, vocab_size, work_dir,
                        max_slots=cfg["max_slots"], batch_size=cfg["batch_size"],
                        use_pool=use_pool)

    crashed = False
    try:
        run_report = make_executor().run(crash_after=crash_after)
    except SimulatedCrash:
        crashed = True
        # restart: a fresh executor resumes from the last committed checkpoint
        run_report = make_executor().run(crash_after=None)
    run_report["recovery"]["forced_termination_simulated"] = crashed

    store = SnapshotStore(os.path.join(work_dir, "snapshots"))
    snap_meta = {
        "artifacts": store.put_next("artifacts", run_report["results"]),
        "ledger": store.put_next("ledger", {
            "admission_ledger": admission.ledger,
            "budgets_consumed": run_report["budgets_consumed"],
        }),
        "decisions": store.put_next("decisions", {
            "rejected": admission.rejected,
            "downgraded": admission.downgraded,
            "arbitration": admission.arbitration,
        }),
    }

    check = run_selfcheck(all_input_ids, outcome.errors, admission, run_report, vocab_size)

    return {
        "ok": not errors and check["ok"],
        "stage_reached": "selfcheck",
        "config": cfg,
        "vocab_version": vocab["version"],
        "errors": errors,
        "admission": {
            "waves": admission.waves,
            "rejected": admission.rejected,
            "downgraded": admission.downgraded,
            "arbitration": admission.arbitration,
            "ledger": admission.ledger,
        },
        "run": {
            "committed_order": run_report["committed_order"],
            "budgets_consumed": run_report["budgets_consumed"],
            "recovery": run_report["recovery"],
            "metrics": run_report["metrics"],
        },
        "results": run_report["results"],
        "snapshots": snap_meta,
        "selfcheck": check,
    }
