"""Command-line interface. Exit codes are plain integers (Windows-safe)."""

import argparse
import sys

from . import EXIT_OK
from .datagen import DEFAULTS, generate
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="peworkbench")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic samples")
    gen.add_argument("--outdir", required=True)
    gen.add_argument("--seed", type=int, default=DEFAULTS["seed"])
    gen.add_argument("--samples", type=int, default=DEFAULTS["n_samples"])

    run_p = sub.add_parser("run", help="run the diagnostic pipeline")
    run_p.add_argument("--config", required=True)
    run_p.add_argument("--samples", required=True)
    run_p.add_argument("--injections", required=True)
    run_p.add_argument("--workdir", required=True)
    run_p.add_argument("--report", default=None)
    run_p.add_argument("--allow-reconfigure", action="store_true",
                       help="reset snapshots (keep content cache) when the "
                            "config/injection hash changed instead of "
                            "refusing with E_CONFIG_MISMATCH")

    args = parser.parse_args(argv)
    if args.command == "generate":
        generate(args.outdir, seed=args.seed, n_samples=args.samples)
        return EXIT_OK
    return run(args.config, args.samples, args.injections, args.workdir,
               report_path=args.report,
               allow_reconfigure=args.allow_reconfigure)


if __name__ == "__main__":
    sys.exit(int(main()))
