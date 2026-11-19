import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"


@pytest.fixture()
def input_records():
    return json.loads((DATA / "input_records.json").read_text(encoding="utf-8"))["records"]


@pytest.fixture()
def demo_pipeline_path():
    return DATA / "pipeline.json"


@pytest.fixture()
def workspace(tmp_path):
    db = tmp_path / "ledger.db"
    out = tmp_path / "results.json"
    return {"db": db, "out": out}
