"""JSON report assembly and replay-divergence comparison."""

from __future__ import annotations

from typing import Any, Dict, List

from .constants import ReplayDiverged
from .engine import full_replay
from .storage import (
    get_watermark,
    load_credentials,
    load_region_states,
    load_verdicts,
)


def build_report(conn, bundle: Dict[str, Any], *, state_dir: str | None = None) -> Dict[str, Any]:
    verdicts = load_verdicts(conn)
    credentials = load_credentials(conn)
    region_states = load_region_states(conn)
    replay = full_replay(bundle)

    states_by_region = {
        (row["endpoint_id"], row["process_guid"], row["region_id"], row["region_generation"]): row
        for row in region_states
    }
    per_region = []
    for region in bundle.get("regions", []):
        key = (
            region["endpoint_id"],
            region["process_guid"],
            region["region_id"],
            region["region_generation"],
        )
        state = states_by_region.get(key)
        region_verdicts = [
            verdict
            for verdict in verdicts
            if verdict.get("region_id") == region["region_id"]
            and verdict.get("process_guid") == region["process_guid"]
        ]
        latest = region_verdicts[-1] if region_verdicts else None
        per_region.append(
            {
                "endpoint_id": region["endpoint_id"],
                "process_guid": region["process_guid"],
                "region_id": region["region_id"],
                "region_generation": region["region_generation"],
                "module": region["module"],
                "map_kind": region["map_kind"],
                "policy_key": region.get("policy_key"),
                "observed_event_count": len(region_verdicts),
                "current_state": state,
                "latest_verdict": latest,
                "conclusion": (latest or {}).get("verdict", "not_observed"),
                "business_code": (latest or {}).get("business_code", "E_NO_EVENT"),
                "credential_id": (latest or {}).get("credential_id"),
                "diff_mask": ((latest or {}).get("diff") or {}).get("xor_mask"),
                "module_closure_id": replay["closures"]["module_closure_by_credential"].get(
                    (latest or {}).get("credential_id")
                ),
                "process_closure_id": replay["closures"]["process_closure_by_credential"].get(
                    (latest or {}).get("credential_id")
                ),
            }
        )

    return {
        "schema": "memguard-report/v1",
        "summary": {
            "watermark_event_seq": get_watermark(conn),
            "valid_events": len(bundle.get("events", [])),
            "adjudicated_events": len(verdicts),
            "credentials": len(credentials),
            "unauthorized": sum(v["verdict"] == "unauthorized" for v in verdicts),
            "compliant": sum(v["verdict"] == "compliant" for v in verdicts),
            "uncertain": sum(v["verdict"] == "uncertain" for v in verdicts),
            "indeterminate": sum(v["verdict"] == "indeterminate" for v in verdicts),
            "input_rejections": len(bundle.get("rejections", [])),
            "policy_conflicts": len(bundle.get("policy_view", {}).get("conflicts", [])),
            "credential_cycles": len(replay["closures"].get("cycles", [])),
        },
        "input_rejections": bundle.get("rejections", []),
        "policy_conflicts": bundle.get("policy_view", {}).get("conflicts", []),
        "per_region": per_region,
        "verdicts": verdicts,
        "credentials": credentials,
        "closures": replay["closures"],
        "full_replay_unauthorized_sync_markers": replay[
            "unauthorized_sync_markers"
        ],
        "state_dir": state_dir,
    }


def assert_replay_matches(conn, bundle: Dict[str, Any]) -> Dict[str, Any]:
    replay = full_replay(bundle)
    stored = load_verdicts(conn)
    stored_by_seq = {int(v["event_seq"]): v for v in stored}
    expected_by_seq = {int(v["event_seq"]): v for v in replay["verdicts"]}

    mismatches = []
    if set(stored_by_seq) != set(expected_by_seq):
        mismatches.append(
            {
                "kind": "event_seq_set",
                "stored": sorted(stored_by_seq),
                "full_replay": sorted(expected_by_seq),
            }
        )
    for seq in sorted(set(stored_by_seq) & set(expected_by_seq)):
        stored_v = stored_by_seq[seq]
        expected_v = expected_by_seq[seq]
        for field in ("verdict", "business_code", "credential_id"):
            if stored_v.get(field) != expected_v.get(field):
                mismatches.append(
                    {
                        "kind": "verdict_field",
                        "event_seq": seq,
                        "field": field,
                        "stored": stored_v.get(field),
                        "full_replay": expected_v.get(field),
                    }
                )

    stored_creds = load_credentials(conn)
    stored_cred_ids = sorted(c["credential_id"] for c in stored_creds)
    expected_cred_ids = sorted(c["credential_id"] for c in replay["credentials"])
    if stored_cred_ids != expected_cred_ids:
        mismatches.append(
            {
                "kind": "credential_set",
                "stored": stored_cred_ids,
                "full_replay": expected_cred_ids,
            }
        )

    expected_module_groups = sorted(
        group["credential_ids"] for group in replay["closures"]["module_closures"]
    )
    stored_module_groups = _stored_module_groups(stored_creds)
    if stored_module_groups != expected_module_groups:
        mismatches.append(
            {
                "kind": "module_closure",
                "stored": stored_module_groups,
                "full_replay": expected_module_groups,
            }
        )

    if mismatches:
        raise ReplayDiverged(
            "recovered ledger differs from deterministic full replay",
            details={"mismatches": mismatches},
        )
    return {
        "consistent": True,
        "compared_events": len(expected_by_seq),
        "credential_ids": expected_cred_ids,
        "unauthorized_sync_markers": replay["unauthorized_sync_markers"],
        "cycles": replay["closures"].get("cycles", []),
        "stored_module_groups": stored_module_groups,
    }


def _stored_module_groups(credentials: List[Dict[str, Any]]) -> List[List[str]]:
    groups: Dict[tuple, List[str]] = {}
    for cred in credentials:
        key = (cred["endpoint_id"], cred["process_guid"], cred.get("module", ""))
        groups.setdefault(key, []).append(cred["credential_id"])
    return sorted(sorted(ids) for ids in groups.values())
