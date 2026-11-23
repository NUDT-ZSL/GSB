"""End-to-end deterministic MT data-preparation pipeline orchestration."""

from pathlib import Path

from . import consistency, dedup, filtering, subword
from .budget import BudgetConfig, Observation, decide_path
from .datagen import load_vocab
from .export import export_tsv, self_check
from .ingest import IngestConfig, run_ingest
from .snapshots import SnapshotStore
from .util import checksum_obj, write_json


def _estimate_avg_candidates(vocab_tokens, cfg, sample=64):
    counts = [len(subword.token_candidates(t, cfg)) for t in vocab_tokens[:sample]]
    return (sum(counts) / len(counts)) if counts else 1.0


def _run_path(records, sub_cfg, filter_cfg, mix_targets, path):
    seg_summary, vocab, freq = subword.segment_pairs(records, sub_cfg, path=path)
    dropped = dedup.dedup(records, "exact" if path == "exact" else "approx")
    details = filtering.run_filter(records, seg_summary["pairs"], dropped, filter_cfg)
    details = filtering.apply_mix(details, mix_targets)
    return seg_summary, vocab, freq, details


def run_pipeline(config, data_dir, out_dir, snapshot_dir):
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    config_checksum = checksum_obj(config)

    vocab_version, src_vocab = load_vocab(data_dir / "src_vocab.txt")
    _, tgt_vocab = load_vocab(data_dir / "tgt_vocab.txt")

    ingest_cfg = IngestConfig(**config.get("ingest", {}))
    sub_cfg = subword.SubwordConfig(**config.get("subword", {}))
    filter_cfg = filtering.FilterConfig(**config.get("filter", {}))
    budget_cfg = BudgetConfig(**config.get("budgets", {}))
    mix_targets = config.get("mix_targets", {})
    filter_cfg.validate()
    sub_cfg.validate()

    # Stage 1: sharded ingest + alignment validation.
    ingest_summary, records = run_ingest(
        data_dir / "corpus.jsonl", src_vocab, tgt_vocab, vocab_version,
        ingest_cfg, config_checksum,
    )
    pair_ids = [r["pair_id"] for r in records]

    # Stages 4+6: observation, dual-budget admission, path decision.
    obs = Observation(
        n_pairs=len(records),
        vocab_size=len(src_vocab) + len(tgt_vocab),
        avg_align_len=ingest_summary["avg_align_len"],
        avg_candidates=_estimate_avg_candidates(src_vocab + tgt_vocab, sub_cfg),
        mem_headroom_bytes=config.get("mem_headroom_bytes", budget_cfg.memory_limit),
    )
    decision_record, batches = decide_path(obs, budget_cfg, pair_ids)
    chosen = decision_record["decision"]["path"]
    seg_path = "exact" if chosen == "exact" else "approx"

    # Stage 2 (+dedup, stage 3 filter/mix) on the chosen path.
    seg_summary, vocab, freq, details = _run_path(
        records, sub_cfg, filter_cfg, mix_targets, seg_path)

    # Stage 5: cross-path consistency adjudication (both paths on same corpus).
    consistency_report = None
    if config.get("consistency_check", True):
        e_seg, e_vocab, e_freq, e_details = _run_path(
            records, sub_cfg, filter_cfg, mix_targets, "exact")
        a_seg, a_vocab, a_freq, a_details = _run_path(
            records, sub_cfg, filter_cfg, mix_targets, "approx")
        e_kept = [d["pair_id"] for d in e_details if d["decision"] == "keep"]
        a_kept = [d["pair_id"] for d in a_details if d["decision"] == "keep"]
        consistency_report = consistency.adjudicate(
            records, e_seg["pairs"], a_seg["pairs"], e_kept, a_kept,
            sub_cfg, e_freq, len(e_vocab), len(a_vocab))

    # Stage 7: versioned snapshots (full replacement on version/checksum change).
    store = SnapshotStore(snapshot_dir)
    versions = config.get("snapshot_versions", {})
    snapshot_actions = {
        "vocab": store.save("vocab", versions.get("vocab", 1), vocab),
        "filter": store.save("filter", versions.get("filter", 1),
                             config.get("filter", {})),
        "mix": store.save("mix", versions.get("mix", 1), mix_targets),
    }

    # Stage 8: export + metrics report + self-check.
    kept_details = [d for d in details if d["decision"] == "keep"]
    dropped_details = [d for d in details if d["decision"] == "drop"]
    seg_by_id = {p["pair_id"]: p for p in seg_summary["pairs"]}
    exported_ids = export_tsv(out_dir / "export.tsv", kept_details, seg_by_id)
    metrics = filtering.metrics_from_details(details)

    check = self_check({
        "ingest_ids": pair_ids,
        "subword_ids": [p["pair_id"] for p in seg_summary["pairs"]],
        "filter_ids": [d["pair_id"] for d in details],
        "kept_ids": [d["pair_id"] for d in kept_details],
        "dropped_ids": [d["pair_id"] for d in dropped_details],
        "exported_ids": exported_ids,
        "vocab": vocab,
        "all_pieces": [p["src_pieces"] + p["tgt_pieces"] for p in seg_summary["pairs"]],
        "details": details,
        "reported_metrics": metrics,
        "batches": batches,
    })

    report = {
        "business_codes": [],
        "config_checksum": config_checksum,
        "input_checksum": ingest_summary["input_checksum"],
        "decision_record": decision_record,
        "stages": {
            "ingest": {k: v for k, v in ingest_summary.items() if k != "pairs"},
            "subword": {k: v for k, v in seg_summary.items() if k != "pairs"},
            "filter": {"strategy": filter_cfg.strategy, "mix_targets": mix_targets},
            "snapshots": snapshot_actions,
        },
        "vocab_size": len(vocab),
        "vocab_checksum": checksum_obj(vocab),
        "metrics": metrics,
        "per_sample_details": details,
        "consistency": consistency_report,
        "self_check": check,
    }
    report["report_checksum"] = checksum_obj(report)
    write_json(out_dir / "pipeline_report.json", report)
    return report
