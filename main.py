"""CLI entry: full / incremental NER runs over a local sample directory."""

import argparse
import sys

from ner_component.engine import Engine


def build_parser():
    parser = argparse.ArgumentParser(description="deterministic NER component")
    parser.add_argument("mode", choices=["full", "incr"])
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--snapshot-dir", default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--edits", default="edits.json",
                        help="edits file name inside the sample dir")
    parser.add_argument("--batch-sleep-ms", type=int, default=None)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    engine = Engine(
        sample_dir=args.sample_dir,
        report_path=args.report,
        snapshot_dir=args.snapshot_dir,
        concurrency=args.concurrency,
        edits_name=args.edits,
        batch_sleep_ms=args.batch_sleep_ms,
    )
    if args.mode == "full":
        return engine.run_full()
    return engine.run_incremental()


if __name__ == "__main__":
    sys.exit(main())
