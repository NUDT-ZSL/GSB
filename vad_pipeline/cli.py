"""Command-line example: deterministic local VAD calibration demo.

Usage:
  python -m vad_pipeline.cli demo [--workdir DIR] [--jobs N] [--out FILE]
  python -m vad_pipeline.cli run --samples DIR --cache DIR [--out FILE]

`demo` synthesizes samples from fixed seeds, runs the pipeline with a base
configuration, recalibrates (percentile / threshold multiplier / min segment
length), replays lineage records, and emits one JSON document containing
frame decisions, segment lists, recompute/reuse manifests, lineage records
and business codes.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import replace
from pathlib import Path

from . import errors
from .cache import ArtifactCache
from .config import VadConfig
from .lineage import replay_frame_decision, replay_segment
from .pipeline import Pipeline
from .synth import load_sample_set, synth_sample_set

DEFAULT_CLIPS = [
    {"clip_id": "clip_a", "seed": 20240101, "duration_ms": 4000},
    {"clip_id": "clip_b", "seed": 20240102, "duration_ms": 5000},
    {"clip_id": "clip_c", "seed": 20240103, "duration_ms": 3000},
]


def _public_result(result):
    return {k: v for k, v in result.items() if k != "_artifacts"}


def _lineage_checks(cache, results):
    checks = []
    for res in results:
        lineage = res["lineage"]
        for frame_index in (0, res["n_frames"] // 2, res["n_frames"] - 1):
            ok = replay_frame_decision(cache, lineage, frame_index) == bool(
                res["decisions"][frame_index]
            )
            checks.append(
                {
                    "clip_id": res["clip_id"],
                    "kind": "frame",
                    "index": frame_index,
                    "ok": ok,
                }
            )
        for seg_index in range(len(res["segments"])):
            ok = replay_segment(cache, lineage, seg_index) == list(
                map(int, res["segments"][seg_index])
            )
            checks.append(
                {
                    "clip_id": res["clip_id"],
                    "kind": "segment",
                    "index": seg_index,
                    "ok": ok,
                }
            )
    return checks


def _tamper_check(cache, lineage):
    """Deliberately break a lineage record to demonstrate E_LINEAGE_BROKEN."""
    bad = copy.deepcopy(lineage)
    bad["components"]["decision"]["mult_high"] = 999.0
    try:
        replay_frame_decision(cache, bad, 0)
    except errors.VadError as exc:
        return {"expected_code": errors.E_LINEAGE_BROKEN, "observed": exc.to_dict()}
    return {"expected_code": errors.E_LINEAGE_BROKEN, "observed": None}


def cmd_demo(args):
    workdir = Path(args.workdir)
    samples_dir = workdir / "samples"
    cache_dir = workdir / "cache"
    clips = synth_sample_set(samples_dir, DEFAULT_CLIPS)

    cfg_a = VadConfig()
    cache = ArtifactCache(cache_dir, max_entries=cfg_a.max_cache_entries)
    pipe = Pipeline(cache, cfg_a)
    run = pipe.run_manifest(clips, jobs=args.jobs)
    results = run["results"]

    cfg_b = replace(
        cfg_a, percentile=35.0, mult_high=5.0, min_speech_ms=90, param_version="v2"
    )
    recalibrations = []
    for clip, res in zip(clips, results):
        report = pipe.recalibrate(clip, cfg_b, res)
        report["result"] = _public_result(report["result"])
        recalibrations.append(report)

    output = {
        "workdir": str(workdir),
        "config_base": cfg_a.to_dict(),
        "config_recalibrated": cfg_b.to_dict(),
        "clips": [_public_result(r) for r in results],
        "recalibrations": recalibrations,
        "lineage_checks": _lineage_checks(cache, results),
        "lineage_tamper_check": _tamper_check(cache, results[0]["lineage"]),
        "degraded": run["degraded"],
        "business_codes": [],
    }
    _emit(output, args.out)
    return 0


def cmd_run(args):
    clips = load_sample_set(args.samples)
    cfg = VadConfig()
    cache = ArtifactCache(args.cache, max_entries=cfg.max_cache_entries)
    pipe = Pipeline(cache, cfg)
    try:
        run = pipe.run_manifest(clips, jobs=args.jobs)
    except errors.VadError as exc:
        _emit({"business_codes": [exc.to_dict()]}, args.out)
        return 1
    output = {
        "clips": [_public_result(r) for r in run["results"]],
        "lineage_checks": _lineage_checks(cache, run["results"]),
        "degraded": run["degraded"],
        "business_codes": [],
    }
    _emit(output, args.out)
    return 0


def _emit(payload, out_path):
    text = json.dumps(payload, indent=1, sort_keys=False)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vad_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="synthesize samples and run the full example")
    p_demo.add_argument("--workdir", default="vad_demo")
    p_demo.add_argument("--jobs", type=int, default=1)
    p_demo.add_argument("--out", default=None)
    p_demo.set_defaults(func=cmd_demo)

    p_run = sub.add_parser("run", help="run the pipeline on an existing samples dir")
    p_run.add_argument("--samples", required=True)
    p_run.add_argument("--cache", required=True)
    p_run.add_argument("--jobs", type=int, default=1)
    p_run.add_argument("--out", default=None)
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
