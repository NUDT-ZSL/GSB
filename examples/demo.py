"""Run the complete demo from a source checkout without prior installation."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sre_burnrate.demo import main  # noqa: E402


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = [
            "--data",
            str(ROOT / "examples" / "data" / "sli_minutes.ndjson"),
            "--config",
            str(ROOT / "examples" / "config" / "slo.json"),
            "--tuned-config",
            str(ROOT / "examples" / "config" / "slo_tuned.json"),
            "--output-dir",
            str(ROOT / "examples" / "out"),
        ]
    raise SystemExit(main(args))

