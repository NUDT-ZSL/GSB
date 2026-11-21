from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bmm_pipeline.jobs import generate_sample_jobs, write_jobs_jsonl


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples/jobs.jsonl")
    parser.add_argument("--seed", type=int, default=20261122)
    args = parser.parse_args()
    output = Path(args.out)
    write_jobs_jsonl(output, generate_sample_jobs(args.seed))
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
