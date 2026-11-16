"""Deterministic end-to-end demonstration.

Sequence:
  1. first evaluation on the bundled sample block;
  2. evaluation after deterministic scale-up;
  3. snapshot recovery and replay verification.
The summary is emitted in a fixed key/row order; timing fields are kept in the
full JSON report but excluded from the stable console summary.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vectorbench.logging_utils import StructuredLogger  # noqa: E402
from vectorbench.model import load_blocks, load_queries  # noqa: E402
from vectorbench.session import ExperimentSession, open_snapshot  # noqa: E402

DATA = os.path.join(ROOT, "samples", "data_blocks.json")
QUERIES = os.path.join(ROOT, "samples", "queries.json")
LOG = os.path.join(ROOT, "logs", "demo.jsonl")
SNAPSHOT = os.path.join(ROOT, "artifacts", "demo_snapshot.json")
REPORT = os.path.join(ROOT, "artifacts", "demo_report.json")
SCALE_FACTOR = 16


def _summary_line(evaluation) -> str:
    decision = evaluation.decision
    result = evaluation.selected.result_summary()
    return (
        f"q{evaluation.query_index:02d} | path={evaluation.selected.path:<5} "
        f"batch={decision.batch_size:<5} batches={decision.batch_count:<3} "
        f"depth={decision.scale.expression_depth:<2} type={result.type_name:<5} "
        f"nulls={result.null_count:<3} digest={result.digest[:12]} "
        f"reason={decision.reason_code}"
    )


def main() -> int:
    logger = StructuredLogger(LOG)
    block = load_blocks(DATA)[0]
    queries = load_queries(QUERIES, block)

    print("== Phase 1: first evaluation ==")
    first = ExperimentSession(block, queries, logger)
    first.evaluate_all()
    for evaluation in first.evaluations:
        print(_summary_line(evaluation))

    print("\n== Phase 2: scaled evaluation ==")
    scaled_block = block.scaled(SCALE_FACTOR)
    scaled = ExperimentSession(scaled_block, queries, logger)
    scaled.evaluate_all()
    for evaluation in scaled.evaluations:
        print(_summary_line(evaluation))
    with open(REPORT, "w", encoding="utf-8") as handle:
        json.dump(scaled.build_report(), handle, sort_keys=True, indent=2, ensure_ascii=False)
    scaled.snapshot(SNAPSHOT)

    print("\n== Phase 3: snapshot recovery replay ==")
    restored, replacement_mode = open_snapshot(SNAPSHOT, block, queries, logger)
    print(f"replacement_mode={replacement_mode}")
    for evaluation in restored.evaluations:
        print(_summary_line(evaluation))

    print("\n== Stable Audit Summary ==")
    for index, (left, right) in enumerate(zip(scaled.evaluations, restored.evaluations)):
        same_path = left.selected.path == right.selected.path
        same_digest = left.result_digest() == right.result_digest()
        print(f"q{index:02d} replay_path_match={same_path} replay_digest_match={same_digest}")
    print(f"log={LOG}")
    print(f"snapshot={SNAPSHOT}")
    print(f"report={REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
