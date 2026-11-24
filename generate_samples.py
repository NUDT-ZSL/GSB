"""First runnable entry point: generate deterministic local samples."""

from __future__ import annotations

import argparse
from pathlib import Path

from sso_ticket_guard.samplegen import DEFAULT_SEED, generate_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic SSO samples")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("samples"),
        help="sample output directory (default: samples)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    manifest = generate_samples(args.output, args.seed)
    print(f"generated {len(manifest['files'])} declaration files in {args.output}")
    print(f"seed={manifest['seed']} quota_profile={manifest['quota_profile_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

