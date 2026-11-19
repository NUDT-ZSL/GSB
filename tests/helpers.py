from __future__ import annotations

import copy
import json
from pathlib import Path

from annotation_app.ontology import OntologySnapshot
from annotation_app.reader import parse_v1, parse_v2


ROOT = Path(__file__).resolve().parents[1]


def ontology_v1() -> OntologySnapshot:
    return OntologySnapshot.from_dict(
        json.loads((ROOT / "snapshots" / "ontology-v1.json").read_text("utf-8"))
    )


def ontology_v2() -> OntologySnapshot:
    return OntologySnapshot.from_dict(
        json.loads((ROOT / "snapshots" / "ontology-v2.json").read_text("utf-8"))
    )


def sample_v1_dict() -> dict:
    return json.loads((ROOT / "samples" / "sample-v1.json").read_text("utf-8"))


def sample_v2_dict() -> dict:
    return json.loads((ROOT / "samples" / "sample-v2.json").read_text("utf-8"))


def load_v1():
    return parse_v1(copy.deepcopy(sample_v1_dict()), ontology=ontology_v1())


def load_v2():
    return parse_v2(copy.deepcopy(sample_v2_dict()), ontology=ontology_v1())

