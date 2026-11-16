"""Command line entry point with an interactive workbench session."""

import argparse
import json
import os
import sys

from .chunker import Vocabulary
from .engine import Engine
from .errors import E_EVICT_POLICY_INVALID, WorkbenchError
from .hashing import EmbeddingEncoder, config_fingerprint
from .pipeline import load_json, run_stream
from .samplegen import generate_sample_set
from .snapshot import SnapshotStore


def default_paths(workdir):
    sample_dir = os.path.join(workdir, "samples")
    snapshot_dir = os.path.join(workdir, "runtime", "snapshots")
    report_path = os.path.join(workdir, "runtime", "reports", "report.json")
    return sample_dir, snapshot_dir, report_path


def cmd_generate(args):
    sample_dir, _, _ = default_paths(args.workdir)
    generate_sample_set(sample_dir)
    print(f"samples written to {sample_dir}")
    return 0


def cmd_run(args):
    sample_dir, snapshot_dir, report_path = default_paths(args.workdir)
    _, report = run_stream(
        sample_dir=sample_dir,
        report_path=report_path,
        snapshot_dir=snapshot_dir,
        run_id=args.run_id,
        mode=args.mode,
        clean=not args.resume,
    )
    print(json.dumps(_summary(report), sort_keys=True, indent=2))
    print(f"report: {report_path}")
    return 0


def _summary(report):
    return {
        "run_id": report["run_id"],
        "mode": report["mode"],
        "request_cursor": report["request_cursor"],
        "failures": len(report["failures"]),
        "event_counts": report["event_counts"],
        "peak_resident": report["residency"]["peak_resident_blocks"],
        "digest": report["chain_fingerprint_digest"],
        "snapshot_version": report["snapshot"]["version"],
    }


def cmd_verify(args):
    sample_dir, snapshot_dir, report_path = default_paths(args.workdir)
    config = load_json(os.path.join(sample_dir, "cache_config.json"))
    _, baseline = run_stream(
        sample_dir=sample_dir,
        report_path=os.path.join(
            args.workdir, "runtime", "reports", "baseline.json"
        ),
        snapshot_dir=os.path.join(
            args.workdir, "runtime", "snapshots_baseline"
        ),
        run_id="verify-baseline",
        mode="serial",
        clean=True,
        config_override={
            "residency_budget_blocks": 10_000,
            "session_retention_cap": 10_000,
        },
    )
    _, actual = run_stream(
        sample_dir=sample_dir,
        report_path=report_path,
        snapshot_dir=snapshot_dir,
        run_id="verify-actual",
        mode=args.mode,
        clean=True,
    )
    diff = compare_final_blocks(baseline, actual)
    print(json.dumps({"identical_final_blocks": not diff, "diff": diff},
                     sort_keys=True, indent=2))
    return 0 if not diff else 2


def compare_final_blocks(baseline, actual):
    base_map = {s["session"]: s for s in baseline["sessions"]}
    actual_map = {s["session"]: s for s in actual["sessions"]}
    diff = []
    for session in sorted(set(base_map) | set(actual_map)):
        if base_map.get(session, {}).get("chain_fps") != actual_map.get(
            session, {}
        ).get("chain_fps"):
            diff.append(session)
    if baseline["chain_fingerprint_digest"] != actual[
        "chain_fingerprint_digest"
    ]:
        diff.append("chain_fingerprint_digest")
    return diff


def interactive(workdir):
    sample_dir, snapshot_dir, report_path = default_paths(workdir)
    config = load_json(os.path.join(sample_dir, "cache_config.json"))
    vocab_rules = load_json(os.path.join(sample_dir, "vocab_rules.json"))
    vocab = Vocabulary(vocab_rules)
    encoder = EmbeddingEncoder(vocab_rules)
    engine = Engine(config, vocab, encoder)
    _, snapshot_dir, _ = default_paths(workdir)
    store = SnapshotStore(snapshot_dir)
    if store.exists():
        store.restore_or_rebuild(engine)
    print("interactive prefix-cache workbench")
    print(
        "commands: policy <LRU|LFU> | budget <int> | "
        "chain <chain_fp> | replay | report | help | exit"
    )
    while True:
        try:
            line = input("pcw> ").strip()
        except EOFError:
            break
        if not line:
            continue
        parts = line.split()
        command = parts[0]
        if command in ("exit", "quit"):
            break
        if command == "help":
            print(
                "policy <LRU|LFU>: switch eviction policy\n"
                "budget <int>: adjust residency budget\n"
                "chain <fingerprint>: inspect dependency chain\n"
                "replay: rerun stream and print report path\n"
                "report: print current config hashes and snapshot identity"
            )
        elif command == "policy" and len(parts) == 2:
            if parts[1] not in ("LRU", "LFU"):
                print(E_EVICT_POLICY_INVALID)
                continue
            config["eviction_policy"] = parts[1]
            engine = Engine(config, vocab, encoder)
            print(
                f"policy={parts[1]} new_config_fp={config_fingerprint(config)}"
            )
        elif command == "budget" and len(parts) == 2 and parts[1].isdigit():
            config["residency_budget_blocks"] = int(parts[1])
            engine = Engine(config, vocab, encoder)
            print(
                f"budget={parts[1]} new_config_fp="
                f"{config_fingerprint(config)}"
            )
        elif command == "chain" and len(parts) == 2:
            view = engine.chain_dependency_view(parts[1])
            print(json.dumps(view, sort_keys=True, indent=2))
        elif command == "replay":
            _, report = run_stream(
                sample_dir=sample_dir,
                report_path=report_path,
                snapshot_dir=snapshot_dir,
                run_id="interactive-replay",
                mode="concurrent",
                clean=True,
                config_override=dict(config),
            )
            print(f"report: {report_path} digest={report['chain_fingerprint_digest']}")
        elif command == "report":
            print(
                json.dumps(
                    {
                        "config_fp": config_fingerprint(config),
                        "policy": config["eviction_policy"],
                        "budget": config["residency_budget_blocks"],
                    },
                    sort_keys=True,
                )
            )
        else:
            print("unknown command; type help")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="prefix-cache-workbench",
        description="Deterministic LLM prefix-cache management workbench",
    )
    parser.add_argument("--workdir", default=os.getcwd())
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="write deterministic samples")
    gen.set_defaults(func=cmd_generate)
    run = sub.add_parser("run", help="process the request stream")
    run.add_argument("--mode", choices=["serial", "concurrent"],
                     default="concurrent")
    run.add_argument("--run-id", default="run")
    run.add_argument("--resume", action="store_true")
    run.set_defaults(func=cmd_run)
    verify = sub.add_parser("verify", help="compare against empty-cache build")
    verify.add_argument("--mode", choices=["serial", "concurrent"],
                        default="concurrent")
    verify.set_defaults(func=cmd_verify)
    inter = sub.add_parser("interactive", help="interactive session")
    inter.set_defaults(func=lambda args: interactive(args.workdir))
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WorkbenchError as exc:
        print(json.dumps({"code": exc.code, "stage": exc.stage,
                          "message": exc.message}, sort_keys=True),
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
