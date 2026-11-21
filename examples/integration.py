from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dep_arc_interop import (
    SnapshotStore,
    judge_equivalence,
    load_format,
    migrate,
    verify_roundtrip,
)


def run() -> dict:
    data = ROOT / "data"
    store = SnapshotStore()
    store.load(data / "config" / "controlled_snapshot_v1.json")
    snapshot = store.current

    v1 = load_format(data / "samples" / "arcs_v1.txt", snapshot)
    v2 = load_format(data / "samples" / "arcs_v2.txt", snapshot)
    v3 = load_format(data / "samples" / "arcs_v3.json", snapshot)

    equivalence = judge_equivalence(
        {"v1": v1, "v2": v2, "v3": v3},
        snapshot,
    )
    upgrade = migrate(v1, "v3", snapshot)
    downgrade = migrate(v3, "v1", snapshot)
    roundtrip = verify_roundtrip(data / "samples" / "arcs_v2.txt", snapshot)
    return {
        "equivalent": equivalence.to_dict()["equivalent"],
        "arc_count": equivalence.arc_count,
        "upgrade_fill_count": len(upgrade.fills),
        "upgrade_replay_differences": len(upgrade.replay_differences()),
        "downgrade_drop_count": len(downgrade.drops),
        "downgrade_replay_differences": len(downgrade.replay_differences()),
        "roundtrip_consistent": roundtrip.to_dict()["consistent"],
        "snapshot_trace": store.history[-1].to_dict(),
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
