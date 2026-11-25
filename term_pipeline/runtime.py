"""Rehydrate committed stage payloads for inspection commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_config
from .features import load_context_features
from .glossary import load_glossary
from .rules import load_rules
from .snapshots import SnapshotStore


def load_committed_runtime(config_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    store = SnapshotStore(config["_resolved_paths"]["snapshot_dir"])
    snapshots = store.load_all()
    if len(snapshots) < 3:
        raise RuntimeError("pipeline has not committed enough stages")
    documents = snapshots[0]["payload"]["documents"]
    extracted = snapshots[1]["payload"]["documents"]
    scored = snapshots[2]["payload"]["documents"]
    glossary = load_glossary(config)
    features = load_context_features(config, documents)
    rules = load_rules(config, glossary, features)
    result = {
        "config": config,
        "store": store,
        "snapshots": snapshots,
        "documents": documents,
        "extracted": extracted,
        "scored_documents": scored,
        "path_decision": snapshots[2]["payload"]["adaptive_decision"],
        "budget_plan": snapshots[2]["payload"]["budget_plan"],
        "glossary": glossary,
        "features": features,
        "rules": rules,
    }
    if len(snapshots) >= 4:
        result["disambiguated_documents"] = snapshots[3]["payload"]["documents"]
    return result

