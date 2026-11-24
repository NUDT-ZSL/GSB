from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from supply_chain_audit.engine import compare_views
from supply_chain_audit.generator import generate_seed_workspace
from supply_chain_audit.parsing import load_revocation_batch, load_snapshot
from supply_chain_audit.service import build_seed_engine


def main() -> None:
    root = Path(__file__).resolve().parent / "seed_workspace"
    paths = generate_seed_workspace(root)

    engine = build_seed_engine(root)
    update = load_snapshot(paths["update_batch"], engine.clock)
    update_result = engine.apply_parse_result(update)
    revocation = load_revocation_batch(paths["revocation_batch"])
    revocation_result = engine.apply_revocation_batch(revocation)

    full = build_seed_engine(root, include_update=True, include_revocation=True)
    divergence = compare_views(engine.view, full.view)

    summary = {
        "artifact_count": engine.view["stats"]["artifact_count"],
        "conflicts": [(item["code"], item["artifact_key"]) for item in engine.view["conflicts"]],
        "crypto_closure": engine.view["closure_sets"].get("security/crypto@3.0.1"),
        "update_rejudged": update_result["rejudged_keys"],
        "revocation_rejudged": revocation_result["rejudged_keys"],
        "divergence": divergence,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
