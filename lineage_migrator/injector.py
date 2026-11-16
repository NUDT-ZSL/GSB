"""Configurable anomaly injection and rollback verification.

Each anomaly scenario runs against an isolated database replayed from the same
deterministic sample up to the last happy-path version. A failed attempt must
leave the database at its pre-attempt state, which is asserted via fingerprint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .engine import Engine
from .errors import MigrationError
from .models import Rule, Sample
from .store import Store


@dataclass
class AnomalyScenario:
    scenario_id: str
    description: str
    version: int
    expected_code: str
    rules: list[Rule]

    @classmethod
    def from_dict(cls, payload: dict) -> "AnomalyScenario":
        return cls(
            scenario_id=payload["scenario_id"],
            description=payload["description"],
            version=int(payload["version"]),
            expected_code=payload["expected_code"],
            rules=[Rule.from_dict(item) for item in payload["rules"]],
        )


def load_anomalies(path: str | Path) -> list[AnomalyScenario]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [AnomalyScenario.from_dict(item) for item in payload]


def _build_replayed_store(db_path: Path, sample: Sample, stop_before: int) -> Store:
    """Fresh DB with baseline plus every happy-path version below stop_before."""
    engine = Engine.bootstrap(db_path, sample)
    for version in sample.ordered_versions():
        if version >= stop_before:
            break
        engine.apply_version(version, sample.versions[version])
    return engine.store


def run_scenario(
    scenario: AnomalyScenario,
    sample: Sample,
    workspace: Path,
    *,
    rules_override: list[Rule] | None = None,
    version_override: int | None = None,
) -> dict:
    """Execute one scenario and return a deterministic result record."""
    workspace.mkdir(parents=True, exist_ok=True)
    rules = rules_override or scenario.rules
    attempt_version = version_override or scenario.version

    happy_versions = sample.ordered_versions()
    if not happy_versions:
        stop_before = 1
    elif attempt_version <= max(happy_versions):
        # Regression attempt: replay the full happy path so the target version
        # is already committed and E_VERSION_REGRESS can be triggered.
        stop_before = max(happy_versions) + 1
    else:
        # Forward-looking anomaly: replay everything strictly below it.
        stop_before = attempt_version
    db_path = workspace / f"{scenario.scenario_id}.db"
    if db_path.exists():
        db_path.unlink()
    store = _build_replayed_store(db_path, sample, stop_before)

    before = {
        "last_version": store.last_version(),
        "fingerprint": store.get_version(store.last_version())["fingerprint"],
        "edge_count": len(store.live_edges()),
    }
    engine = Engine(store)
    actual_code = "OK"
    message = ""
    try:
        engine.apply_version(attempt_version, rules)
    except MigrationError as exc:
        actual_code = exc.code.value
        message = exc.message

    after = {
        "last_version": store.last_version(),
        "fingerprint": store.get_version(store.last_version())["fingerprint"],
        "edge_count": len(store.live_edges()),
    }
    rolled_back_cleanly = before == after
    store.close()
    return {
        "scenario_id": scenario.scenario_id,
        "attempt_version": attempt_version,
        "expected_code": scenario.expected_code,
        "actual_code": actual_code,
        "matched": actual_code == scenario.expected_code,
        "rolled_back_cleanly": rolled_back_cleanly,
        "state_before": before,
        "state_after": after,
        "message": message,
    }


def run_all_scenarios(sample: Sample, anomalies_path: str | Path, workspace: Path) -> list[dict]:
    scenarios = load_anomalies(anomalies_path)
    return [run_scenario(scenario, sample, workspace) for scenario in scenarios]


def happy_path_summary(sample: Sample, db_path: Path) -> dict:
    """Replay all happy-path versions twice and assert bit-identical results."""
    fingerprints: list[list[dict]] = []
    final_edges: list[list[dict]] = []
    for run_index in range(2):
        run_db = db_path.parent / f"replay_run{run_index + 1}.db"
        if run_db.exists():
            run_db.unlink()
        engine = Engine.bootstrap(run_db, sample)
        engine.replay_all(sample)
        store = engine.store
        fingerprints.append(store.versions())
        final_edges.append([edge.__dict__ for edge in store.snapshot_edges(store.last_version())])
        store.close()
    identical = fingerprints[0] == fingerprints[1] and final_edges[0] == final_edges[1]
    return {
        "replay_runs": 2,
        "bit_identical": identical,
        "versions": fingerprints[0],
        "final_edge_count": len(final_edges[0]),
        "final_edges": final_edges[0],
    }
