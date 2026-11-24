"""Subprocess used by the forced-termination recovery test."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mfa_guard.engine import EngineConfig, load_engine_from_samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--marker", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    work = Path(args.work)
    engine = load_engine_from_samples(
        root / "samples",
        ledger_path=work / "mfa_ledger.sqlite",
        work_dir=work,
        config=EngineConfig(
            canonical_scale=6,
            fingerprint_threshold_micro=5_000,
            concurrency=1,
            timeout_ms=None,
            queue_limit=64,
            auto_recover=True,
        ),
    )
    attempts = sorted(
        engine.initial_attempts,
        key=lambda item: (
            item.submitted_at_ms,
            item.attempt_no,
            item.user_id,
            item.factor_id,
        ),
    )
    engine.process_batch(attempts[:5], checkpoint=True)
    engine.process_batch(attempts[5:], checkpoint=False)
    Path(args.marker).write_text("COMMITTED_UNCHECKPOINTED\n", encoding="utf-8")
    # The parent deliberately kills this process here.
    time.sleep(60)
    engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
