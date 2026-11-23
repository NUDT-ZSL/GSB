"""Run the full pipeline; write pipeline_report.json + export.tsv."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mt_pipeline.errors import PipelineError
from mt_pipeline.pipeline import run_pipeline
from mt_pipeline.util import read_json, write_json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/pipeline_config.json")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--snapshots", default="snapshots")
    args = ap.parse_args()

    config = read_json(args.config)
    try:
        report = run_pipeline(config, args.data, args.out, args.snapshots)
    except PipelineError as exc:
        write_json(Path(args.out) / "pipeline_report.json",
                   {"business_codes": [exc.code], "error": exc.to_dict()})
        print(f"pipeline stopped with {exc.code}: {exc.message}")
        sys.exit(1)
    dec = report["decision_record"]["decision"]
    print(f"path={dec['path']} degradation={dec['degradation']} "
          f"kept={report['metrics']['kept']}/{report['metrics']['total']} "
          f"self_check={report['self_check']}")


if __name__ == "__main__":
    main()
