"""Generate the deterministic local sample without network or external data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ts_pipeline.config import load_config
from ts_pipeline.series import generate_series, save_series


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/sample_config.json")
    parser.add_argument("--output", default="data/sample_series.json")
    args = parser.parse_args()
    config = load_config(args.config)
    series = generate_series(config)
    destination = save_series(series, Path(args.output))
    print(
        f"generated length={series.length} seed={series.seed} "
        f"target_sha256={series.digest} output={destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
