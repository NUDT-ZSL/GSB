#!/usr/bin/env python3
"""Run one round but pause after the incomplete ledger row commits.

The demo parent force-kills this process at the marker. The pause is only a
crash-test barrier: no probe result exists or is fabricated at that point.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lbhealth.config import load_config  # noqa: E402
from lbhealth.engine import HealthController  # noqa: E402
from lbhealth.ledger import Ledger  # noqa: E402
from lbhealth.prober import LoopbackProber  # noqa: E402
from lbhealth.recovery import recover_session  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--pause-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    ledger = Ledger(args.db)
    session = recover_session(config, ledger)
    marker = Path(args.marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        marker.unlink()

    def after_round_start(round_no: int, attempt_id: int) -> None:
        marker.write_text(
            f'{{"round":{round_no},"attempt_id":{attempt_id}}}\n', encoding="utf-8"
        )
        time.sleep(args.pause_seconds)

    controller = HealthController(
        config,
        ledger,
        session.engine,
        LoopbackProber(config),
        round_started_callback=after_round_start,
    )
    controller.run_round()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
