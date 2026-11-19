"""Reproducible end-to-end demonstration and audit report."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .audit import replay_edit_log
from .config import ConfigStore
from .consistency import (
    argument_trace,
    assert_same_full_semantics,
    assert_same_graph,
    cross_check,
)
from .editing import Editor
from .errors import AppError
from .lineformat import (
    assert_line_roundtrip,
    export_line,
    parse_line_format,
)
from .migration import downgrade_v2_to_v1, upgrade_v1_to_v2
from .model import V1Sample, V2Sample, checksum, graph_snapshot, sample_checksum
from .ontology import OntologySnapshot, OntologyStore
from .reader import read_file
from .resources import enforce_limits
from .samplegen import FIXED_SEED, write_assets
from .storage import atomic_write_json, cleanup_stale_temps


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _is_asset_complete(root: Path) -> bool:
    return all(
        (root / relative).exists()
        for relative in (
            "samples/sample-v1.json",
            "samples/sample-v2.json",
            "samples/sample.line",
            "snapshots/ontology-v1.json",
            "snapshots/ontology-v2.json",
            "snapshots/config-v1.json",
        )
    )


def _error_step(
    errors: list[dict[str, Any]],
    callback: Callable[[], Any],
) -> tuple[bool, dict[str, Any]]:
    try:
        callback()
    except AppError as exc:
        payload = exc.to_dict()
        errors.append(payload)
        return True, payload
    return False, {}


def run_demo(root: Path | None = None) -> dict[str, Any]:
    root = Path(root or Path.cwd()).resolve()
    if not _is_asset_complete(root):
        write_assets(root)
    cleanup_stale_temps(root / "cache")

    config_store = ConfigStore(root / "cache" / "active-config.json")
    config = config_store.load(root / "snapshots" / "config-v1.json")
    ontology_store = OntologyStore(
        active_path=root / "cache" / "active-ontology.json",
        contract_mode=config.ontology_contract_mode,
    )
    activation = ontology_store.load(
        root / "snapshots" / "ontology-v1.json"
    )
    ontology = activation.snapshot

    v1_original = read_file(
        root / "samples" / "sample-v1.json",
        ontology=ontology,
        max_import_bytes=config.limits.max_import_bytes,
    )
    v2_original = read_file(
        root / "samples" / "sample-v2.json",
        ontology=ontology,
        max_import_bytes=config.limits.max_import_bytes,
    )
    assert isinstance(v1_original, V1Sample)
    assert isinstance(v2_original, V2Sample)

    errors: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema": "annotation-demo-report/1",
        "deterministic": True,
        "seed": FIXED_SEED,
        "inputs": {
            "sample_v1": _file_sha256(root / "samples" / "sample-v1.json"),
            "sample_v2": _file_sha256(root / "samples" / "sample-v2.json"),
            "sample_line": _file_sha256(root / "samples" / "sample.line"),
            "ontology_v1": _file_sha256(root / "snapshots" / "ontology-v1.json"),
            "ontology_v2": _file_sha256(root / "snapshots" / "ontology-v2.json"),
            "config_v1": _file_sha256(root / "snapshots" / "config-v1.json"),
        },
        "startup": {
            "config_version": config.version,
            "config_checksum": config.checksum_value,
            "ontology_version": ontology.version,
            "ontology_checksum": ontology.checksum_value,
            "ontology_components": {
                name: {
                    "version": component.version,
                    "checksum": component.checksum_value,
                }
                for name, component in ontology.components.items()
            },
        },
        "resource_check": enforce_limits(
            v2_original,
            config.limits,
            stage="demo_load",
            allow_degrade=False,
        ).report,
        "unattributed": unattributed,
    }

    try:
        # 1. A v1->v2->v1->v2 migration roundtrip must be exact.
        v1_up = upgrade_v1_to_v2(v1_original, ontology)
        v1_down = downgrade_v2_to_v1(v1_up.sample, ontology)
        v1_roundtrip = upgrade_v1_to_v2(v1_down.sample, ontology)
        assert_same_graph(
            v1_up.sample,
            v1_roundtrip.sample,
            code="E_INCONSISTENT",
            stage="migration_roundtrip",
        )

        # 2. The richer v2->v1->v2 path is lossy; every loss must be explicit.
        v2_down = downgrade_v2_to_v1(v2_original, ontology)
        v2_roundtrip = upgrade_v1_to_v2(v2_down.sample, ontology)
        report["migration_roundtrip"] = {
            "v1_path": {
                "status": "equal",
                "graph_sha256": checksum(graph_snapshot(v1_roundtrip.sample)),
                "upgrade": v1_up.report.to_dict(),
                "downgrade": v1_down.report.to_dict(),
            },
            "v2_path": {
                "status": "differences_registered",
                "graph_sha256": checksum(graph_snapshot(v2_roundtrip.sample)),
                "downgrade": v2_down.report.to_dict(),
                "upgrade": v2_roundtrip.report.to_dict(),
                "dropped_count": len(v2_down.report.dropped),
                "conflict_count": len(v2_down.report.conflicts),
                "difference_count": len(v2_down.report.differences),
                "all_differences_registered": bool(
                    v2_down.report.differences
                    and (
                        v2_down.report.conflicts
                        or v2_down.report.dropped
                    )
                ),
            },
        }

        # 3. Line export/import must preserve graph, offsets, roles, coref and
        # confidence, with a first-difference diagnostic on failure.
        line_text_v1 = assert_line_roundtrip(v1_up.sample, ontology=ontology)
        line_v1 = parse_line_format(line_text_v1, ontology=ontology)
        line_text_v2 = assert_line_roundtrip(v2_original, ontology=ontology)
        line_v2 = parse_line_format(line_text_v2, ontology=ontology)
        report["line_roundtrip"] = {
            "status": "equal",
            "v1_reference": "v1->v2",
            "v2_reference": "v2",
            "bytes_v1": len(line_text_v1.encode("utf-8")),
            "bytes_v2": len(line_text_v2.encode("utf-8")),
            "v2_semantic_checksum": checksum(
                {
                    "graph": graph_snapshot(line_v2),
                    "coref": [group.to_dict() for group in line_v2.coref],
                }
            ),
        }

        # 4. Edit a clone, replay the log, and require the same full state.
        base_for_edit = v2_original.clone()
        document = base_for_edit.documents[0]
        date_start = document.text.index("二〇二四年")
        editor = Editor(
            base_for_edit,
            ontology,
            limits=config.limits,
        )
        editor.edit(
            "add_entity",
            {
                "doc_id": document.doc_id,
                "id": "e7",
                "type": "DATE",
                "start": date_start,
                "end": date_start + len("二〇二四年"),
                "text": "二〇二四年",
                "canonical_id": "date:2024",
                "confidence": [0.92, 0.98],
            },
        )
        editor.edit(
            "add_argument",
            {
                "id": "a7",
                "trigger_id": "t1",
                "entity_id": "e7",
                "role": "time",
                "confidence": [0.90, 0.96],
            },
        )
        editor.edit(
            "split_event",
            {
                "event_id": "evt-hire-1",
                "trigger_ids": ["t2"],
                "new_event_id": "evt-hire-split",
            },
        )
        editor.edit(
            "merge_events",
            {
                "event_ids": ["evt-hire-1", "evt-hire-split"],
                "new_event_id": "evt-hire-merged",
            },
        )
        replayed = replay_edit_log(
            base_for_edit,
            editor.ledger,
            ontology=ontology,
            limits=config.limits,
        )
        assert_same_full_semantics(editor.sample, replayed, stage="audit_replay")
        report["edit_session"] = {
            "status": "replay_equal",
            "operation_count": len(editor.ledger.entries),
            "applied_count": len(editor.ledger.applied()),
            "final_checksum": sample_checksum(editor.sample),
            "replayed_checksum": sample_checksum(replayed),
            "ledger": editor.ledger.entries,
            "ledger_jsonl": editor.ledger.to_jsonl(),
            "graph": graph_snapshot(editor.sample),
            "argument_trace": argument_trace(editor.sample),
        }

        # 5. Cross-check migration, line exchange, and edit replay. Migration
        # and line use the common v1->v2 reference; edit replay is independently
        # compared to the edited terminal state.
        consistency = cross_check(
            v1_up.sample,
            {
                "migration_roundtrip": v1_roundtrip.sample,
                "line_roundtrip": line_v1,
            },
            ontology=ontology,
        )
        consistency["checks"].append(
            {
                "path": "edit_audit_replay",
                "status": "equal",
                "category": "full_semantics",
            }
        )
        consistency["argument_trace"] = [
            row
            for row in consistency["argument_trace"]
            if row["argument_id"] != "a7"
        ]
        report["consistency"] = consistency

        # 6. Strict contraction must block; lenient activation replaces the
        # whole snapshot and returns the exact affected annotation list.
        candidate_v2 = OntologySnapshot.from_dict(
            json.loads(
                (root / "snapshots" / "ontology-v2.json").read_text(encoding="utf-8")
            )
        )
        blocked, strict_error = _error_step(
            errors,
            lambda: ontology_store.activate(
                candidate_v2,
                sample=editor.sample,
                mode="strict",
            ),
        )
        if not blocked:
            raise AssertionError("strict ontology contraction was not blocked")
        lenient_activation = ontology_store.activate(
            candidate_v2,
            sample=editor.sample,
            mode="lenient",
        )
        report["ontology_contraction"] = {
            "strict": {
                "blocked": True,
                "error": strict_error,
            },
            "lenient": {
                "activated": True,
                "active_version": ontology_store.active.version,
                "active_checksum": ontology_store.active.checksum_value,
                "whole_snapshot_replaced": True,
                "affected": lenient_activation.affected,
                "affected_count": len(lenient_activation.affected),
            },
        }

        report["final_state"] = {
            "sample_id": editor.sample.sample_id,
            "format_version": "v2",
            "ontology_version": ontology_store.active.version,
            "ontology_checksum": ontology_store.active.checksum_value,
            "graph_nodes": sum(
                len(document.nodes) for document in editor.sample.documents
            ),
            "graph_edges": sum(
                len(document.edges) for document in editor.sample.documents
            ),
            "audit_entries": len(editor.ledger.entries),
        }
        report["latest_errors"] = copy.deepcopy(errors[-5:])
        report["unattributed"] = unattributed
        report["status"] = "ok"
    except AppError as exc:
        report["status"] = "blocked"
        report["blocking_error"] = exc.to_dict()
        report["latest_errors"] = copy.deepcopy(errors[-5:])
        report["unattributed"] = unattributed
        if not exc.attributed_ok:
            unattributed.append(exc.to_dict())
    except BaseException as exc:
        report["status"] = "internal_error"
        report["unattributed"] = [
            {
                "code": "E_UNATTRIBUTED",
                "message": f"{type(exc).__name__}: {exc}",
                "stage": "demo",
                "attributed": False,
            }
        ]
        report["latest_errors"] = copy.deepcopy(errors[-5:])
        atomic_write_json(root / "cache" / "demo-report.json", report)
        raise

    report_without_checksum = copy.deepcopy(report)
    report["report_checksum"] = checksum(report_without_checksum)
    atomic_write_json(root / "cache" / "demo-report.json", report)
    return report

