"""Cross-module consistency self-checks (requirement 8)."""

from .cache import stage_cache_parts, build_cache_key
from .errors import ErrorCode, OpChainError
from .hashing import hash_array
from .task import generate_input
from .blocks import run_chunked_chain


def verify_snapshot_envelope(envelope):
    required = {"kind", "version", "payload", "checksum"}
    missing = required.difference(envelope)
    if missing:
        raise OpChainError(
            ErrorCode.SNAPSHOT_CORRUPT,
            "snapshot envelope is missing required fields",
            {"missing": sorted(missing)},
        )
    if not isinstance(envelope["version"], int) or envelope["version"] < 1:
        raise OpChainError(
            ErrorCode.SNAPSHOT_CORRUPT,
            "snapshot version must be a positive monotonic integer",
            {"version": envelope["version"]},
        )


def verify_cache_keys(specs, envelope, config):
    """Recompute every cache key and product hash from first principles."""
    entries = envelope["payload"]["cache_entries"]
    by_case = {}
    for spec in specs:
        tensor = generate_input(spec)
        input_hash = hash_array(tensor)
        block_size = _selected_block_size(spec, envelope)
        chunked = run_chunked_chain(tensor, spec, block_size)
        expected_keys = [
            build_cache_key(
                stage_cache_parts(
                    spec, stage, input_hash, block_size, spec.dtype
                )
            )
            for stage in range(len(spec.op_chain))
        ]
        by_case[spec.case_id] = {
            "keys": expected_keys,
            "hashes": [record["content_hash"] for record in chunked["stages"]],
        }

    seen = set()
    for entry in entries:
        if entry["key"] in seen:
            raise OpChainError(
                ErrorCode.INCONSISTENT,
                "duplicate cache key detected",
                {"key": entry["key"]},
            )
        seen.add(entry["key"])
        expected = by_case.get(entry["case_id"])
        if expected is None or entry["key"] not in expected["keys"]:
            raise OpChainError(
                ErrorCode.INCONSISTENT,
                "cache key cannot be reproduced from declared inputs",
                {"entry": entry},
            )
        stage = entry["stage"]
        if entry["content_hash"] != expected["hashes"][stage]:
            raise OpChainError(
                ErrorCode.INCONSISTENT,
                "cached product hash does not match a recomputed stage",
                {"case_id": entry["case_id"], "stage": stage},
            )
    return {"checked_entries": len(entries), "cases": sorted(by_case)}


def _selected_block_size(spec, envelope):
    for result in envelope["payload"]["results"]:
        if result["case_id"] == spec.case_id:
            return result["execution_plan"]["block_adjudication"][
                "selected_block_size"
            ]
    raise OpChainError(
        ErrorCode.INCONSISTENT,
        f"case {spec.case_id} missing from snapshot during self-check",
    )


def verify_invalidation_plan(plan):
    reused = set(plan["reused_keys"])
    invalidated = set(plan["invalidated_keys"])
    overlap = reused & invalidated
    if overlap:
        raise OpChainError(
            ErrorCode.INCONSISTENT,
            "reused and invalidated sets intersect",
            {"intersection": sorted(overlap)},
        )
    affected = set(plan["affected_keys"])
    union = reused | invalidated
    if union != affected:
        raise OpChainError(
            ErrorCode.INCONSISTENT,
            "union of reused and invalidated sets does not cover affected entries",
            {"union_only": sorted(union - affected), "affected_only": sorted(affected - union)},
        )
    return {"reused": len(reused), "invalidated": len(invalidated)}


def verify_replay(spec, envelope, config):
    """Re-run the chain and confirm the final output reproduces the snapshot."""
    block_size = _selected_block_size(spec, envelope)
    tensor = generate_input(spec)
    chunked = run_chunked_chain(tensor, spec, block_size)
    saved = next(
        item for item in envelope["payload"]["results"] if item["case_id"] == spec.case_id
    )
    if chunked["final_hash"] != saved["final_hash"]:
        raise OpChainError(
            ErrorCode.INCONSISTENT,
            "replayed final output does not match the snapshot",
            {"case_id": spec.case_id},
        )
    for recomputed, stored in zip(chunked["stages"], saved["stages"]):
        if recomputed["content_hash"] != stored["content_hash"]:
            raise OpChainError(
                ErrorCode.INCONSISTENT,
                "replayed intermediate hash does not match the snapshot",
                {"case_id": spec.case_id, "stage": stored["stage"]},
            )
    return {
        "case_id": spec.case_id,
        "final_hash": chunked["final_hash"],
        "stage_count": len(chunked["stages"]),
    }


def run_full_selfcheck(specs, envelope, config, invalidation_plan=None):
    verify_snapshot_envelope(envelope)
    key_report = verify_cache_keys(specs, envelope, config)
    replay_reports = [verify_replay(spec, envelope, config) for spec in specs]
    report = {
        "code": ErrorCode.OK,
        "cache_key_check": key_report,
        "replay_check": replay_reports,
    }
    if invalidation_plan is not None:
        report["invalidation_check"] = verify_invalidation_plan(invalidation_plan)
    return report
