from __future__ import annotations

import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "samples"


def load_sample(name: str = "config.json") -> dict:
    with open(SAMPLES / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


def sample_copy(name: str = "config.json") -> dict:
    return copy.deepcopy(load_sample(name))
