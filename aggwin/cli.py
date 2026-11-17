"""Command line entry point and structured JSON logging.

Fault injection never edits source code; it only changes parameters or the
on-disk snapshot file:

* ``--inject-mid-window``  - report E_BATCH when a batch boundary lands while
  a window frame is still open;
* ``--ooo-ratio-limit``    - override the declared table limit so an out-of-
  order row trips E_ORDER;
* ``--inject-corrupt-snapshot`` - truncate the newest snapshot after the next
  checkpoint so resume fails with E_SNAPSHOT.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .errors import BusinessError, EXIT_CODES
from .pipeline import StreamingEngine, report_to_dict, write_results
from .queries import load_queries
from .schema import load_tables
from .verifier import assert_identical_results, verify


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "event": record.getMessage(),
        }
        if hasattr(record, "business_code"):
            payload["code"] = record.business_code
        if hasattr(record, "context") and record.context:
            payload["context"] = record.context
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _configure_logging(log_path: str | None) -> logging.Logger:
    logger = logging.getLogger("aggwin")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(JsonLineFormatter())
        logger.addHandler(file_handler)
    return logger


def _apply_overrides(tables, args) -> None:
    if args.ooo_ratio_limit is not None:
        for table in tables.values():
            table.ooo_ratio_limit = float(args.ooo_ratio_limit)


def _corrupt_hook_factory(enabled: bool, logger: logging.Logger):
    if not enabled:
        return None

    def hook(snapshot_name: str, event: dict, engine: StreamingEngine) -> None:
        path = engine.store.path_for(snapshot_name)
        engine.store.corrupt(path)
        logger.warning(
            "injected snapshot corruption",
            extra={"business_code": "E_SNAPSHOT",
                   "context": {"snapshot": snapshot_name}},
        )

    return hook


def _run(args) -> int:
    logger = _configure_logging(args.log_file)
    tables = load_tables(args.tables)
    _apply_overrides(tables, args)
    plan = load_queries(args.queries, tables)
    batch_sizes = {}
    if args.batch_size:
        batch_sizes = {name: args.batch_size for name in tables}
    engine = StreamingEngine(
        tables, plan, args.snapshot_dir,
        batch_sizes=batch_sizes,
        checkpoint_hook=_corrupt_hook_factory(
            args.inject_corrupt_snapshot, logger),
    )
    try:
        engine.restore()
    except BusinessError as exc:
        logger.error(exc.message, extra={"business_code": exc.code,
                                         "context": exc.context})
        _emit_error(args, exc)
        return EXIT_CODES[exc.code]
    if engine.replaced_state:
        logger.warning(
            "snapshot parameter version mismatch; whole state replaced",
            extra={"business_code": "E_SNAPSHOT",
                   "context": {"params_digest": engine.params_digest}},
        )
    try:
        report = engine.run()
    except BusinessError as exc:
        logger.error(exc.message, extra={"business_code": exc.code,
                                         "context": exc.context})
        _emit_error(args, exc)
        return EXIT_CODES[exc.code]

    write_results(args.result_dir, report)
    document = report_to_dict(report)
    with open(os.path.join(args.result_dir, "report.json"),
              "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True,
                  ensure_ascii=False, indent=2)
        handle.write("\n")

    for event in report.batch_events:
        if event["code"] == "E_BATCH" and args.inject_mid_window:
            logger.warning(
                "batch boundary landed inside an open window frame",
                extra={"business_code": "E_BATCH", "context": event},
            )
    print(json.dumps({
        "status": "OK",
        "replaced_state": report.replaced_state,
        "cursors": report.cursors,
        "checkpoints": report.checkpoints,
        "batch_events": report.batch_events,
        "result_dir": os.path.abspath(args.result_dir),
    }, sort_keys=True, indent=2))
    if args.strict_batch and any(
            event["code"] == "E_BATCH" for event in report.batch_events):
        return EXIT_CODES["E_BATCH"]
    return 0


def _emit_error(args, exc: BusinessError) -> None:
    if args.result_dir:
        os.makedirs(args.result_dir, exist_ok=True)
        with open(os.path.join(args.result_dir, "error.json"),
                  "w", encoding="utf-8") as handle:
            json.dump(exc.to_dict(), handle, sort_keys=True,
                      ensure_ascii=False, indent=2)
            handle.write("\n")
    print(json.dumps(exc.to_dict(), sort_keys=True, indent=2))


def _verify(args) -> int:
    result = verify(args.tables, args.queries, args.result_dir,
                    args.snapshot_dir)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["overall_status"] == "PASS" else EXIT_CODES["E_DIVERGE"]


def _compare(args) -> int:
    outcomes = []
    for name in ("report.json", "aggregates.json", "windows.json", "trace.json"):
        outcomes.append(assert_identical_results(
            os.path.join(args.left, name), os.path.join(args.right, name)))
    print(json.dumps({"status": "PASS", "byte_identical": outcomes},
                     sort_keys=True, indent=2))
    return 0


def _gui(args) -> int:
    from .gui import launch_gui

    launch_gui(args.tables, args.queries, args.result_dir, args.snapshot_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aggwin",
        description="Aggregation/window streaming pipeline (stdlib only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute the streaming pipeline")
    run.add_argument("--tables", required=True)
    run.add_argument("--queries", required=True)
    run.add_argument("--snapshot-dir", required=True)
    run.add_argument("--result-dir", required=True)
    run.add_argument("--batch-size", type=int, default=None)
    run.add_argument("--log-file", default=None)
    run.add_argument("--inject-mid-window", action="store_true")
    run.add_argument("--inject-corrupt-snapshot", action="store_true")
    run.add_argument("--ooo-ratio-limit", type=float, default=None)
    run.add_argument("--strict-batch", action="store_true")
    run.set_defaults(func=_run)

    verify_cmd = sub.add_parser("verify", help="independent verification")
    verify_cmd.add_argument("--tables", required=True)
    verify_cmd.add_argument("--queries", required=True)
    verify_cmd.add_argument("--snapshot-dir", required=True)
    verify_cmd.add_argument("--result-dir", required=True)
    verify_cmd.set_defaults(func=_verify)

    compare = sub.add_parser(
        "compare", help="byte-level comparison of two result directories")
    compare.add_argument("--left", required=True)
    compare.add_argument("--right", required=True)
    compare.set_defaults(func=_compare)

    gui = sub.add_parser("gui", help="launch the Tkinter inspection UI")
    gui.add_argument("--tables", required=True)
    gui.add_argument("--queries", required=True)
    gui.add_argument("--snapshot-dir", required=True)
    gui.add_argument("--result-dir", required=True)
    gui.set_defaults(func=_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True, indent=2))
        return EXIT_CODES.get(exc.code, 1)


if __name__ == "__main__":
    raise SystemExit(main())
