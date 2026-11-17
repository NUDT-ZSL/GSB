"""JSON diagnostics report assembly and atomic persistence."""

from __future__ import annotations

from .cache import atomic_write_json


def build_report(run_result):
    return {
        "snapshot_version": run_result["snapshot_version"],
        "snapshot": run_result["snapshot"],
        "policy_source_version": run_result["policy_source_version"],
        "policy_fingerprint": run_result["policy_fingerprint"],
        "score_fingerprint": run_result["score_fingerprint"],
        "requests": run_result["requests"],
        "decoder_summaries": run_result["decoder_summaries"],
        "downgrade_losses": run_result["downgrade_losses"],
        "cache_stats": run_result["cache_stats"],
        "conversions": run_result["conversions"],
        "injection_hits": run_result["injection_hits"],
        "failures": run_result["failures"],
        "stopped": run_result["stopped"],
    }


def write_report(path, run_result):
    atomic_write_json(path, build_report(run_result))
