from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
RULES = SAMPLES / "rules.json"
TOPOLOGY = SAMPLES / "topology.json"
EVENTS = SAMPLES / "events.ndjson"
CHANGESET = SAMPLES / "changeset-pay-severity.json"
