from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .checkpoint import CheckpointManager, CheckpointLockError
from .codes import E_LOCK, OK
from .io_utils import atomic_write_json, cleanup_temp_files
from .merge import MergedView, apply_batch, new_view
from .parser import SampleInput, load_sample_input


JsonDict = dict[str, Any]


def entity_conclusions(view: MergedView) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for key in sorted(view.entities):
        entity = view.entities[key]
        authoritative = entity.authoritative
        codes = [conflict["code"] for conflict in entity.conflicts]
        codes.extend(finding["code"] for finding in entity.findings)
        if entity.status == "tombstoned":
            conclusion = "tombstoned"
        elif entity.status == "conflict":
            conclusion = "state_conflict"
        else:
            conclusion = "active"
        rows.append(
            {
                "entity_key": key,
                "entity_type": entity.entity_type,
                "conclusion": conclusion,
                "business_codes": sorted(set(codes)) or [OK],
                "status": entity.status,
                "generation": authoritative.generation if authoritative else None,
                "authoritative_source": authoritative.source_dict() if authoritative else None,
                "sources": [record.source_dict() for record in entity.records],
                "conflict_fields": sorted({item["field"] for item in entity.conflicts}),
                "conflicts": entity.conflicts,
                "findings": entity.findings,
                "scope": authoritative.scope if authoritative else [],
                "client_id": authoritative.client_id if authoritative else None,
                "expires_at_ms": authoritative.expires_at_ms if authoritative else None,
                "tombstoned": bool(authoritative and authoritative.tombstoned),
            }
        )
    return rows


def import_sample(
    sample_dir: str | Path,
    state_dir: str | Path,
    *,
    clock_path: Optional[str | Path] = None,
    force_rebuild: bool = False,
) -> JsonDict:
    sample = load_sample_input(sample_dir, clock_path)
    manager = CheckpointManager(state_dir, clock_ms=sample.clock_ms)
    recovery = manager.recover()

    rebuilt = False
    resume_watermark = 0
    if force_rebuild:
        view = new_view(sample.rejections)
        rebuilt = True
    elif recovery["status"] == "rebuild_required":
        view = new_view(sample.rejections)
        rebuilt = True
    elif recovery.get("checkpoint"):
        checkpoint = recovery["checkpoint"]
        view = MergedView.from_dict(checkpoint["view"])
        resume_watermark = int(checkpoint["watermark"])
    else:
        view = new_view(sample.rejections)

    commit_info: Optional[JsonDict] = None
    if rebuilt:
        for batch in sample.batches:
            apply_batch(view, batch.batch_id, batch.records)
        commit_info = manager.rebuild(view, len(sample.batches), sample.batches)
    else:
        watermark = resume_watermark
        for batch in sample.batches:
            if batch.sequence <= resume_watermark:
                continue
            apply_batch(view, batch.batch_id, batch.records)
            watermark = batch.sequence
            commit_info = manager.commit(view, watermark, batch)
        if commit_info is None and sample.batches:
            current = manager.recover()
            if current.get("checkpoint"):
                commit_info = {
                    "status": "unchanged",
                    "watermark": current["checkpoint"]["watermark"],
                }

    report = _import_report(
        sample=sample,
        view=view,
        recovery=recovery,
        commit_info=commit_info,
        rebuilt=rebuilt,
        manager=manager,
    )
    return report


def _import_report(
    *,
    sample: SampleInput,
    view: MergedView,
    recovery: JsonDict,
    commit_info: Optional[JsonDict],
    rebuilt: bool,
    manager: CheckpointManager,
) -> JsonDict:
    stats = view.stats()
    issue_total = (
        stats["rejection_total"]
        + stats["conflict_total"]
        + stats["pkce_finding_total"]
    )
    checkpoint_verify = manager.verify()
    return {
        "report_type": "import",
        "ok": issue_total == 0 and checkpoint_verify.get("code") == OK,
        "sample_dir": str(sample.sample_dir).replace("\\", "/"),
        "clock_path": str(sample.clock_path).replace("\\", "/"),
        "observed_at_ms": sample.clock_ms,
        "checkpoint": {
            "recovery": _json_safe_recovery(recovery),
            "commit": commit_info,
            "verify": checkpoint_verify,
            "rebuilt": rebuilt,
        },
        "summary": {
            **stats,
            "issue_total": issue_total,
            "batch_total_available": len(sample.batches),
        },
        "entity_conclusions": entity_conclusions(view),
        "conflicts": view.conflicts,
        "pkce_findings": view.pkce_findings,
        "rejections": view.rejections,
    }


def _json_safe_recovery(recovery: JsonDict) -> JsonDict:
    safe = dict(recovery)
    safe.pop("checkpoint", None)
    safe.pop("snapshot_data", None)
    return safe


def write_report(report: JsonDict, report_path: str | Path) -> Path:
    path = Path(report_path)
    if path.parent.exists():
        cleanup_temp_files(path.parent)
    return atomic_write_json(path, report)


def run_batch_import(
    batch_dir: str | Path,
    state_root: str | Path,
    report_dir: Optional[str | Path] = None,
    force_rebuild: bool = False,
) -> JsonDict:
    root = Path(batch_dir)
    state_root_path = Path(state_root)
    names = sorted(path.name for path in root.iterdir() if path.is_dir()) if root.exists() else []
    sample_reports: list[JsonDict] = []

    for name in names:
        sample_path = root / name
        state_path = state_root_path / name
        try:
            report = import_sample(sample_path, state_path, force_rebuild=force_rebuild)
            sample_reports.append(
                {
                    "name": name,
                    "sample_dir": str(sample_path).replace("\\", "/"),
                    "state_dir": str(state_path).replace("\\", "/"),
                    "ok": bool(report["ok"]),
                    "report": report,
                }
            )
        except CheckpointLockError as exc:
            sample_reports.append(
                {
                    "name": name,
                    "ok": False,
                    "code": E_LOCK,
                    "error": str(exc),
                }
            )
        except Exception as exc:  # one malformed sample must not stop peers
            sample_reports.append(
                {
                    "name": name,
                    "ok": False,
                    "code": "E_SAMPLE_EXCEPTION",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    succeeded = sum(1 for item in sample_reports if item.get("ok"))
    failed = len(sample_reports) - succeeded
    aggregate = {
        "report_type": "batch_import",
        "batch_dir": str(root).replace("\\", "/"),
        "total": len(sample_reports),
        "succeeded": succeeded,
        "failed": failed,
        "all_succeeded": failed == 0 and len(sample_reports) > 0,
        "samples": sample_reports,
    }

    if report_dir is not None:
        out_dir = Path(report_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        cleanup_temp_files(out_dir)
        for item in sample_reports:
            if "report" in item:
                atomic_write_json(out_dir / f"{item['name']}.json", item["report"])
        atomic_write_json(out_dir / "batch-summary.json", aggregate)
    return aggregate
