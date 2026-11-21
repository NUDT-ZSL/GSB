"""Command line example: deterministic local sample -> JSON report.

Usage:
    python -m meeting_align.cli --workdir artifacts/demo [--config cfg.json] \
        [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import AlignConfig
from .errors import AlignError
from .lineage import replay_segment, verify_lineage
from .pipeline import Batch, Pipeline
from .synth import generate_sample_set, load_pcm
from .util import canonical_json


def load_batches(samples_dir, batch_b_file="batchB.pcm"):
    samples = Path(samples_dir)
    transcripts = json.loads((samples / "transcripts.json").read_text())
    manifest = json.loads((samples / "manifest.json").read_text())
    batches = {}
    for batch_id, fname in (("A", "batchA.pcm"), ("B", batch_b_file)):
        batches[batch_id] = Batch(
            batch_id=batch_id,
            pcm=load_pcm(samples / fname),
            declared_sample_rate=manifest["batches"]["A" if batch_id == "A" else "B"][
                "declared_sample_rate"
            ],
            segments=transcripts[batch_id]["segments"],
        )
    return batches


def build_report(workdir, config, out_path):
    workdir = Path(workdir)
    samples_dir = workdir / "samples"
    manifest = generate_sample_set(samples_dir)

    # baseline run
    baseline = Pipeline(workdir / "run_baseline", config)
    batches = load_batches(samples_dir)
    r1 = baseline.run(batches, ["A", "B"])

    # targeted invalidation: batch B PCM replaced by the v2 variant
    batches_v2 = load_batches(samples_dir, batch_b_file="batchB_v2.pcm")
    r2 = baseline.run(batches_v2, ["A", "B"], previous=r1)

    # from-scratch recompute with the same input history must be
    # byte-identical to the incrementally updated result
    fresh = Pipeline(workdir / "run_fresh", config)
    fresh.run(batches, ["A", "B"])
    r3 = fresh.run(batches_v2, ["A", "B"])
    keys = ("segments", "confidence", "calibration", "total_duration_ms",
            "applied_hop", "hashes", "versions")
    strip = lambda r: {k: r[k] for k in keys}
    identical = canonical_json(strip(r2)) == canonical_json(strip(r3))

    # lineage verification + replay of every global segment
    lineage_state = r2["lineage_state"]
    sr = config.sample_rate
    seams_global_ms = [
        c["sample_index"] / sr * 1000.0
        for c in r2["seams"]["global"]["candidates"]
        if c["kind"] == "splice"
    ]
    gaps_global_ms = [
        [g[0] / sr * 1000.0, g[1] / sr * 1000.0]
        for g in r2["seams"]["global"]["gaps"]
    ]
    originals = [s for b in batches_v2.values() for s in b.segments]
    replay_ok = 0
    for seg in r2["segments"]:
        verify_lineage(seg, lineage_state)
        replay_segment(seg, originals, r2["calibration"], seams_global_ms,
                       gaps_global_ms, config)
        replay_ok += 1

    report = {
        "business_code": r2["business_code"],
        "degradations": r2["degradations"],
        "sample_manifest": manifest,
        "versions": r2["versions"],
        "hashes": r2["hashes"],
        "applied_hop": r2["applied_hop"],
        "total_duration_ms": r2["total_duration_ms"],
        "seam_candidates": {
            "global": r2["seams"]["global"]["candidates"],
            "batches": {
                b: r2["seams"]["batches"][b]["candidates"]
                for b in r2["seams"]["batches"]
            },
        },
        "calibration": r2["calibration"],
        "segments": r2["segments"],
        "confidence": r2["confidence"],
        "invalidation": {
            "scenario": "replace_batch_B_pcm",
            "recompute": r2["invalidation"]["recompute"],
            "reuse": r2["invalidation"]["reuse"],
            "full_recompute_identical": identical,
        },
        "replay": {"segments_checked": replay_ok, "ok": True},
        "errors": [],
    }
    Path(out_path).write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default="artifacts/demo")
    parser.add_argument("--config", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    config = AlignConfig.load(args.config) if args.config else AlignConfig()
    out = args.out or str(Path(args.workdir) / "report.json")
    try:
        report = build_report(args.workdir, config, out)
    except AlignError as exc:
        report = {"business_code": exc.code, "errors": [exc.to_dict()]}
        Path(out).write_text(json.dumps(report, indent=1, ensure_ascii=False))
        print(json.dumps(report, indent=1, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "business_code": report["business_code"],
                "segments": len(report["segments"]),
                "recompute": report["invalidation"]["recompute"],
                "reuse": report["invalidation"]["reuse"],
                "full_recompute_identical": report["invalidation"][
                    "full_recompute_identical"
                ],
                "replay": report["replay"],
                "report": out,
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
