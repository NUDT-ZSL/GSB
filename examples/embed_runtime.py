"""Minimal embedded-library example.

Run from the repository root:

    python examples/embed_runtime.py
"""

from __future__ import annotations

import argparse

from stream_reopt import RuntimeEngine, load_scenario


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="samples/adaptive_join_sample.json")
    parser.add_argument("--db", default="runtime/example/state.db")
    parser.add_argument("--blocks", default="runtime/example/blocks")
    args = parser.parse_args()
    scenario = load_scenario(args.sample)
    engine = RuntimeEngine(
        scenario,
        db_path=args.db,
        blocks_path=args.blocks,
    )
    try:
        result = engine.run(resume=False)
        print(
            {
                "complete": result.complete,
                "watermark": result.final_watermark,
                "shape_version": result.final_shape["version"],
                "join_order": result.final_shape["join_order"],
                "drift_triggers": len(result.drift_triggers),
                "suppressed": len(result.suppressed_requests),
                "decision_error_codes": [
                    decision["error_code"]
                    for decision in result.decisions
                    if decision.get("error_code")
                ],
            }
        )
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

