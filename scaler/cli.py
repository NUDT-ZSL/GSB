"""Repeatable command line interface: load, reconcile, resume, status, verify."""

import argparse
import glob
import json
import os
import sys

from .errors import ScalerError
from .merger import PolicyMerger
from .orchestrator import Context, Orchestrator, shard_hash_of
from .parser import (
    load_json,
    parse_config,
    parse_load,
    parse_nodes,
    parse_services,
    parse_v1,
    parse_v2_shard,
    read_v2_metadata,
    validate_shard_order,
)


def _emit(payload, exit_code=0):
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return exit_code


def build_context(sample_dir):
    services = parse_services(load_json(
        os.path.join(sample_dir, "services.json")
    ))
    nodes = parse_nodes(load_json(os.path.join(sample_dir, "nodes.json")))
    v1 = parse_v1(load_json(os.path.join(sample_dir, "v1_policies.json")))
    metadata = read_v2_metadata(os.path.join(sample_dir, "v2_meta.db"))
    shard_paths = sorted(glob.glob(os.path.join(sample_dir, "v2_shard_*.json")))
    shards = [
        parse_v2_shard(load_json(path), metadata) for path in shard_paths
    ]
    validate_shard_order(shards)
    load_trace = parse_load(load_json(
        os.path.join(sample_dir, "load_trace.json")
    ))
    config = parse_config(load_json(os.path.join(sample_dir, "config.json")))
    manifest_path = os.path.join(sample_dir, "manifest.json")
    manifest = load_json(manifest_path) if os.path.exists(manifest_path) else {}
    injected_clock = manifest.get("injected_clock_ms", 0)
    merger = PolicyMerger(services, v1, shards, injected_clock)
    unified = merger.merge()
    version = manifest.get("version", "0.0.0")
    return Context(services, nodes, unified, load_trace, config, version), manifest


def _load(orch, sample_dir):
    context, manifest = build_context(sample_dir)
    hashes = [shard["shard_hash"] for shard in manifest.get("shards", [])]
    if not hashes:
        hashes = [shard_hash_of(load_json(path)) for path in sorted(
            glob.glob(os.path.join(sample_dir, "v2_shard_*.json"))
        )]
    result = orch.load_package(
        context,
        manifest.get("version", "0.0.0"),
        manifest.get("generation_floor", 0),
        hashes,
    )
    return _emit(result)


def _reconcile(orch, sample_dir, failure_domain):
    context, _ = build_context(sample_dir)
    result = orch.reconcile(context, failure_domain=failure_domain)
    return _emit(result)


def _resume(orch, sample_dir):
    context, _ = build_context(sample_dir)
    return _emit(orch.resume(context))


def _status(orch, sample_dir):
    context, _ = build_context(sample_dir)
    return _emit(orch.status(context))


def _verify(orch, sample_dir):
    context, _ = build_context(sample_dir)
    result = orch.verify(context)
    return _emit(result, 0 if result["ok"] else 2)


def _patch(orch, sample_dir, patch_path):
    context, _ = build_context(sample_dir)
    with open(patch_path, "r", encoding="utf-8") as handle:
        patch_payload = json.load(handle)
    return _emit(orch.apply_patch(context, patch_payload))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="scaler",
        description="Local platform upgrade scaling decision component",
    )
    parser.add_argument("--db", default="scaler_state.db",
                        help="SQLite state database path")
    parser.add_argument("--samples", default="samples/data",
                        help="directory containing generated JSON samples")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("load", help="validate and register a strategy package")
    reconcile_parser = sub.add_parser(
        "reconcile", help="run one bounded parallel reconcile round"
    )
    reconcile_parser.add_argument("--failure-domain", default=None,
                                  help="limit processing to one failure domain")
    sub.add_parser("resume", help="replay pending actions after a crash")
    sub.add_parser("status", help="stable sorted JSON status")
    sub.add_parser("verify", help="recompute and compare durable state")
    patch_parser = sub.add_parser(
        "patch", help="compute incremental invalidation closure"
    )
    patch_parser.add_argument("patch_file", help="JSON patch document")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    orch = Orchestrator(args.db)
    try:
        if args.command == "load":
            return _load(orch, args.samples)
        if args.command == "reconcile":
            return _reconcile(orch, args.samples, args.failure_domain)
        if args.command == "resume":
            return _resume(orch, args.samples)
        if args.command == "status":
            return _status(orch, args.samples)
        if args.command == "verify":
            return _verify(orch, args.samples)
        if args.command == "patch":
            return _patch(orch, args.samples, args.patch_file)
    except ScalerError as exc:
        return _emit(exc.to_dict(), exit_code=1)
    finally:
        orch.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
