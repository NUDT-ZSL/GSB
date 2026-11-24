"""High-level deterministic use cases used by the CLI and tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import LEDGER_LAYOUT_VERSION
from .codes import (
    E_CHECKPOINT_MISMATCH,
    E_CHECKPOINT_RESIDUE,
    AuthzParameterError,
    PreconditionError,
)
from .engine import adjudicate_bundle
from .graph import build_closure_report
from .jsonio import atomic_write_json, short_marker
from .parsing import load_bundle
from .policy import build_unified_view
from .storage import LedgerStore, reset_state_dir


def load_view(input_dir: str | Path, workers: int = 1):
    path = Path(input_dir)
    if not path.exists():
        raise AuthzParameterError(f"input directory does not exist: {path}")
    bundle = load_bundle(path, workers=workers)
    view = build_unified_view(bundle)
    bundle.rejections.extend(view.get("policy_rejections", []))
    return bundle, view


def partial_reasons(bundle: Any, view: dict[str, Any], closure: dict[str, Any] | None = None) -> list[str]:
    reasons: list[str] = []
    if bundle.rejections:
        reasons.append("declaration_rejections")
    if view.get("conflicts"):
        reasons.append("policy_conflicts")
    if closure and closure.get("cycles"):
        reasons.append("bind_cycles")
    return reasons


def write_json_output(output: str | Path | None, payload: dict[str, Any], default_path: Path | None = None) -> Path:
    target = Path(output) if output else default_path
    if target is None:
        raise AuthzParameterError("an output path is required")
    atomic_write_json(target, payload)
    return target


def run_import(input_dir: Path, state_dir: Path, output: Path | None, workers: int = 1) -> dict[str, Any]:
    bundle, view = load_view(input_dir, workers)
    store = LedgerStore(state_dir)
    store.initialize(
        bundle.snapshot_version,
        bundle.observation_clock_ms,
        LEDGER_LAYOUT_VERSION,
    )
    summary = _bundle_summary(bundle)
    payload = {
        "command": "import",
        "status": "partial_failure" if bundle.rejections or view["conflicts"] else "success",
        "sync_marker": "",
        "input_dir": str(input_dir),
        "snapshot_version": bundle.snapshot_version,
        "observation_clock_ms": bundle.observation_clock_ms,
        "summary": summary,
        "rejections": bundle.rejections,
        "policy_conflicts": view["conflicts"],
        "unified_policies": {
            key: {
                "generation": value["generation"],
                "source": value["source"],
                "expires_at_ms": value["expires_at_ms"],
                "required_factors": value["required_factors"],
                "binding_requirement": value["binding_requirement"],
            }
            for key, value in view["policies"].items()
        },
        "bindings_count": len(view["bindings"]),
    }
    payload["sync_marker"] = _import_sync_marker(payload)
    target = output or store.report_dir / "import-report.json"
    write_json_output(output, payload, store.report_dir / "import-report.json")
    store.save_artifact("import_report", payload, bundle.observation_clock_ms)
    payload["report_path"] = str(target)
    return payload


def run_adjudicate(
    input_dir: Path,
    state_dir: Path,
    watermark: int | None = None,
    workers: int = 1,
    rebuild: bool = False,
    output: Path | None = None,
) -> dict[str, Any]:
    bundle, view = load_view(input_dir, workers)
    store = LedgerStore(state_dir)
    if rebuild:
        reset_state_dir(state_dir)
    store.initialize(
        bundle.snapshot_version,
        bundle.observation_clock_ms,
        LEDGER_LAYOUT_VERSION,
    )

    verification = store.verify_checkpoint()
    recovery: dict[str, Any] = {"recovered": False}
    blocking_issues = [
        issue for issue in verification.get("issues", [])
        if issue.get("code") == E_CHECKPOINT_MISMATCH
        and issue.get("scope") == "sqlite_ledger"
    ]
    if blocking_issues:
        raise PreconditionError(
            E_CHECKPOINT_MISMATCH,
            "checkpoint/ledger consistency check failed",
            {"issues": blocking_issues},
        )
    recoverable_issues = [
        issue for issue in verification.get("issues", [])
        if issue.get("code") in {E_CHECKPOINT_RESIDUE, E_CHECKPOINT_MISMATCH}
    ]
    if recoverable_issues:
        recovery = store.recover_checkpoint()
        recovery["recovered"] = True
    elif verification.get("stale_temp_files"):
        recovery = {
            "recovered": False,
            "stale_temp_files": verification["stale_temp_files"],
            "note": "temporary files are never used as checkpoints",
        }

    with store.connect() as connection:
        checkpoint = store.last_checkpoint(connection)
    last_watermark = int(checkpoint["watermark"]) if checkpoint else 0
    attempt_watermarks = [item["batch_watermark"] for item in bundle.valid_attempts]
    target_watermark = int(watermark if watermark is not None else max(attempt_watermarks or [0]))
    if target_watermark <= 0:
        raise AuthzParameterError("a positive --watermark is required when no attempts exist")
    if target_watermark < last_watermark:
        raise AuthzParameterError(
            f"requested watermark {target_watermark} is older than checkpoint {last_watermark}"
        )

    if target_watermark == last_watermark and not rebuild:
        all_rows = store.list_ledger()
        results = [row for row in all_rows if row.get("batch_watermark") == target_watermark]
        committed_checkpoint = checkpoint
        idempotent = True
    else:
        lower_bound = 0 if rebuild else last_watermark
        pending = [
            item
            for item in bundle.valid_attempts
            if lower_bound < item["batch_watermark"] <= target_watermark
        ]
        results = adjudicate_bundle(bundle, view, workers=workers, attempts=pending)
        committed_checkpoint = store.commit_adjudication(
            results,
            target_watermark,
            bundle.snapshot_version,
            bundle.observation_clock_ms,
            extra_checkpoint={"workers": workers, "rebuild": rebuild},
        )
        idempotent = False

    closure = build_closure_report(bundle, view, watermark=target_watermark)
    counts = {
        "total": len(results),
        "allow": sum(1 for item in results if item["decision"] == "ALLOW"),
        "deny": sum(1 for item in results if item["decision"] == "DENY"),
    }
    status = "success"
    if partial_reasons(bundle, view, closure):
        status = "partial_failure"
    payload = {
        "command": "adjudicate",
        "status": status,
        "sync_marker": "",
        "input_dir": str(input_dir),
        "state_dir": str(state_dir),
        "snapshot_version": bundle.snapshot_version,
        "target_watermark": target_watermark,
        "last_checkpoint_watermark_before": last_watermark,
        "idempotent_replay": idempotent,
        "recovery": recovery,
        "checkpoint": committed_checkpoint,
        "summary": counts,
        "results": results,
        "rejections": bundle.rejections,
        "policy_conflicts": view["conflicts"],
        "invalidation_closure": closure,
    }
    payload["sync_marker"] = _adjudication_sync_marker(payload)
    default_report = store.report_dir / f"adjudication-{target_watermark:06d}.json"
    target = write_json_output(output, payload, default_report)
    atomic_write_json(store.report_dir / "adjudication-latest.json", payload)
    store.save_artifact("latest_adjudication", payload, bundle.observation_clock_ms)
    payload["report_path"] = str(target)
    return payload


def run_revoke(
    input_dir: Path,
    state_dir: Path,
    watermark: int | None = None,
    workers: int = 1,
    output: Path | None = None,
) -> dict[str, Any]:
    bundle, view = load_view(input_dir, workers)
    store = LedgerStore(state_dir)
    store.initialize(
        bundle.snapshot_version,
        bundle.observation_clock_ms,
        LEDGER_LAYOUT_VERSION,
    )
    event_watermarks = [event["batch_watermark"] for event in bundle.revocations]
    target_watermark = int(watermark if watermark is not None else max(event_watermarks or [0]))
    closure = build_closure_report(bundle, view, watermark=target_watermark)
    recomputed = adjudicate_bundle(bundle, view, workers=workers)
    consistency = compare_rejudgement(store, recomputed)
    status = "success"
    if partial_reasons(bundle, view, closure) or not consistency["consistent"]:
        status = "partial_failure"
    payload = {
        "command": "revoke-propagation",
        "status": status,
        "sync_marker": "",
        "watermark": target_watermark,
        "closure": closure,
        "rejudgement_consistency": consistency,
        "recomputed_results": recomputed,
        "rejections": bundle.rejections,
        "policy_conflicts": view["conflicts"],
    }
    payload["sync_marker"] = _revocation_sync_marker(payload)
    default_path = store.report_dir / f"revocation-{target_watermark:06d}.json"
    target = write_json_output(output, payload, default_path)
    atomic_write_json(store.report_dir / "revocation-latest.json", payload)
    store.save_artifact("latest_revocation", payload, bundle.observation_clock_ms)
    payload["report_path"] = str(target)
    return payload


def compare_rejudgement(store: LedgerStore, recomputed: list[dict[str, Any]]) -> dict[str, Any]:
    stored = store.list_ledger()
    if not stored:
        return {"consistent": True, "ledger_present": False, "checked": 0, "mismatches": []}
    stored_map = {(row["user_id"], row["attempt_seq"]): row for row in stored}
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for result in recomputed:
        key = (result["user_id"], result["attempt_seq"])
        existing = stored_map.get(key)
        if existing is None:
            continue
        checked += 1
        if existing.get("result_hash") != result.get("result_hash"):
            mismatches.append({"user_id": key[0], "attempt_seq": key[1]})
    return {
        "consistent": not mismatches,
        "ledger_present": True,
        "checked": checked,
        "ledger_rows": len(stored),
        "mismatches": mismatches,
    }


def run_report(state_dir: Path, output: Path | None = None) -> dict[str, Any]:
    store = LedgerStore(state_dir)
    verification = store.verify_checkpoint()
    attempts = store.list_ledger()
    closure = store.load_artifact("latest_revocation") or store.load_artifact("latest_adjudication")
    if closure and "closure" in closure:
        closure_payload = closure["closure"]
    elif closure and "invalidation_closure" in closure:
        closure_payload = closure["invalidation_closure"]
    else:
        closure_payload = None
    status = "success" if verification["ok"] else "partial_failure"
    payload = {
        "command": "export-diagnostic-report",
        "status": status,
        "sync_marker": "",
        "checkpoint_verification": verification,
        "attempt_count": len(attempts),
        "attempts": attempts,
        "invalidation_closure": closure_payload,
    }
    payload["sync_marker"] = short_marker(
        {
            "attempts": payload["attempts"],
            "checkpoint_digest": payload["checkpoint_verification"].get("ledger_digest"),
            "closure": payload["invalidation_closure"],
        }
    )
    target = write_json_output(output, payload, store.report_dir / "diagnostic-report.json")
    payload["report_path"] = str(target)
    atomic_write_json(store.report_dir / "diagnostic-report.json", payload)
    return payload


def run_verify_checkpoint(state_dir: Path, recover: bool = False) -> dict[str, Any]:
    store = LedgerStore(state_dir)
    result = store.verify_checkpoint()
    if recover:
        result["recovery"] = store.recover_checkpoint()
        result["after_recovery"] = store.verify_checkpoint()
    result["command"] = "verify-checkpoint"
    result["status"] = "success" if result.get("ok") or (recover and result.get("after_recovery", {}).get("ok")) else "partial_failure"
    result["sync_marker"] = short_marker({k: v for k, v in result.items() if k != "sync_marker"})
    return result


def _bundle_summary(bundle: Any) -> dict[str, Any]:
    return {
        "users": len(bundle.users),
        "devices": len(bundle.devices),
        "factors": len(bundle.factors),
        "sessions": len(bundle.sessions),
        "edges": len(bundle.edges),
        "attempts": len(bundle.attempts),
        "valid_attempts": len(bundle.valid_attempts),
        "revocations": len(bundle.revocations),
        "v2_shard_records": len(bundle.v2_shard_records),
        "v2_meta_rows": len(bundle.v2_meta_rows),
        "rejections": len(bundle.rejections),
    }


def exit_code_for_payload(payload: dict[str, Any]) -> int:
    status = payload.get("status")
    if status == "parameter_error":
        return 2
    if status in {"partial_failure", "error"}:
        return 1
    return 0


def _adjudication_sync_marker(payload: dict[str, Any]) -> str:
    closure = payload.get("invalidation_closure") or {}
    return short_marker(
        {
            "snapshot_version": payload.get("snapshot_version"),
            "target_watermark": payload.get("target_watermark"),
            "results": payload.get("results", []),
            "rejections": payload.get("rejections", []),
            "policy_conflicts": payload.get("policy_conflicts", []),
            "closure_nodes": closure.get("closure_nodes", []),
            "cycles": closure.get("cycles", []),
        }
    )


def _import_sync_marker(payload: dict[str, Any]) -> str:
    return short_marker(
        {
            "snapshot_version": payload.get("snapshot_version"),
            "observation_clock_ms": payload.get("observation_clock_ms"),
            "summary": payload.get("summary"),
            "rejections": payload.get("rejections", []),
            "policy_conflicts": payload.get("policy_conflicts", []),
            "unified_policies": payload.get("unified_policies", {}),
        }
    )


def _revocation_sync_marker(payload: dict[str, Any]) -> str:
    return short_marker(
        {
            "watermark": payload.get("watermark"),
            "closure": payload.get("closure", {}),
            "recomputed_results": payload.get("recomputed_results", []),
            "rejections": payload.get("rejections", []),
            "policy_conflicts": payload.get("policy_conflicts", []),
        }
    )
