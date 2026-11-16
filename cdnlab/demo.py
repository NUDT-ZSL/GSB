"""Repeatable demo: four diagnostic scenarios, each printing a structured
JSON summary. Everything runs on loopback with deterministic sample content
and a logical clock, so repeated runs produce identical traces.

Scenarios:
  1. cold_start        - cold cache fill, then fresh hits with decaying TTL
  2. origin_update     - injected origin update (E_STALE) + revalidation
  3. eviction_pressure - injected pressure (E_EVICT) causing evictions
  4. restore_replay    - snapshot, restore, replay verification
"""

from __future__ import annotations

import json
import os
import sys

from .orchestrator import Orchestrator, find_free_ports
from .replay import replay_snapshot

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO_ROOT, "samples")
CONFIG = os.path.join(SAMPLES, "cache_policy.json")
CONTENT = os.path.join(SAMPLES, "content")
OUT_DIR = os.path.join(REPO_ROOT, "artifacts", "demo")


def _make_orch(tag, num_edges=3):
    os.makedirs(OUT_DIR, exist_ok=True)
    ports = find_free_ports(num_edges + 1)
    return Orchestrator(
        CONFIG, CONTENT,
        db_path=os.path.join(OUT_DIR, f"{tag}.sqlite"),
        num_edges=num_edges, ports=ports,
        log_path=os.path.join(OUT_DIR, f"{tag}.log"),
    )


def _fresh_db(path):
    if os.path.exists(path):
        os.remove(path)


def scenario_cold_start():
    tag = "scenario1_cold_start"
    _fresh_db(os.path.join(OUT_DIR, f"{tag}.sqlite"))
    with _make_orch(tag) as orch:
        results = [
            orch.request(0, "/news.txt"),                 # MISS_COLD
            orch.request(0, "/news.txt"),                 # HIT_FRESH
            orch.request(0, "/news.txt"),                 # HIT_FRESH, ttl decaying
            orch.request(1, "/article.txt", {"lang": "zh"}),  # MISS_COLD
            orch.request(1, "/article.txt", {"lang": "zh"}),  # HIT_FRESH same entry
            orch.request(1, "/article.txt", {"lang": "en"}),  # MISS_COLD other key
            orch.request(2, "/banner.txt", {"device": "pc"}),
            orch.request(2, "/banner.txt", {"device": "pc"}),
        ]
        state = [orch.store.load_trace()]
        summary = {
            "scenario": tag,
            "config_version": orch.policy.config_version,
            "outcomes": [r.get("outcome") for r in results],
            "remaining_ttl_trace": [r.get("remaining_ttl") for r in results[:3]],
            "key_distinct_miss": results[5].get("outcome"),
            "consistent": all(r.get("consistency") == "CONSISTENT" for r in results),
        }
    return summary


def scenario_origin_update():
    tag = "scenario2_origin_update"
    _fresh_db(os.path.join(OUT_DIR, f"{tag}.sqlite"))
    with _make_orch(tag) as orch:
        first = orch.request(0, "/news.txt")              # MISS_COLD, v1
        hit = orch.request(0, "/news.txt")                # HIT_FRESH v1
        injection = orch.inject_origin_update(
            "/news.txt",
            "CDN-LAB SAMPLE news v2: injected origin update for staleness checks.\n",
        )
        # TTL not yet expired and no force flag: still a fresh hit on v1.
        stale_window = orch.request(0, "/news.txt")
        # Force revalidation: entry replaced wholesale, entry_version bumped.
        revalidated = orch.request(0, "/news.txt", force_validate=True)
        after = orch.request(0, "/news.txt")
        # TTL expiry path on another node.
        orch.request(1, "/news.txt")
        orch.advance(5)  # let ttl=4 expire on the single logical clock
        expired = orch.request(1, "/news.txt")
        summary = {
            "scenario": tag,
            "injection_code": injection["code"],
            "origin_version_after_update": injection["origin_version"],
            "etag_before": first["etag"],
            "etag_after_revalidate": revalidated["etag"],
            "entry_version_bumped": revalidated["entry_version"] == first["entry_version"] + 1,
            "revalidated_outcome": revalidated["outcome"],
            "expired_revalidate_reason": expired.get("revalidate_reason"),
            "version_changed": first["etag"] != revalidated["etag"],
            "post_revalidate_hit": after["outcome"],
        }
    return summary


def scenario_eviction_pressure():
    tag = "scenario3_eviction_pressure"
    _fresh_db(os.path.join(OUT_DIR, f"{tag}.sqlite"))
    with _make_orch(tag) as orch:
        orch.request(0, "/article.txt", {"lang": "zh"})   # capacity 3, lru
        orch.request(0, "/article.txt", {"lang": "en"})
        orch.request(0, "/article.txt", {"lang": "ja"})
        # Injected pressure: 3 extra distinct keys evict everything above.
        orch.inject_eviction_pressure(
            0, "/article.txt", 3,
            fields_factory=lambda i: {"lang": f"f{i}"},
        )
        evicted_refetch = orch.request(0, "/article.txt", {"lang": "zh"})
        evictions = orch.store.load_evictions()
        summary = {
            "scenario": tag,
            "eviction_events": len(evictions),
            "eviction_rule": evictions[0]["rule"] if evictions else None,
            "first_evicted_key": evictions[0]["key"] if evictions else None,
            "refetch_outcome_after_eviction": evicted_refetch["outcome"],
            "injection_code": "E_EVICT",
        }
    return summary


def scenario_restore_replay():
    tag = "scenario4_restore_replay"
    _fresh_db(os.path.join(OUT_DIR, f"{tag}.sqlite"))
    with _make_orch(tag) as orch:
        orch.request(0, "/news.txt")
        orch.request(0, "/news.txt")
        orch.inject_origin_update(
            "/news.txt",
            "CDN-LAB SAMPLE news v2: injected origin update for staleness checks.\n",
        )
        orch.request(0, "/news.txt", force_validate=True)
        orch.request(1, "/article.txt", {"lang": "zh"})
        orch.request(1, "/article.txt", {"lang": "zh"})
        snapshot_id = orch.save_snapshot("demo-snapshot")
        # Disturb the live state, then restore + replay.
        orch.request(2, "/banner.txt", {"device": "pc"})
        verdict = replay_snapshot(orch, snapshot_id)
        # Reproducibility: replaying again must give the identical verdict.
        verdict2 = replay_snapshot(orch, snapshot_id)
        summary = {
            "scenario": tag,
            "snapshot_id": snapshot_id,
            "replay_ok": verdict["ok"],
            "requests_compared": verdict["compared"],
            "first_diff_seq": verdict["first_diff_seq"],
            "replay_reproducible": verdict == verdict2,
        }
    return summary


def main():
    summaries = [
        scenario_cold_start(),
        scenario_origin_update(),
        scenario_eviction_pressure(),
        scenario_restore_replay(),
    ]
    for summary in summaries:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    out_path = os.path.join(OUT_DIR, "summary.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(summaries, handle, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"demo summaries written to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
