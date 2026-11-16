"""Deterministic JSON audit report construction."""

import hashlib
import json

from .hashing import FINGERPRINT_VERSION, canonical_json


def fingerprint_digest(engine):
    material = canonical_json(
        [
            result.get("chain_fps", [])
            for result in sorted(
                engine.request_results,
                key=lambda item: item.get("sort_key", []),
            )
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_report(
    engine,
    *,
    run_id,
    mode,
    request_total,
    injection_config,
    stop_reason,
):
    store = engine.store
    budget = int(engine.config["residency_budget_blocks"])
    success = [r for r in engine.request_results if r.get("status") != "failed"]
    sessions = {}
    for result in sorted(success, key=lambda item: item["sort_key"]):
        name = result["session"]
        entry = sessions.setdefault(
            name,
            {
                "session": name,
                "requests": 0,
                "total_blocks": 0,
                "declared_reusable_blocks": 0,
                "resident_reusable_blocks": 0,
                "recomputed_blocks": 0,
                "chain_fps": [],
                "reuse_ranges": [],
            },
        )
        entry["requests"] += 1
        entry["total_blocks"] += result["block_count"]
        entry["declared_reusable_blocks"] += result[
            "declared_reusable_blocks"
        ]
        entry["resident_reusable_blocks"] += result[
            "resident_reusable_blocks"
        ]
        entry["recomputed_blocks"] += len(result["recomputed_chain_fps"])
        entry["chain_fps"].extend(result["chain_fps"])
        entry["reuse_ranges"].append(
            {
                "turn": result["turn"],
                "reusable_range": result["reusable_range"],
                "new_range": result["new_range"],
            }
        )

    evict_events = [e for e in engine.events if e["type"] == "EVICT"]
    invalidate_events = [e for e in engine.events if e["type"] == "INVALIDATE"]
    recompute_events = [e for e in engine.events if e["type"] == "RECOMPUTE"]
    chain_broken_events = [
        e for e in engine.events if e["type"] == "E_CHAIN_BROKEN"
    ]
    injection_hits = sorted(
        {
            e.get("injection_id")
            for e in engine.events
            if e.get("injection_id")
        }
        | {f.get("injection_id") for f in engine.failures if f.get("injection_id")}
    )
    resident_blocks = sorted(
        fp for fp, block in store.registry.items() if block.resident
    )
    invalid_blocks = sorted(
        fp for fp, block in store.registry.items() if block.invalidated
    )

    report = {
        "run_id": run_id,
        "mode": mode,
        "fingerprint_version": FINGERPRINT_VERSION,
        "config": engine.config,
        "config_fp": engine.config_fp,
        "vocab_fp": engine.vocab_fp,
        "chunking": {
            "block_tokens": engine.config["block_tokens"],
            "hash_derive": engine.config["hash_derive"],
            "min_reusable_blocks": engine.config["min_reusable_blocks"],
        },
        "residency": {
            "budget_blocks": budget,
            "peak_resident_blocks": store.peak_resident,
            "final_resident_blocks": len(resident_blocks),
            "budget_utilization_peak": round(
                store.peak_resident / budget, 6
            ) if budget else None,
            "final_resident_chain_fps": resident_blocks,
            "invalid_chain_fps": invalid_blocks,
            "utilization_samples": store.budget_utilization_events,
        },
        "sessions": [sessions[key] for key in sorted(sessions)],
        "events": engine.events,
        "event_counts": {
            "evict": len(evict_events),
            "invalidate": len(invalidate_events),
            "recompute": len(recompute_events),
            "chain_broken": len(chain_broken_events),
        },
        "failures": engine.failures,
        "injection_hits": injection_hits,
        "injection_config_fp": hashlib.sha256(
            canonical_json(injection_config).encode("utf-8")
        ).hexdigest(),
        "snapshot": {
            "version": engine.snapshot_version,
            "recovery": engine.recovery,
            "commit_interval": engine.config["snapshot_commit_interval"],
        },
        "request_cursor": engine.cursor,
        "request_total": request_total,
        "stop_reason": stop_reason,
        "chain_fingerprint_digest": fingerprint_digest(engine),
    }
    return report


def write_report(path, report):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, sort_keys=True, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_report(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
