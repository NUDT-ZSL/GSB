"""Repeatable JSON command-line interface for the delayed queue."""

import argparse
import json
import sys

from .config import QueueConfig
from .errors import DQError, exit_status_for
from .queue import DelayedQueue


def _print(obj, status=0):
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2,
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return status


def _load_config(path):
    if not path:
        return QueueConfig()
    with open(path, "r", encoding="utf-8") as fh:
        return QueueConfig.from_dict(json.load(fh))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="dqueue",
        description="Local deterministic delayed-queue middleware")
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--config", help="optional queue config JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("enqueue", help="validate and persist a task manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--policies", required=True)
    p.add_argument("--now", required=True, type=int)

    p = sub.add_parser("scan", help="trigger due tasks at a logical clock")
    p.add_argument("--now", required=True, type=int)

    p = sub.add_parser("poll", help="lease due tasks of a topic")
    p.add_argument("--topic", required=True)
    p.add_argument("--consumer", required=True)
    p.add_argument("--now", required=True, type=int)
    p.add_argument("--max", dest="max_items", type=int, default=100)

    p = sub.add_parser("ack", help="commit success/failure for a pull")
    p.add_argument("--pull-id", required=True)
    p.add_argument("--consumer", required=True)
    p.add_argument("--now", required=True, type=int)
    p.add_argument("--success", action="store_true")
    p.add_argument("--failure", action="store_true")
    p.add_argument("--error-code", default=None)

    p = sub.add_parser("resume", help="crash recovery replay")
    p.add_argument("--now", required=True, type=int)

    sub.add_parser("status", help="stable-sorted JSON state report")
    sub.add_parser("verify", help="verify state against manifest snapshot")

    p = sub.add_parser("mode", help="force a scheduler mode/version")
    p.add_argument("--set", dest="mode", choices=["heap", "wheel"],
                   required=True)
    p.add_argument("--now", required=True, type=int)
    p.add_argument("--version", type=int, default=None)
    return parser


def run(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    config = _load_config(args.config)
    queue = None
    try:
        # Validate inputs before opening/creating any database file, so a
        # rejected manifest leaves zero persistent state behind.
        if args.command == "enqueue":
            from .manifest import load_files
            load_files(args.manifest, args.policies)
        queue = DelayedQueue(args.db, config)
        if args.command == "enqueue":
            result = queue.enqueue_files(
                args.manifest, args.policies, args.now)
        elif args.command == "scan":
            result = queue.scan(args.now)
        elif args.command == "poll":
            result = queue.poll(args.topic, args.consumer, args.now,
                                args.max_items)
        elif args.command == "ack":
            if args.success == args.failure:
                raise DQError("E_PARSE",
                              "exactly one of --success/--failure is required")
            result = queue.ack(args.pull_id, args.consumer, args.success,
                               args.now, args.error_code)
        elif args.command == "resume":
            result = queue.resume(args.now)
        elif args.command == "status":
            result = queue.status()
        elif args.command == "verify":
            result = queue.verify()
        elif args.command == "mode":
            result = queue.set_mode(args.mode, args.now, args.version)
        else:  # pragma: no cover
            parser.error("unknown command")
        return _print(result)
    except DQError as exc:
        return _print(exc.to_dict(), exit_status_for(exc.code))
    finally:
        if queue is not None:
            queue.close()


def main():  # pragma: no cover
    sys.exit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
