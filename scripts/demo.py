#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alertlab.service import AlertLabService
from alertlab.util import pretty_json


def main() -> None:
    database = ROOT / "data" / "demo.sqlite3"
    service = AlertLabService(ROOT, database_path=database)
    service.store.reset()
    loaded = service.load()
    before = service.status()
    patch = json.loads(
        (ROOT / "samples" / "patches" / "dependency_patch.json").read_text(
            encoding="utf-8"
        )
    )
    patched = service.patch(patch)
    idempotent = service.patch(patch)
    after = service.status()
    verification = service.verify()

    unchanged_ids = {
        cluster["cluster_id"]
        for cluster in before["clusters"]
        if cluster["rule_name"] != "GatewayLatency"
    }
    unchanged_after = {
        cluster["cluster_id"]: cluster
        for cluster in after["clusters"]
        if cluster["cluster_id"] in unchanged_ids
    }
    unchanged_before = {
        cluster["cluster_id"]: cluster
        for cluster in before["clusters"]
        if cluster["cluster_id"] in unchanged_ids
    }
    gateway_before = next(
        cluster
        for cluster in before["clusters"]
        if cluster["rule_name"] == "GatewayLatency"
    )
    gateway_after = next(
        cluster
        for cluster in after["clusters"]
        if cluster["rule_name"] == "GatewayLatency"
    )
    report = {
        "loaded": loaded["load"],
        "before": {
            "rule_view": before["rule_view"],
            "summary": before["summary"],
            "representatives": [
                {
                    "rule_name": cluster["rule_name"],
                    "service": cluster["service"],
                    "representative_event_uid": cluster["representative"]["event_uid"],
                    "representative_seq": cluster["representative"]["seq"],
                    "occurrence_count": cluster["occurrence_count"],
                    "suppressed_count": cluster["suppressed_count"],
                }
                for cluster in before["clusters"]
            ],
            "late_events": before["late_events"],
        },
        "patch": {
            "patch_id": patched["patch_id"],
            "applied": patched["applied"],
            "invalidated_cluster_count": len(patched["invalidated"]["clusters"]),
            "invalidated_candidate_count": len(
                patched["invalidated"]["root_candidates"]
            ),
            "reused_cluster_count": len(patched["reused"]["clusters"]),
            "reused_candidate_count": len(patched["reused"]["root_candidates"]),
            "idempotent_repeat": idempotent["idempotent"],
        },
        "reuse_check": {
            "unaffected_clusters_byte_identical": unchanged_before == unchanged_after,
            "gateway_summary_byte_identical": {
                key: gateway_before[key]
                for key in gateway_before
                if key != "root_candidates"
            }
            == {
                key: gateway_after[key]
                for key in gateway_after
                if key != "root_candidates"
            },
            "gateway_candidates_changed": gateway_before["root_candidates"]
            != gateway_after["root_candidates"],
            "gateway_after_services": sorted(
                {
                    candidate["service"]
                    for candidate in gateway_after["root_candidates"]
                }
            ),
        },
        "verify": verification,
    }
    print(pretty_json(report))


if __name__ == "__main__":
    main()
