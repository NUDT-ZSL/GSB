#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from acoustic_alignment.sample_generator import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
