"""blocksnap command line interface (runs on Windows cmd / PowerShell / bash)."""
from __future__ import annotations

import argparse
import json
import sys

from .audit import run_audit
from .errors import BlockSnapError
from .logging_utils import query_logs
from .manager import SnapshotManager, open_store
from .rollback import (
    DEFAULT_THRESHOLD,
    PATH_BITMAP,
    PATH_CHAIN,
    PATH_FULL,
    rollback,
)
from .volume import Volume


def _print(payload) -> None:
    print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))


def _manager(args) -> SnapshotManager:
    return open_store(args.image, args.store)


def cmd_create_volume(args) -> int:
    volume = Volume.create(args.image, args.block_size, args.block_count)
    payload = {
        "image": volume.image_path,
        "block_size": volume.block_size,
        "block_count": volume.block_count,
        "image_digest": volume.current_digest(),
    }
    volume.close()
    _print(payload)
    return 0


def cmd_init_store(args) -> int:
    with _manager(args) as manager:
        _print({
            "store": manager.store_dir,
            "baseline_object": manager.manifest.baseline_object,
            "format_version": manager.manifest.format_version,
        })
    return 0


def cmd_inject(args) -> int:
    with _manager(args) as manager:
        manager.inject_hex(args.block_index, args.hex)
        _print({
            "block_index": args.block_index,
            "image_digest": manager.volume.current_digest(),
        })
    return 0


def cmd_snapshot(args) -> int:
    with _manager(args) as manager:
        entry = manager.create_snapshot(args.name)
        _print(entry.to_dict())
    return 0


def cmd_list(args) -> int:
    with _manager(args) as manager:
        _print([entry.to_dict() for entry in manager.list_snapshots()])
    return 0


def cmd_rollback(args) -> int:
    with _manager(args) as manager:
        report = rollback(
            manager,
            args.snapshot,
            path=args.path,
            threshold=args.threshold,
            crash_after_prepare=args.crash_after_prepare,
        )
        _print(report)
    return 0


def cmd_recover(args) -> int:
    with _manager(args) as manager:
        result = manager.recover_pending()
        _print({"recovered": result is not None, "result": result})
    return 0


def cmd_audit(args) -> int:
    with _manager(args) as manager:
        conclusion = run_audit(manager)
        _print(conclusion)
    return 0


def cmd_manifest(args) -> int:
    with _manager(args) as manager:
        _print(manager.manifest.to_dict())
    return 0


def cmd_replace_manifest(args) -> int:
    from .manifest import Manifest, load_manifest

    with open(args.bundle, "r", encoding="utf-8") as handle:
        bundle = json.load(handle)
    new_manifest = Manifest.from_dict(bundle["manifest"])
    objects = {key: bytes.fromhex(value) for key, value in
               bundle.get("objects_hex", {}).items()}
    with _manager(args) as manager:
        report = manager.replace_manifest(new_manifest, objects)
        _print(report)
    return 0


def cmd_logs(args) -> int:
    records = query_logs(args.store, code=args.code, event=args.event)
    _print(records)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blocksnap",
                                     description="Local CoW snapshot & rollback tool")
    parser.add_argument("--image", help="volume image path")
    parser.add_argument("--store", help="snapshot store directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-volume", help="create a deterministic sample volume")
    p.add_argument("--block-size", type=int, required=True,
                   choices=sorted(__import__("blocksnap.volume", fromlist=["x"]).
                                  SUPPORTED_BLOCK_SIZES))
    p.add_argument("--block-count", type=int, required=True)
    p.set_defaults(func=cmd_create_volume)

    p = sub.add_parser("init-store", help="initialise the snapshot store")
    p.set_defaults(func=cmd_init_store)

    p = sub.add_parser("inject", help="inject a deterministic hex change")
    p.add_argument("--block-index", type=int, required=True)
    p.add_argument("--hex", required=True, help="hex payload, e.g. deadbeef")
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("snapshot", help="create a named CoW snapshot")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("list", help="list snapshots")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("rollback", help="roll the volume back to a snapshot")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--path", choices=[PATH_BITMAP, PATH_FULL, PATH_CHAIN],
                   default=None)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--crash-after-prepare", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("recover", help="recover an interrupted rollback")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("audit", help="run the chain consistency audit")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("manifest", help="print the current manifest")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("replace-manifest",
                       help="whole-replace the manifest from a bundle")
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_replace_manifest)

    p = sub.add_parser("logs", help="query structured business logs")
    p.add_argument("--code", default=None)
    p.add_argument("--event", default=None)
    p.set_defaults(func=cmd_logs)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BlockSnapError as exc:
        store = getattr(args, "store", None)
        if store:
            from .logging_utils import log_error
            log_error(store, "cli.business_error", exc)
        _print({"error": exc.to_record()})
        return 2
    except Exception as exc:  # noqa: BLE001
        _print({"error": {"code": "E_UNKNOWN", "message": str(exc),
                          "context": {}}})
        return 3


if __name__ == "__main__":
    sys.exit(main())
