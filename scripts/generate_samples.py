from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diagnostic_app.samples import generate_all


if __name__ == "__main__":
    manifest = generate_all(ROOT / "samples")
    print(f"generated {manifest['task_count']} tasks under {ROOT / 'samples'}")
