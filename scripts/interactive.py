"""Launch the local interactive diagnostic REPL."""

from __future__ import annotations

import argparse
import json

from seraph_core.config.config import EngineConfig
from seraph_core.diag.app import DiagnosticApp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--data", default="data/corpus")
    parser.add_argument("--snapshot-dir", default="snapshots/interactive")
    args = parser.parse_args()
    with open(args.config, "r", encoding="utf-8") as handle:
        config = EngineConfig.from_dict(json.load(handle))
    DiagnosticApp(config, args.data, args.snapshot_dir).interact()


if __name__ == "__main__":
    main()
