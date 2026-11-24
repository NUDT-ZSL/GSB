from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

from . import LEDGER_LAYOUT_VERSION, __version__
from .atomicio import atomic_write_json
from .atomicio import canonical_json_bytes
from .codecs import ERROR_BOUND_CONTRACT
from .engine import AuditEngine
from .locking import WorkDirLock
from .parser import parse_bundle
from .store import AuditStore, CheckpointMismatch
import hashlib


def build_report(
    engine: AuditEngine,
    analysis: dict[str, Any],
    final_digest: str,
    sample_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decisions = analysis["request_reports"]
    decision_counts = {"allow": 0, "deny": 0, "uncertain": 0}
    for decision in decisions:
        decision_counts[decision["decision"]] = decision_counts.get(decision["decision"], 0) + 1
    issues = [issue.to_dict() for issue in engine.bundle.issues]
    return {
        "report_version": __version__,
        "ledger_layout_version": LEDGER_LAYOUT_VERSION,
        "sample": sample_manifest or {},
        "summary": {
            "principal_count": len(engine.bundle.principals),
            "object_count": len(engine.bundle.objects),
            "ace_count": len(engine.bundle.aces),
            "request_count": len(engine.bundle.requests),
            "rejection_count": len(issues),
            "decision_counts": decision_counts,
            "deny_shadow_count": len(analysis["deny_shadows"]),
            "inheritance_amplification_count": len(analysis["inheritance_amplifications"]),
            "policy_divergence_count": len(analysis["policy_divergences"]),
            "canonical_alias_group_count": sum(
                len(groups) for groups in analysis["canonical_aliases"].values()
            ),
            "final_ledger_digest": final_digest,
        },
        "final_ledger_digest": final_digest,
        "error_bound_contract": ERROR_BOUND_CONTRACT,
        "decisions": decisions,
        "deny_shadows": analysis["deny_shadows"],
        "inheritance_amplifications": analysis["inheritance_amplifications"],
        "policy_divergences": analysis["policy_divergences"],
        "canonical_aliases": analysis["canonical_aliases"],
        "policy_metrics": analysis["policy_metrics"],
        "rejections": issues,
    }


def load_sample_manifest(sample_dir: Path) -> dict[str, Any]:
    path = sample_dir / "manifest.json"
    if not path.exists():
        return {}
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def run_audit(
    sample_dir: str | Path,
    workdir: str | Path,
    *,
    crash_after_objects: int | None = None,
    lock_timeout_ms: int = 0,
    report_name: str = "report.json",
) -> dict[str, Any]:
    sample_path = Path(sample_dir)
    workdir_path = Path(workdir)
    workdir_path.mkdir(parents=True, exist_ok=True)

    with WorkDirLock(workdir_path, timeout_ms=lock_timeout_ms):
        bundle = parse_bundle(sample_path)
        engine = AuditEngine(bundle)
        analysis = engine.analyze()
        all_rows = analysis["ledger_rows"]
        sample_manifest = load_sample_manifest(sample_path)

        with AuditStore(workdir_path) as store:
            recovery_info = store.recover()
            start_watermark = int(recovery_info.get("resumed_from_watermark", 0))
            watermark_map = analysis["watermark_map"]
            if start_watermark:
                checkpoint = store.latest_checkpoint()
                prefix_rows = [
                    row for row in all_rows if int(row["watermark"]) <= start_watermark
                ]
                replay_digest = hashlib.sha256(
                    canonical_json_bytes(AuditStore._digest_tuples(prefix_rows))
                ).hexdigest()
                if checkpoint is None or replay_digest != checkpoint["ledger_digest"]:
                    raise CheckpointMismatch(
                        "replayed prefix does not match last complete checkpoint"
                    )
                recovery_info["boundary_replay_verified"] = True
                store.recovery_info["boundary_replay_verified"] = True

            for obj in engine.ordered_objects:
                watermark = watermark_map[obj.object_id]
                if watermark <= start_watermark:
                    continue
                store.commit_object_checkpoint(all_rows, watermark, obj.object_id)
                if crash_after_objects is not None and watermark >= crash_after_objects:
                    # Simulate SIGKILL after the SQLite transaction and snapshot
                    # replacement are durable, before any final report exists.
                    if os.name == "nt":  # pragma: no cover - Windows termination
                        os._exit(137)
                    os.kill(os.getpid(), signal.SIGKILL)

            final_digest = store.ledger_digest()
            report = build_report(engine, analysis, final_digest, sample_manifest)
            store.save_final_results(
                analysis["request_reports"],
                analysis["deny_shadows"]
                + analysis["inheritance_amplifications"]
                + analysis["policy_divergences"],
                bundle.issues,
                engine._default_policy().policy_id,
            )
            report_path = workdir_path / report_name
            atomic_write_json(report_path, report)
            canonical_dump = store.canonical_dump()
            return {
                "report": report,
                "report_path": str(report_path),
                "workdir": str(workdir_path),
                "recovery": recovery_info,
                "engine": engine,
                "canonical_dump": canonical_dump,
            }
