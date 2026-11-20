from __future__ import annotations

import copy
from pathlib import Path

from leasecoord.config import read_json
from leasecoord.engine import Engine


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "workspace"


def sample_documents() -> tuple[dict, dict]:
    return (
        copy.deepcopy(read_json(SAMPLE / "nodes.json")),
        copy.deepcopy(read_json(SAMPLE / "election_config.json")),
    )


def load_sample(workspace: Path) -> tuple[Engine, dict, dict]:
    nodes, election = sample_documents()
    engine = Engine(workspace)
    engine.load(nodes, election, clock="0", reset=True)
    return engine, nodes, election


def run_to_failover(workspace: Path) -> Engine:
    engine, _, _ = load_sample(workspace)
    engine.elect(clock="0")
    for tick in range(100, 1000, 100):
        engine.renew(node="alpha", session_epoch=1, clock=str(tick))
        engine.renew(node="charlie", session_epoch=3, clock=f"{tick}.0002")
    engine.elect(clock="1000")
    return engine
