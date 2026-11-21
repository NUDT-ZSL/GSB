from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .store import SnapshotStore, STAGE_ORDER


def _read_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_state(
    *,
    store_path: str | Path,
    output_dir: str | Path,
    data_dir: str | Path,
) -> dict[str, Any]:
    store_path = Path(store_path)
    output_dir = Path(output_dir)
    data_dir = Path(data_dir)
    if not store_path.exists():
        return {
            "ok": True,
            "code": ErrorCode.OK,
            "current_stage": "not_started",
            "snapshots": [],
            "generations": [],
            "recent_errors": [],
            "selfcheck": None,
            "recovery_report": None,
            "degradations": [],
            "affected": {},
            "recomputed": {},
            "reused": {},
        }
    try:
        store = SnapshotStore(store_path)
        active = store.active_generation()
        generations = [generation.to_dict() for generation in store.list_generations()]
        all_snapshots = [snapshot.summary() for snapshot in store.list_snapshots()]
        recent_errors = store.recent_events(30)
        if active is None:
            return {
                "ok": True,
                "code": ErrorCode.OK,
                "current_stage": "not_started",
                "active_generation": None,
                "generations": generations,
                "snapshots": all_snapshots,
                "recent_errors": recent_errors,
                "selfcheck": None,
                "recovery_report": None,
                "degradations": [],
                "affected": {},
                "recomputed": {},
                "reused": {},
            }

        current_records = {
            record.stage: record
            for record in store.list_snapshots(active.generation)
        }
        stage_cards: list[dict[str, Any]] = []
        degradations: list[dict[str, Any]] = []
        affected: dict[str, list[str]] = {}
        recomputed: dict[str, list[str]] = {}
        reused: dict[str, list[str]] = {}
        for stage in STAGE_ORDER:
            record = current_records.get(stage)
            if record is None:
                stage_cards.append(
                    {
                        "stage": stage,
                        "committed": False,
                        "snapshot_version": None,
                        "row_count": 0,
                        "completed_doc_count": 0,
                    }
                )
                continue
            payload = record.payload
            row_count = int(
                payload.get(
                    "line_count",
                    payload.get("doc_count", len(payload.get("lines", []))),
                )
            )
            stage_cards.append(
                {
                    "stage": stage,
                    "committed": True,
                    "snapshot_version": record.snapshot_version,
                    "payload_checksum": record.payload_checksum,
                    "row_count": row_count,
                    "completed_doc_count": len(record.completed_doc_ids),
                    "completed_doc_ids": list(record.completed_doc_ids),
                    "stage_version": record.metadata.get("stage_version"),
                    "degradation_count": len(payload.get("degradations", [])),
                }
            )
            affected[stage] = list(record.metadata.get("affected_doc_ids", []))
            recomputed[stage] = list(record.metadata.get("recomputed_doc_ids", []))
            reused[stage] = list(record.metadata.get("reused_doc_ids", []))
            degradations.extend(payload.get("degradations", []))

        last_stage = None
        for stage in reversed(STAGE_ORDER):
            if stage in current_records:
                last_stage = stage
                break
        complete = last_stage == "summarize"
        run_meta = _read_json_if_exists(output_dir / "run_meta.json")
        selfcheck = _read_json_if_exists(output_dir / "selfcheck.json")
        manifest = _read_json_if_exists(output_dir / "manifest.json")
        sample_manifest = _read_json_if_exists(data_dir / "manifest.json")

        return {
            "ok": True,
            "code": ErrorCode.OK,
            "current_stage": last_stage or "not_started",
            "complete": complete,
            "active_generation": active.to_dict(),
            "generations": generations,
            "snapshots": all_snapshots,
            "stage_cards": stage_cards,
            "affected": affected,
            "recomputed": recomputed,
            "reused": reused,
            "degradations": degradations,
            "recovery_report": (
                run_meta.get("recovery_report") if isinstance(run_meta, dict) else None
            ),
            "selfcheck": selfcheck,
            "run_meta": run_meta,
            "manifest": manifest,
            "sample_manifest": sample_manifest,
            "recent_errors": recent_errors,
        }
    except BusinessError as exc:
        return {
            "ok": False,
            "code": exc.code,
            "current_stage": "unknown",
            "error": exc.to_dict(),
            "recent_errors": [],
            "degradations": [],
            "snapshots": [],
            "generations": [],
        }
    except Exception as exc:
        return {
            "ok": False,
            "code": ErrorCode.E_UNATTRIBUTED,
            "current_stage": "unknown",
            "error": {
                "code": ErrorCode.E_UNATTRIBUTED,
                "message": f"{type(exc).__name__}: {exc}",
            },
            "recent_errors": [],
            "degradations": [],
            "snapshots": [],
            "generations": [],
        }
