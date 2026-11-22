"""Command line entry point.

Usage:
    python -m quant_conv_pipeline.cli gen-samples --out data
    python -m quant_conv_pipeline.cli run --config config.json --data data
    python -m quant_conv_pipeline.cli commit --config config.json
        --updates data/updates/weight_params_v2.jsonl --kind weight
    python -m quant_conv_pipeline.cli verify --config config.json
        --snapshot snap-0002
    python -m quant_conv_pipeline.cli check-result --config config.json
        --snapshot snap-0001 --job job-002
    python -m quant_conv_pipeline.cli replay --config config.json --key KEY
    python -m quant_conv_pipeline.cli report --config config.json
        --snapshot snap-0002

All commands print JSON. Business errors print a structured error object
and exit with status 2.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import nodes as dag
from .config import Config
from .errors import PipelineError
from .pipeline import Pipeline
from .sample_gen import generate, read_jsonl
from .verify import verify_snapshot


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _load_pipeline(args):
    cfg = Config.load(args.config) if args.config else Config()
    return Pipeline(cfg)


def _cmd_gen_samples(args):
    jobs = generate(args.out, n_jobs=args.n_jobs)
    _emit({"status": "ok", "out": args.out, "jobs": len(jobs)})


def _compact_invalidation(inv):
    return {"kind": inv["kind"], "affected_nodes": inv["affected_nodes"],
            "affected_jobs": inv["affected_jobs"],
            "recompute_count": len(inv["recompute"]),
            "reuse_count": len(inv["reuse"])}


def _cmd_run(args):
    pipe = _load_pipeline(args)
    jobs = read_jsonl(f"{args.data}/jobs.jsonl")
    wp = read_jsonl(f"{args.data}/weight_params.jsonl")
    cal = read_jsonl(f"{args.data}/calib_stats.jsonl")
    result = pipe.full_run(jobs, wp, cal)
    _emit({"status": "ok", "snapshot_id": result["snapshot_id"],
           "jobs": sorted(result["lineage"].keys()),
           "validation_errors": result["validation_errors"],
           "tolerance_failures": result["tolerance_failures"],
           "invalidation": _compact_invalidation(result["invalidation"])})


def _cmd_commit(args):
    pipe = _load_pipeline(args)
    rows = read_jsonl(args.updates)
    result = pipe.commit(rows, args.kind)
    _emit({"status": "ok", "snapshot_id": result["snapshot_id"],
           "invalidation": result["invalidation"],
           "validation_errors": result["validation_errors"],
           "degradations": result["degradations"],
           "tolerance_failures": result["tolerance_failures"]})


def _cmd_verify(args):
    pipe = _load_pipeline(args)
    report = verify_snapshot(pipe, args.snapshot)
    _emit({"status": "ok", **report})


def _cmd_check_result(args):
    pipe = _load_pipeline(args)
    _emit({"status": "ok", **pipe.check_result(args.snapshot, args.job)})


def _cmd_replay(args):
    pipe = _load_pipeline(args)
    artifact, ok = dag.replay(pipe.cache, args.key)
    _emit({"status": "ok", "key": args.key, "matches_stored_hash": ok,
           "arrays": {name: {"shape": list(a.shape), "dtype": str(a.dtype)}
                      for name, a in artifact.items()}})


def _cmd_report(args):
    pipe = _load_pipeline(args)
    snap = pipe.load_snapshot(args.snapshot)
    _emit({"status": "ok", "snapshot_id": args.snapshot,
           "invalidation_report": snap["invalidation_report"],
           "deviation_spectrum": snap["deviation_spectrum"],
           "validation_errors": snap["validation_errors"]})


def build_parser():
    p = argparse.ArgumentParser(prog="quant-conv-pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_cfg(sp):
        sp.add_argument("--config", default=None,
                        help="pipeline config JSON (cache/snapshot/state "
                             "dirs, limits, tolerances)")

    sp = sub.add_parser("gen-samples")
    sp.add_argument("--out", required=True)
    sp.add_argument("--n-jobs", type=int, default=12)
    sp.set_defaults(fn=_cmd_gen_samples)

    sp = sub.add_parser("run")
    add_cfg(sp)
    sp.add_argument("--data", required=True)
    sp.set_defaults(fn=_cmd_run)

    sp = sub.add_parser("commit")
    add_cfg(sp)
    sp.add_argument("--updates", required=True)
    sp.add_argument("--kind", choices=("weight", "calib"), required=True)
    sp.set_defaults(fn=_cmd_commit)

    sp = sub.add_parser("verify")
    add_cfg(sp)
    sp.add_argument("--snapshot", required=True)
    sp.set_defaults(fn=_cmd_verify)

    sp = sub.add_parser("check-result")
    add_cfg(sp)
    sp.add_argument("--snapshot", required=True)
    sp.add_argument("--job", required=True)
    sp.set_defaults(fn=_cmd_check_result)

    sp = sub.add_parser("replay")
    add_cfg(sp)
    sp.add_argument("--key", required=True)
    sp.set_defaults(fn=_cmd_replay)

    sp = sub.add_parser("report")
    add_cfg(sp)
    sp.add_argument("--snapshot", required=True)
    sp.set_defaults(fn=_cmd_report)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except PipelineError as exc:
        _emit({"status": "error", **exc.to_dict()})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
