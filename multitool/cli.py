"""Command line entry point.

Usage:
    python -m multitool.cli path/to/timeline.json

The alignment report is printed to stdout and the process exit code maps to
the E_xxx business code.
"""

import argparse
import json
import sys

from .engine import process
from .errors import EXIT_CODES


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Multi-track WAV offline transcode and sample-accurate mixer"
    )
    parser.add_argument("manifest", help="path to the timeline manifest JSON")
    args = parser.parse_args(argv)

    report, code = process(args.manifest)
    print(json.dumps(report, indent=2, sort_keys=True))
    return EXIT_CODES.get(code, 1)


if __name__ == "__main__":
    sys.exit(main())
