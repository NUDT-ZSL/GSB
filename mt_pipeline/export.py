"""Stage 8: TSV export, JSON metrics report, cross-stage self-check."""

from pathlib import Path

from .errors import Codes, PipelineError
from .filtering import metrics_from_details


def export_tsv(path, kept_details, seg_by_id):
    """Export kept pairs as TSV in declared (pair_id ascending) order."""
    ordered = sorted(kept_details, key=lambda x: x["pair_id"])
    lines = []
    for d in ordered:
        seg = seg_by_id[d["pair_id"]]
        lines.append("\t".join([
            d["pair_id"],
            " ".join(seg["src_pieces"]),
            " ".join(seg["tgt_pieces"]),
        ]))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return [d["pair_id"] for d in ordered]


def self_check(ctx):
    """Cross-stage consistency self-check; raises E_INCONSISTENT with the
    first divergence location."""
    def fail(location, message):
        raise PipelineError(Codes.INCONSISTENT, message, position=location)

    ingest_ids = ctx["ingest_ids"]
    for stage, ids in (("subword", ctx["subword_ids"]), ("filter", ctx["filter_ids"])):
        if set(ids) != set(ingest_ids):
            diff = sorted(set(ingest_ids) ^ set(ids))
            fail(f"stage_pair_ids:{stage}", f"{stage} pair_id set differs, first: {diff[0]}")

    exported = set(ctx["exported_ids"])
    kept = set(ctx["kept_ids"])
    dropped = set(ctx["dropped_ids"])
    if exported != kept:
        diff = sorted(exported ^ kept)
        fail("export:kept_mismatch", f"exported set != kept set, first: {diff[0]}")
    bad = sorted(exported & dropped)
    if bad:
        fail("export:dropped_present", f"dropped pair {bad[0]} present in export")

    recomputed_vocab = set()
    for pieces in ctx["all_pieces"]:
        recomputed_vocab.update(pieces)
    if recomputed_vocab != set(ctx["vocab"]):
        fail("vocab_recompute", "vocab not recomputable from per-sentence segmentations")

    recomputed = metrics_from_details(ctx["details"])
    if recomputed != ctx["reported_metrics"]:
        fail("metrics:recompute", "reported metrics differ from detail-recomputed metrics")

    covered = set()
    for batch in ctx["batches"]:
        covered.update(batch)
    if covered != set(ingest_ids):
        missing = sorted(set(ingest_ids) - covered)
        fail("path_coverage", f"decision path misses pair {missing[0]}")
    return "PASS"
