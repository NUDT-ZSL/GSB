"""Independent verification tool and cross-stage consistency self-check.

Checks, in order:
  1. every stage is committed and its snapshot checksum matches manifest+meta;
  2. pair_id sets are identical across all four stages and the input;
  3. kept vs fallback alignment counts are disjoint and partition the terms;
  4. the final ranking contains each pair exactly once, ranks are 1..N and
     scores are non-increasing;
  5. a full independent recompute into a scratch state dir reproduces every
     stage checksum byte-for-byte.

Any mismatch raises E_INCONSISTENT with a category and the stage version.
"""

import shutil
import tempfile

import numpy as np

from .errors import E_INCONSISTENT, PipelineError
from .pipeline import Pipeline, load_pairs
from .stages import (
    FALLBACK_VIAS,
    STAGE_ALIGN,
    STAGE_NORMALIZE,
    STAGE_ORDER,
    STAGE_SCORE,
    STAGE_SUMMARIZE,
    VIA_DIRECT,
)
from .store import StageStore
from .util import sha256_hex


def _inconsistent(category, message, *, stage=None, version=None):
    return PipelineError(
        E_INCONSISTENT, message, stage=stage, version=version,
        category=category)


def _check_committed_and_checksums(store):
    for stage in STAGE_ORDER:
        committed = store.committed(stage)
        if not committed:
            raise _inconsistent("stage_missing",
                                f"stage {stage} has no committed snapshot",
                                stage=stage)
        version = committed["version"]
        data = store.load_snapshot_bytes(stage, version)
        actual = sha256_hex(data)
        meta = store.load_meta(stage, version)
        if actual != committed["checksum"] or actual != meta.get("checksum"):
            raise _inconsistent(
                "checksum",
                f"stage {stage} v{version} checksum mismatch",
                stage=stage, version=version)


def _load_stage_records(store):
    records = {}
    for stage in STAGE_ORDER:
        version = store.committed(stage)["version"]
        records[stage] = list(store.iter_snapshot(stage, version))
    return records


def _check_pair_sets(records, input_pair_ids):
    reference = None
    for stage in STAGE_ORDER:
        ids = sorted(r["pair_id"] for r in records[stage])
        if len(set(ids)) != len(ids):
            raise _inconsistent("pair_set",
                                f"stage {stage} contains duplicate pair_ids",
                                stage=stage)
        if reference is None:
            reference = ids
        elif ids != reference:
            raise _inconsistent(
                "pair_set",
                f"stage {stage} pair_id set differs from earlier stages",
                stage=stage)
    if sorted(input_pair_ids) != reference:
        raise _inconsistent("pair_set",
                            "stage pair_id sets differ from pipeline input",
                            stage=STAGE_NORMALIZE)


def _check_alignment_stats(records):
    for rec in records[STAGE_ALIGN]:
        stats = rec["stats"]
        direct = sum(1 for a in rec["aligned"] if a["via"] == VIA_DIRECT)
        fallback = sum(1 for a in rec["aligned"] if a["via"] in FALLBACK_VIAS)
        total = len(rec["aligned"])
        kept_count = stats[VIA_DIRECT]
        fallback_count = sum(stats[v] for v in FALLBACK_VIAS)
        if direct != kept_count or fallback != fallback_count \
                or kept_count + fallback_count != total \
                or direct + fallback != total:
            raise _inconsistent(
                "mapping_overlap",
                f"pair {rec['pair_id']}: kept/fallback counts overlap or "
                f"do not partition {total} aligned terms",
                stage=STAGE_ALIGN)


def _check_ranking(records):
    ranking = records[STAGE_SUMMARIZE]
    ids = [r["pair_id"] for r in ranking]
    if len(set(ids)) != len(ids):
        raise _inconsistent("ranking", "ranking contains a pair more than once",
                            stage=STAGE_SUMMARIZE)
    if sorted(ids) != sorted(r["pair_id"] for r in records[STAGE_SCORE]):
        raise _inconsistent("ranking", "ranking pair set differs from scoring",
                            stage=STAGE_SUMMARIZE)
    ranks = [r["rank"] for r in ranking]
    if ranks != list(range(1, len(ranking) + 1)):
        raise _inconsistent("ranking", "ranks are not contiguous from 1",
                            stage=STAGE_SUMMARIZE)
    scores = [r["score"] for r in ranking]
    if any(b > a for a, b in zip(scores, scores[1:])):
        raise _inconsistent("ranking", "scores are not non-increasing",
                            stage=STAGE_SUMMARIZE)


def _recompute_and_compare(data_path, resources_dir, config, store):
    scratch = tempfile.mkdtemp(prefix="xlpipe_verify_")
    try:
        rerun = Pipeline(data_path, resources_dir, config, scratch)
        rerun.run()
        for stage in STAGE_ORDER:
            expected = rerun.store.committed(stage)["checksum"]
            actual = store.committed(stage)["checksum"]
            if expected != actual:
                version = store.committed(stage)["version"]
                raise _inconsistent(
                    "recompute",
                    f"stage {stage} v{version} does not reproduce under "
                    f"independent recompute",
                    stage=stage, version=version)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _coverage_stats(records):
    coverage = np.array([r["coverage"] for r in records[STAGE_SCORE]],
                        dtype=np.float64) / 1_000_000.0
    matched = np.array([r["matched"] for r in records[STAGE_SCORE]])
    return {
        "pairs": int(len(coverage)),
        "coverage_mean": round(float(np.mean(coverage)), 6),
        "coverage_min": round(float(np.min(coverage)), 6),
        "coverage_max": round(float(np.max(coverage)), 6),
        "matched_terms_total": int(np.sum(matched)),
    }


def verify(data_path, resources_dir, config, state_dir):
    store = StageStore(state_dir)
    _check_committed_and_checksums(store)
    records = _load_stage_records(store)
    input_pair_ids = [p["pair_id"] for p in load_pairs(data_path)]
    _check_pair_sets(records, input_pair_ids)
    _check_alignment_stats(records)
    _check_ranking(records)
    _recompute_and_compare(data_path, resources_dir, config, store)
    return {
        "status": "ok",
        "stages": {stage: dict(store.committed(stage)) for stage in STAGE_ORDER},
        "coverage": _coverage_stats(records),
        "pair_count": len(records[STAGE_SUMMARIZE]),
    }
