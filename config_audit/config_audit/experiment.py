"""End-to-end reproducible experiment.

Sequence
--------
1. First audit at a coarse grid (places=0) and persist a snapshot.
2. Second audit at a fine grid (places=3); document any verdict flips and mark
   the coarse-grid conclusions stale (never mixed with current ones).
3. Whole-state restore + bit-identical replay from the snapshot.
4. A version-mismatch restore demonstrating whole replacement (not merge).

All outputs land under ``runs/`` with stable ordering.  NumPy is used only for
the final integer error-bound aggregation (a tiny, deterministic reduction).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .audit import run_audit
from .generate_samples import write_samples
from .snapshot import (
    current_archive_versions,
    restore_and_replay,
    save_snapshot,
)

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
RUNS = ROOT / "runs"


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _verdict_index(report: dict) -> dict[str, str]:
    return {key: entry["key_verdict"] for key, entry in report["keys"].items()}


def _error_bound_summary(report: dict) -> dict:
    items = report["quantized_items"]
    down = np.array([q["down_error"] for q in items], dtype=np.int64)
    up = np.array([q["up_error"] for q in items], dtype=np.int64)
    derived = [q for q in items if q["source"] == "derived"]
    return {
        "quantized_item_count": int(items.__len__()),
        "down_error_sum": int(down.sum()),
        "up_error_sum": int(up.sum()),
        "down_error_max": int(down.max()),
        "up_error_max": int(up.max()),
        "derived_item_count": len(derived),
        "derived_keys": sorted(q["key"] for q in derived),
    }


def main() -> dict:
    RUNS.mkdir(parents=True, exist_ok=True)
    write_samples(SAMPLES)
    baseline_inputs = [SAMPLES / "node_a.json", SAMPLES / "node_b.json"]

    # Step 1: coarse grid first audit + snapshot.
    coarse = run_audit(baseline_inputs, places=0, abs_tolerance="5",
                       relative_ppm=2000)
    _dump(RUNS / "report_coarse_p0.json", coarse)
    save_snapshot(coarse, current_archive_versions(baseline_inputs),
                  RUNS / "snapshot_p0.json")

    # Step 2: fine grid audit, marking the older grid signature stale.
    fine = run_audit(baseline_inputs, places=3, abs_tolerance="5",
                     relative_ppm=2000,
                     previous_grid_sig=coarse["grid"]["signature"])
    _dump(RUNS / "report_fine_p3.json", fine)

    flips = []
    coarse_idx, fine_idx = _verdict_index(coarse), _verdict_index(fine)
    for key in sorted(set(coarse_idx) | set(fine_idx)):
        if coarse_idx.get(key) != fine_idx.get(key):
            flips.append({"key": key,
                          "coarse_p0": coarse_idx.get(key),
                          "fine_p3": fine_idx.get(key)})

    # Step 3: restore + replay from the coarse snapshot (same versions).
    replay = restore_and_replay(baseline_inputs, RUNS / "snapshot_p0.json")
    bit_identical = (replay["canonical_hash"] == coarse["canonical_hash"])

    # Step 4: version mismatch -> whole replacement.
    v2_inputs = [SAMPLES / "node_a_v2.json", SAMPLES / "node_b.json"]
    replay_v2 = restore_and_replay(v2_inputs, RUNS / "snapshot_p0.json")

    summary = {
        "coarse_grid": coarse["grid"]["signature"],
        "fine_grid": fine["grid"]["signature"],
        "fine_marked_stale_previous":
            fine["grid"]["stale_previous_signature"],
        "grid_changed_flag": fine["grid"]["grid_changed"],
        "verdict_flips": flips,
        "replay_bit_identical": bit_identical,
        "replay_action": replay["restore"]["action"],
        "version_mismatch_action": replay_v2["restore"]["action"],
        "coarse_error_bounds": _error_bound_summary(coarse),
        "fine_error_bounds": _error_bound_summary(fine),
        "coarse_hash": coarse["canonical_hash"],
        "fine_hash": fine["canonical_hash"],
        "replay_hash": replay["canonical_hash"],
    }
    _dump(RUNS / "experiment_summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    main()
