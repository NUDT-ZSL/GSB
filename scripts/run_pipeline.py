#!/usr/bin/env python3
"""Run (or resume) the four-stage deterministic pipeline.

Hard-kill demo hooks (environment):
  XLPIPE_KILL_AT="score:40"        kill -9 style exit mid-stage
  XLPIPE_KILL_BEFORE_COMMIT=align  kill right before the commit
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xlpipe.config import PipelineConfig
from xlpipe.errors import PipelineError
from xlpipe.pipeline import Pipeline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data/pairs.jsonl")
    parser.add_argument("--resources", default="resources")
    parser.add_argument("--config", default="data/config.json")
    parser.add_argument("--state", default="state")
    args = parser.parse_args()
    cfg = PipelineConfig.load(args.config)
    try:
        pipeline = Pipeline(args.data, args.resources, cfg, args.state)
        events = pipeline.run()
    except PipelineError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    for event in events:
        print(json.dumps(event, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
