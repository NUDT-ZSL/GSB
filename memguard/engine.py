"""Deterministic adjudication and credential issuance."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .closure import full_closure_report
from .constants import (
    CODE_EXTERNAL_INJECTION,
    CODE_OWNER_DENIED,
    CODE_POLICY_CONFLICT,
    CODE_POLICY_DISABLED,
    CODE_POLICY_MISSING,
    CODE_POLICY_TOMBSTONED,
    CODE_REGION_UNDECLARED,
    CODE_UNCERTAIN,
    CODE_VIEW_MISMATCH,
    CREDENTIAL_VERSION,
    PERMISSION_BITS,
    POLICY_ALGEBRA_VERSION,
    PROT_CALIBER_VERSION,
    PROT_EXEC,
    PROT_WRITE,
)
from .policy import resolve_policy_key, select_policy
from .precision import boundary_analysis, mask_names


EXTERNAL_PATTERN = re.compile(
    r"external|inject|remote|writeprocessmemory|wpm", re.IGNORECASE
)


def is_external_source(write_source: Optional[str]) -> bool:
    return bool(write_source and EXTERNAL_PATTERN.search(write_source))


def _mask_diff(before: int, after: int) -> Dict[str, Any]:
    added = after & ~before & PERMISSION_BITS | (after & ~before & 0x18)
    removed = before & ~after & PERMISSION_BITS | (before & ~after & 0x18)
    xor = before ^ after
    return {
        "before_mask": before,
        "after_mask": after,
        "added_mask": added,
        "removed_mask": removed,
        "xor_mask": xor,
        "added_named": mask_names(added),
        "removed_named": mask_names(removed),
        "write_appended": bool(added & PROT_WRITE),
        "execute_removed": bool(removed & PROT_EXEC),
    }


def _safe_restore_mask(
    before: int,
    initial: int,
    allowed_masks: List[int],
    required_mask: Optional[int],
) -> int:
    allowed = set(allowed_masks)

    def satisfies_required(mask: int) -> bool:
        return required_mask is None or (mask & required_mask) == required_mask

    if before in allowed and satisfies_required(before):
        return before
    if initial in allowed and satisfies_required(initial):
        return initial
    candidates = [mask for mask in allowed if satisfies_required(mask)]
    if candidates:
        return max(candidates)
    if required_mask is not None:
        return required_mask
    return initial


class GuardEngine:
    def __init__(self, bundle: Dict[str, Any]):
        self.bundle = bundle
        self.regions: List[Dict[str, Any]] = bundle.get("regions", [])
        self.region_index = {
            (r["endpoint_id"], r["process_guid"], r["region_id"]): r
            for r in self.regions
        }
        self.policy_view = bundle.get("policy_view", {})

    def adjudicate(self, event: Dict[str, Any]) -> Dict[str, Any]:
        before_prot = event["prot_before"]
        after_prot = event["prot_after"]
        before = int(before_prot["mask"])
        after = int(after_prot["mask"])
        diff = _mask_diff(before, after)
        external = is_external_source(event.get("write_source"))
        base_verdict: Dict[str, Any] = {
            "event_seq": event["event_seq"],
            "sync_marker": event.get("sync_marker"),
            "endpoint_id": event["endpoint_id"],
            "process_guid": event["process_guid"],
            "region_id": event["region_id"],
            "observed_ms": event["observed_ms"],
            "write_source": event["write_source"],
            "external_source": external,
            "diff": diff,
            "raw_prot_before": before_prot.get("raw"),
            "raw_prot_after": after_prot.get("raw"),
            "prot_before_caliber": before_prot.get("caliber"),
            "prot_after_caliber": after_prot.get("caliber"),
        }

        region_key = (
            event["endpoint_id"],
            event["process_guid"],
            event["region_id"],
        )
        region = self.region_index.get(region_key)
        if region is None:
            base_verdict.update(
                {
                    "verdict": "indeterminate",
                    "business_code": CODE_REGION_UNDECLARED,
                    "credential_id": None,
                }
            )
            return base_verdict

        base_verdict.update(
            {
                "region_generation": region["region_generation"],
                "module": region["module"],
                "map_kind": region["map_kind"],
            }
        )

        if (
            event.get("map_kind") != region.get("map_kind")
            or event.get("base_offset") != region.get("base_offset")
            or event.get("region_size") != region.get("region_size")
        ):
            base_verdict.update(
                {
                    "verdict": "indeterminate",
                    "business_code": CODE_VIEW_MISMATCH,
                    "credential_id": None,
                    "expected_region_view": {
                        "map_kind": region.get("map_kind"),
                        "base_offset": region.get("base_offset"),
                        "region_size": region.get("region_size"),
                    },
                }
            )
            return base_verdict

        policy_key = resolve_policy_key(self.policy_view, region)
        selection = select_policy(
            self.policy_view,
            policy_key,
            event["observed_ms"],
            event.get("map_kind"),
        )
        base_verdict["policy_key"] = policy_key

        if selection["status"] == "missing":
            base_verdict.update(
                {
                    "verdict": "indeterminate",
                    "business_code": CODE_POLICY_MISSING,
                    "credential_id": None,
                }
            )
            return base_verdict
        if selection["status"] == "conflict":
            base_verdict.update(
                {
                    "verdict": "indeterminate",
                    "business_code": CODE_POLICY_CONFLICT,
                    "credential_id": None,
                    "conflict_candidates": selection.get("candidates"),
                }
            )
            return base_verdict
        if selection["status"] == "tombstoned":
            base_verdict.update(
                {
                    "verdict": "indeterminate",
                    "business_code": CODE_POLICY_TOMBSTONED,
                    "policy_generation": selection.get("generation"),
                    "credential_id": None,
                }
            )
            return base_verdict
        if selection["status"] == "disabled":
            base_verdict.update(
                {
                    "verdict": "indeterminate",
                    "business_code": CODE_POLICY_DISABLED,
                    "policy_generation": selection.get("generation"),
                    "credential_id": None,
                }
            )
            return base_verdict

        authority = selection["authority"]
        allowed_masks = selection["allowed_masks_resolved"]
        required_mask = authority.get("required_mask")
        base_verdict["policy_generation"] = selection.get("generation")
        base_verdict["policy_algebra_version"] = POLICY_ALGEBRA_VERSION

        initial_prot = region["initial_prot"]["mask"]
        stages = [
            {
                "stage": "region_initial",
                "caliber": region["initial_prot"].get("caliber"),
                "mask": initial_prot,
                "parse_error_bound": region["initial_prot"].get(
                    "parse_error_bound", 0
                ),
            },
            {
                "stage": "policy_upper_limit",
                "caliber": "canonical_v2_mask",
                "mask": authority.get("max_mask"),
                "parse_error_bound": 0,
            },
            {
                "stage": "prot_before",
                "caliber": before_prot.get("caliber"),
                "mask": before,
                "parse_error_bound": before_prot.get("parse_error_bound", 0),
            },
            {
                "stage": "prot_after",
                "caliber": after_prot.get("caliber"),
                "mask": after,
                "parse_error_bound": after_prot.get("parse_error_bound", 0),
            },
        ]
        observation_bound = int(event.get("precision_error_bound", 0) or 0)
        if observation_bound:
            stages.append(
                {
                    "stage": "sender_observation_quantization",
                    "caliber": "declared_error_bound",
                    "mask": after,
                    "error_bound": observation_bound,
                }
            )
        precision = boundary_analysis(
            stages,
            {"before": before, "after": after},
            allowed_masks,
        )
        base_verdict["precision"] = precision
        base_verdict["time_boundary_uncertain"] = selection.get(
            "time_boundary_uncertain", False
        )
        base_verdict["time_quantization_error_bound_ms"] = selection.get(
            "time_quantization_error_bound_ms", 0
        )

        if selection.get("time_boundary_uncertain"):
            base_verdict.update(
                {
                    "verdict": "uncertain",
                    "business_code": CODE_UNCERTAIN,
                    "uncertainty_reason": "v1_second_to_ms_quantization_boundary",
                    "credential_id": None,
                }
            )
            return base_verdict

        if precision["boundary_crossed"]:
            central_allowed = after in set(allowed_masks)
            base_verdict.update(
                {
                    "verdict": "uncertain",
                    "business_code": CODE_UNCERTAIN,
                    "uncertainty_reason": "protection_mask_error_bound_crosses_boundary",
                    "central_conclusion": (
                        "allowed" if central_allowed else "unauthorized"
                    ),
                    "alternative_conclusion": (
                        "unauthorized" if central_allowed else "allowed"
                    ),
                    "boundary_margin": precision["boundary_margin"],
                    "credential_id": None,
                }
            )
            return base_verdict

        after_required = required_mask is None or (after & required_mask) == required_mask
        policy_denied = after not in set(allowed_masks) or not after_required
        changed = before != after

        if not policy_denied and not (external and changed):
            base_verdict.update(
                {
                    "verdict": "compliant",
                    "business_code": "E_OK",
                    "policy_denied": False,
                    "credential_id": None,
                    "allowed_masks": allowed_masks,
                }
            )
            return base_verdict

        business_code = (
            CODE_EXTERNAL_INJECTION if external else CODE_OWNER_DENIED
        )
        credential_id = f"CRED-E{event['event_seq']:05d}"
        restore_mask = _safe_restore_mask(
            before, initial_prot, allowed_masks, required_mask
        )
        credential = {
            "credential_id": credential_id,
            "credential_version": CREDENTIAL_VERSION,
            "prot_caliber_version": PROT_CALIBER_VERSION,
            "policy_algebra_version": POLICY_ALGEBRA_VERSION,
            "endpoint_id": event["endpoint_id"],
            "process_guid": event["process_guid"],
            "region_id": event["region_id"],
            "region_generation": region["region_generation"],
            "module": region["module"],
            "event_seq": event["event_seq"],
            "sync_marker": event.get("sync_marker"),
            "target": {
                "endpoint_id": event["endpoint_id"],
                "process_guid": event["process_guid"],
                "region_id": event["region_id"],
                "region_generation": region["region_generation"],
                "module": region["module"],
                "base_offset": region["base_offset"],
                "region_size": region["region_size"],
            },
            "restore_prot_mask": restore_mask,
            "restore_prot_named": mask_names(restore_mask),
            "basis_policy_key": policy_key,
            "basis_policy_generation": selection.get("generation"),
            "business_code": business_code,
            "added_mask": diff["added_mask"],
            "removed_mask": diff["removed_mask"],
            "xor_mask": diff["xor_mask"],
            "write_source": event["write_source"],
            "issued_at_observed_ms": event["observed_ms"],
            "status": "issued",
        }
        base_verdict.update(
            {
                "verdict": "unauthorized",
                "business_code": business_code,
                "policy_denied": policy_denied,
                "credential_id": credential_id,
                "credential": credential,
                "allowed_masks": allowed_masks,
            }
        )
        return base_verdict


def full_replay(bundle: Dict[str, Any]) -> Dict[str, Any]:
    engine = GuardEngine(bundle)
    verdicts = [engine.adjudicate(event) for event in bundle.get("events", [])]
    credentials = [
        verdict["credential"]
        for verdict in verdicts
        if verdict.get("credential")
    ]
    closures = full_closure_report(credentials, bundle.get("regions", []))
    return {
        "verdicts": verdicts,
        "credentials": credentials,
        "closures": closures,
        "unauthorized_sync_markers": sorted(
            verdict.get("sync_marker")
            for verdict in verdicts
            if verdict["verdict"] == "unauthorized"
            and verdict.get("sync_marker")
        ),
    }
