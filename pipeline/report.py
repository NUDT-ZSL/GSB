"""Stable sort keys and deterministic report assembly."""

from __future__ import annotations

from typing import Any


def task_sort_key(sample_name: str, round_index: int) -> list[Any]:
    """Stable key derived solely from sample name and round index.

    Every collection in the report is ordered through this key (or a
    sub-key consistent with it), so thread scheduling can never influence
    output ordering.
    """
    return [sample_name, round_index]


def failure_sort_key(record: dict) -> list[Any]:
    return [record.get("sample") or "", record.get("round", -1),
            record.get("stage", ""), record.get("code", ""),
            record.get("injection_id") or ""]


def hit_sort_key(record: dict) -> list[Any]:
    return [record["sample"], record["round"], record["injection_id"]]


def build_report(config_dict: dict, config_hash: str, relation_hash: str,
                 injection_hash: str, sample_results: list[dict],
                 failures: list[dict], injection_hits: list[dict],
                 snapshot_version: int, static_dangling: list[dict],
                 manifest: list[str], diagnostics: dict) -> dict:
    """Assemble the final report with fixed, deterministic key order."""
    accepted = sum(1 for item in sample_results
                   if item["final_decision"] == "ACCEPTED")
    rejected = sum(1 for item in sample_results
                   if item["final_decision"] == "REJECTED")
    summary = {
        "samples_total": len(manifest),
        "samples_completed": len(sample_results),
        "samples_failed": len({item["sample"] for item in failures
                               if item.get("sample")}),
        "accepted": accepted,
        "rejected": rejected,
        "failure_records": len(failures),
        "injection_hits": len(injection_hits),
        "static_dangling_relations": len(static_dangling),
        "max_rounds": config_dict["max_rounds"],
    }
    return {
        "report_version": 1,
        "decision_config": {
            key: value for key, value in config_dict.items()
            if key != "concurrency"
        },
        "hashes": {
            "config_hash": config_hash,
            "relation_hash": relation_hash,
            "injection_hash": injection_hash,
        },
        "diagnostics": diagnostics,
        "sample_manifest": list(manifest),
        "samples": sample_results,
        "injection_hits": sorted(injection_hits, key=hit_sort_key),
        "static_dangling_relations": static_dangling,
        "failures": sorted(failures, key=failure_sort_key),
        "statistics": summary,
        "snapshot_version": snapshot_version,
    }
