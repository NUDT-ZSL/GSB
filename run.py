"""
Workspace launcher: `python run.py --frames ... --dialogues ... --tolerance-ms 8 --out-dir out`
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lip_sync.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
