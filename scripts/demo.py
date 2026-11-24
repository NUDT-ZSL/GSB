#!/usr/bin/env python3
"""Generate a sample, analyze it with several concurrency modes, and verify truth."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tlsdiag import AnalysisConfig, analyze_run, generate_sample, verify_sample


def main() -> int:
    work = ROOT / "work"
    sample = work / "sample"
    print(json.dumps(generate_sample(sample), indent=2, sort_keys=True))
    verified = verify_sample(sample)
    print(json.dumps(verified, indent=2, sort_keys=True))
    if not verified.get("ok"):
        return 1
    expected = json.loads((sample / "expected/expected.json").read_text(encoding="utf-8"))
    profile = expected["profiles"]["default"]
    digests = []
    for jobs in (1, 4):
        run = analyze_run(
            handshake_path=sample / profile["handshake"],
            v1_path=sample / profile["v1"],
            v2_shards=sample / profile["v2_shards"],
            v2_db=sample / profile["v2_db"],
            output_dir=work / f"out-jobs-{jobs}",
            config=AnalysisConfig(
                jobs=jobs,
                timeout_ms=profile["config"]["timeout_ms"],
                max_frags_per_connection=profile["config"]["max_frags_per_connection"],
                max_expanded_bytes_per_connection=profile["config"][
                    "max_expanded_bytes_per_connection"
                ],
                max_total_expanded_bytes=profile["config"]["max_total_expanded_bytes"],
            ),
        )
        digests.append(run.report["summary"]["conclusion_digest"])
        print(
            f"jobs={jobs} connections={run.report['summary']['connections_total']} "
            f"trusted={run.report['summary']['connections_trusted']} "
            f"digest={digests[-1]}"
        )
    if len(set(digests)) != 1:
        print("ERROR: concurrency modes disagree")
        return 1
    print("concurrency conclusions agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
