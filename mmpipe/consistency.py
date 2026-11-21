"""Independent cross-stage consistency checks and snapshot re-verification.

This module never trusts pipeline bookkeeping: it re-opens every committed
    snapshot, recomputes checksums, compares pair sets and replays provenance
    rules. Any violation raises E_INCONSISTENT annotated with a category and
    the offending snapshot version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Sequence

import numpy as np

from .config import STAGES, PipelineConfig
from .errors import InconsistentError
from .manifest import PairRecord, canonical_order, validate_manifest
from .stages import fuse, image_encode, score, text_encode
from .store import SnapshotStore
from .weights import FixedWeights, generate_image


def _inconsistent(category: str, message: str, *, stage: str, version: int) -> InconsistentError:
    return InconsistentError(
        f"[{category}] {message}", stage=stage, version=version, category=category
    )


def verify_snapshots(
    work_dir: Path,
    records: Sequence[PairRecord],
    config: PipelineConfig,
    *,
    force_rebuild: bool = False,
) -> dict:
    """Re-verify all four committed snapshots and cross-stage consistency."""
    work_dir = Path(work_dir)
    validated = validate_manifest(records, config)
    ordered = canonical_order(validated)
    expected_pairs = [r.pair_id for r in ordered]
    expected_set = set(expected_pairs)

    store = SnapshotStore(work_dir, config)
    snapshots = {}
    for stage in STAGES:
        snapshots[stage] = store.load_committed(stage, force_rebuild=force_rebuild)
        if snapshots[stage] is None:
            raise _inconsistent(
                "missing_stage",
                f"stage {stage!r} has no committed snapshot",
                stage=stage,
                version=0,
            )

    findings: List[str] = []
    # Check 1: identical pair_id set across every stage.
    for stage, snapshot in snapshots.items():
        if set(snapshot.pair_ids) != expected_set:
            raise _inconsistent(
                "pair_set_mismatch",
                f"stage {stage!r} pair set {sorted(snapshot.pair_ids)} != manifest {expected_pairs}",
                stage=stage,
                version=snapshot.version,
            )
        if list(snapshot.pair_ids) != sorted(snapshot.pair_ids):
            raise _inconsistent(
                "pair_order",
                "pair_ids are not canonically ordered",
                stage=stage,
                version=snapshot.version,
            )
    findings.append("all four stages share the identical, canonically ordered pair_id set")

    # Check 2: every fusion pair has exactly one score entry.
    fusion_pairs = list(snapshots["fusion"].pair_ids)
    score_pairs = list(snapshots["score"].pair_ids)
    if len(score_pairs) != len(set(score_pairs)) or score_pairs != fusion_pairs:
        raise _inconsistent(
            "score_cardinality",
            "each fusion pair must be scored exactly once",
            stage="score",
            version=snapshots["score"].version,
        )
    findings.append("each fusion pair is scored exactly once")

    # Check 3: recompute/reuse provenance disjoint and covering.
    report_path = work_dir / "run_report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for stage in STAGES:
            prov = report["provenance"][stage]
            reused = set(prov["reused_pair_ids"])
            recomputed = set(prov["recomputed_pair_ids"])
            if reused & recomputed:
                raise _inconsistent(
                    "recompute_overlap",
                    f"reused and recomputed sets intersect: {sorted(reused & recomputed)}",
                    stage=stage,
                    version=snapshots[stage].version,
                )
            if (reused | recomputed) != expected_set:
                raise _inconsistent(
                    "recompute_coverage",
                    "reused ∪ recomputed does not cover every manifest pair",
                    stage=stage,
                    version=snapshots[stage].version,
                )
        findings.append("recompute/reuse provenance is disjoint and covers all pairs")

    # Check 4: independent numeric replay must reproduce stored bytes.
    weights = FixedWeights(config)
    images_dir = work_dir / "images"
    by_id = {record.pair_id: record for record in ordered}
    for pair_id in expected_pairs:
        record = by_id[pair_id]
        pixels = generate_image(record.image_seed, tuple(record.image_shape))
        stored_pixels = np.load(images_dir / f"{pair_id}.npy")
        if not np.array_equal(pixels, stored_pixels):
            raise _inconsistent(
                "pixels_drift",
                "stored pixels differ from deterministic seed output",
                stage="image",
                version=snapshots["image"].version,
            )
        image_emb = image_encode(stored_pixels, config, weights)
        text_emb = text_encode(record, config, weights)
        fused = fuse(record, image_emb, text_emb, config, weights)
        final = score(fused, config, weights)
        checks = [
            ("image", image_emb),
            ("text", text_emb),
            ("fusion", fused),
            ("score", final),
        ]
        for stage, expected_array in checks:
            stored_array = snapshots[stage].load(pair_id)
            if stored_array.shape != expected_array.shape:
                raise _inconsistent(
                    "replay_shape",
                    f"pair {pair_id} stage {stage} shape mismatch",
                    stage=stage,
                    version=snapshots[stage].version,
                )
            if not np.array_equal(stored_array, expected_array):
                raise _inconsistent(
                    "replay_value",
                    f"pair {pair_id} stage {stage} recomputed values differ from snapshot",
                    stage=stage,
                    version=snapshots[stage].version,
                )
            if stored_array.tobytes() != expected_array.tobytes():
                raise _inconsistent(
                    "replay_bytes",
                    f"pair {pair_id} stage {stage} byte representation differs",
                    stage=stage,
                    version=snapshots[stage].version,
                )
    findings.append("independent replay reproduces every stored embedding byte-for-byte")

    return {
        "ok": True,
        "pair_count": len(expected_pairs),
        "versions": {stage: snapshots[stage].version for stage in STAGES},
        "checksums": {stage: snapshots[stage].embeddings_checksum for stage in STAGES},
        "findings": findings,
    }


def compare_runs(report_a: dict, report_b: dict) -> dict:
    """Compare two runs; checksums and pair order must be byte-identical."""
    diffs = []
    for stage in STAGES:
        a = report_a["stages"][stage]
        b = report_b["stages"][stage]
        if a["embeddings_checksum"] != b["embeddings_checksum"]:
            diffs.append(f"{stage}: checksum differs")
    if report_a["pair_ids_canonical"] != report_b["pair_ids_canonical"]:
        diffs.append("canonical pair order differs")
    return {"identical": not diffs, "differences": diffs}

